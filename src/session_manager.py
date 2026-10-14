"""
Proje V1 — Session & Browser Yönetimi (Thread-based, Windows uyumlu)
Playwright sync_api ayrı bir daemon thread'de çalışır.
Windows ProactorEventLoop ile async_playwright uyumsuzluğunu çözer.
SOLID: Single Responsibility — sadece browser oturum yönetimi.
"""
import logging
import random
import threading
import queue
import uuid
from pathlib import Path
from typing import Optional, Callable, Any

from playwright.sync_api import (
    sync_playwright,
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
)

from config import (
    SESSION_FILE,
    HEADLESS,
    VIEWPORT,
    NAVIGATION_TIMEOUT,
    CONSENT_BUTTON_SELECTORS,
    MAPS_LOADED_SELECTOR,
    SCREENSHOTS_DIR,
    SCREENSHOT_TIMEOUT,
)

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.6312.122 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]


class _BrowserTask:
    """Browser thread'e gönderilecek görev paketi."""
    def __init__(self, fn: Callable, *args, **kwargs):
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.result_queue: queue.Queue = queue.Queue()


class BrowserThread(threading.Thread):
    """
    Playwright sync_api'yi ayrı bir daemon thread'de çalıştırır.
    FastAPI'nin asyncio event loop'undan bağımsızdır.
    """

    def __init__(self) -> None:
        super().__init__(daemon=True, name="playwright-browser")
        self._task_queue: queue.Queue[Optional[_BrowserTask]] = queue.Queue()
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._user_agent: str = random.choice(USER_AGENTS)
        self._started_event = threading.Event()
        self._error: Optional[Exception] = None

    def run(self) -> None:
        """Thread ana döngüsü — Playwright sync API buradan çalışır."""
        # ── Windows Fix: sync_playwright() dahili olarak asyncio.new_event_loop()
        # çağırır ve bu loop üzerinden create_subprocess_exec() ile browser
        # sürecini başlatır. Windows'ta subprocess yalnızca ProactorEventLoop
        # ile desteklenir. Global policy SelectorEventLoop ayarlanmışsa bile
        # bu thread için ProactorEventLoop'u garanti altına alıyoruz.
        import asyncio, sys
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
            loop = asyncio.ProactorEventLoop()
            asyncio.set_event_loop(loop)

        try:
            self._playwright = sync_playwright().start()
            self._launch_browser()
            self._started_event.set()
            logger.info("BrowserThread hazır.")

            # Görev döngüsü
            while True:
                task = self._task_queue.get()
                if task is None:  # Durdurma sinyali
                    break
                try:
                    result = task.fn(*task.args, **task.kwargs)
                    task.result_queue.put(("ok", result))
                except Exception as e:
                    task.result_queue.put(("error", e))

        except Exception as e:
            self._error = e
            self._started_event.set()
            logger.error(f"BrowserThread başlatma hatası: {e}", exc_info=True)
        finally:
            self._cleanup()

    def wait_ready(self, timeout: float = 30.0) -> None:
        """Thread hazır olana kadar bekle."""
        self._started_event.wait(timeout=timeout)
        if self._error:
            raise self._error

    def execute(self, fn: Callable, *args, **kwargs) -> Any:
        """Browser thread'de senkron bir fonksiyon çalıştır, sonucu döndür."""
        task = _BrowserTask(fn, *args, **kwargs)
        self._task_queue.put(task)
        status, result = task.result_queue.get(timeout=60.0)
        if status == "error":
            raise result
        return result

    def stop(self) -> None:
        """Thread'i durdur."""
        self._task_queue.put(None)
        self.join(timeout=10.0)

    def _launch_browser(self) -> None:
        """Browser ve context başlat."""
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-infobars",
            "--window-size=1280,800",
            "--disable-extensions",
            "--lang=tr-TR,tr",
        ]
        self._browser = self._playwright.chromium.launch(
            headless=HEADLESS,
            args=launch_args,
        )
        self._create_context()

    def _create_context(self) -> None:
        context_kwargs = dict(
            viewport=VIEWPORT,
            user_agent=self._user_agent,
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            bypass_csp=True,
            java_script_enabled=True,
            extra_http_headers={
                "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            },
        )
        if SESSION_FILE.exists():
            context_kwargs["storage_state"] = str(SESSION_FILE)
            logger.info("Kayıtlı session yüklendi.")
        self._context = self._browser.new_context(**context_kwargs)
        self._context.set_default_timeout(NAVIGATION_TIMEOUT)

    def _cleanup(self) -> None:
        try:
            if self._context:
                try:
                    self._context.storage_state(path=str(SESSION_FILE))
                except Exception:
                    pass
                self._context.close()
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception as e:
            logger.error(f"Cleanup hatası: {e}")


class SessionManager:
    """
    BrowserThread üzerinden Playwright işlemlerini yönetir.
    Tüm browser işlemleri _browser_thread.execute() ile thread'e gönderilir.
    """

    def __init__(self) -> None:
        self._thread: Optional[BrowserThread] = None

    def start(self) -> None:
        """Browser thread'i başlat ve hazır olmasını bekle."""
        self._thread = BrowserThread()
        self._thread.start()
        self._thread.wait_ready(timeout=30.0)
        logger.info("SessionManager başlatıldı.")

    def stop(self) -> None:
        if self._thread:
            self._thread.stop()
            self._thread = None
        logger.info("SessionManager durduruldu.")

    def take_screenshot(self, record: dict, max_retries: int = 3) -> dict:
        """
        Browser thread'de Google Maps sayfasının screenshot'ını al.
        record: {row_index, url, name, position, total}
        Returns: {file_path, row_index, name, url, position, total, error}
        """
        return self._thread.execute(self._do_screenshot, record, 0, max_retries)

    def refresh_session(self) -> None:
        """Session'ı yenile (bot bloğu durumunda)."""
        self._thread.execute(self._do_refresh)

    # ── Browser thread'de çalışan metodlar ──────────────────────────────────

    def _do_screenshot(self, record: dict, retry: int, max_retries: int) -> dict:
        """Sync modda screenshot al (BrowserThread içinde çalışır)."""
        page: Optional[Page] = None
        try:
            page = self._thread._context.new_page()
            self._apply_stealth(page)

            page.goto(record["url"], wait_until="domcontentloaded", timeout=SCREENSHOT_TIMEOUT)
            self._handle_consent(page)

            if self._is_blocked(page):
                logger.warning("Bot tespiti, session yenileniyor...")
                page.close()
                self._do_refresh()
                if retry < max_retries:
                    return self._do_screenshot(record, retry + 1, max_retries)
                return self._error_result(record, "Bot bloğu")

            # Harita yüklenmeyi bekle
            try:
                page.wait_for_selector(MAPS_LOADED_SELECTOR, timeout=8_000)
            except PlaywrightTimeout:
                pass

            import time
            time.sleep(1.5)

            file_name = f"{uuid.uuid4().hex}.png"
            file_path = SCREENSHOTS_DIR / file_name
            page.screenshot(
                path=str(file_path),
                full_page=False,
                clip={"x": 0, "y": 0, "width": 1280, "height": 800},
            )
            page.close()
            return {**record, "file_path": str(file_path), "error": None}

        except PlaywrightTimeout as e:
            logger.error(f"Timeout — {record['url'][:60]}: {e}")
            if retry < max_retries:
                return self._do_screenshot(record, retry + 1, max_retries)
            return self._error_result(record, f"Timeout: {str(e)[:100]}")

        except Exception as e:
            logger.error(f"Screenshot hatası — {record['url'][:60]}: {e}")
            return self._error_result(record, str(e)[:200])

        finally:
            if page and not page.is_closed():
                try:
                    page.close()
                except Exception:
                    pass

    def _do_refresh(self) -> None:
        """Context'i yenile (BrowserThread içinde çalışır)."""
        try:
            if self._thread._context:
                self._thread._context.close()
            self._thread._user_agent = random.choice(USER_AGENTS)
            self._thread._create_context()
            logger.info("Session yenilendi.")
        except Exception as e:
            logger.error(f"Session yenileme hatası: {e}")

    def _apply_stealth(self, page: Page) -> None:
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){}, app: {} };
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['tr-TR','tr','en-US','en'] });
        """)

    def _handle_consent(self, page: Page) -> None:
        for selector in CONSENT_BUTTON_SELECTORS:
            try:
                btn = page.locator(selector).first
                if btn.is_visible(timeout=2000):
                    btn.click()
                    page.wait_for_load_state("networkidle", timeout=10_000)
                    logger.info(f"Consent kabul edildi: {selector}")
                    return
            except Exception:
                continue

    def _is_blocked(self, page: Page) -> bool:
        url = page.url.lower()
        title = page.title().lower()
        if any(kw in url for kw in ("sorry", "captcha")):
            return True
        if any(kw in title for kw in ("unusual traffic", "robot", "captcha")):
            return True
        try:
            return page.locator('form[action*="sorry"]').is_visible(timeout=500)
        except Exception:
            return False

    @staticmethod
    def _error_result(record: dict, error_msg: str) -> dict:
        return {**record, "file_path": "", "error": error_msg}
