"""
Proje V1 — Eş Zamanlı Ekran Görüntüsü Motoru (CaptureEngine)

Eski yapı: tek Playwright thread'i, sync API, görevler SIRAYLA çalışıyordu
(ThreadPoolExecutor(2) olsa da BrowserThread işleri tek tek yürütüyordu).

Yeni yapı:
  • Tek Chromium süreci, çok sekme (CPU/RAM açısından ucuz).
  • Async Playwright kendi thread'indeki ProactorEventLoop'ta çalışır
    (uvicorn --reload Windows'ta SelectorEventLoop kullandığı için gerekli).
  • AdaptiveController: dinamik eş zamanlılık + adaptif hız + soğuma süresi.
  • Hata sınıflandırma: rate limit / geçici / kalıcı / geçersiz URL.
  • Exponential backoff + jitter ile kontrollü tekrar deneme.
  • CaptureStore: tamamlanan her yakalama anında kalıcı (checkpoint/resume),
    aynı mekan tekrar yakalanmaz (önbellek + uçuştaki istek paylaşımı).
  • Atomik dosya yazımı (tmp → replace): kesintide yarım dosya kalmaz.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import random
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)

import config
from capture_store import CaptureStore
from data_model import CaptureOutcome, ErrorKind, PlaceRecord, capture_file_stem
from rate_control import AdaptiveController, ResourceProbe, backoff_delay

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.6312.122 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

INIT_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){}, app: {} };
    Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
    Object.defineProperty(navigator, 'languages', { get: () => ['tr-TR','tr','en-US','en'] });
"""

TIMEOUT_PREFIX = "Zaman aşımı: "

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-infobars",
    "--window-size=1280,800",
    "--disable-extensions",
    "--lang=tr-TR,tr",
]


@dataclass
class CaptureSettings:
    min_concurrency: int = config.CAPTURE_MIN_CONCURRENCY
    start_concurrency: int = config.CAPTURE_START_CONCURRENCY
    max_concurrency: int = config.CAPTURE_MAX_CONCURRENCY
    max_rate_per_min: float = config.CAPTURE_MAX_RATE_PER_MIN
    max_attempts: int = config.CAPTURE_MAX_ATTEMPTS
    max_timeout_attempts: int = config.CAPTURE_MAX_TIMEOUT_ATTEMPTS
    backoff_base: float = config.BACKOFF_BASE_SEC
    backoff_cap: float = config.BACKOFF_CAP_SEC
    cooldown_base: float = config.RATE_LIMIT_COOLDOWN_SEC
    cooldown_cap: float = config.RATE_LIMIT_COOLDOWN_CAP_SEC
    page_timeout_ms: int = config.SCREENSHOT_TIMEOUT
    maps_wait_ms: int = config.MAPS_WAIT_TIMEOUT
    settle_ms: int = config.SETTLE_DELAY_MS
    image_format: str = config.SCREENSHOT_FORMAT
    image_quality: int = config.SCREENSHOT_QUALITY
    headless: bool = config.HEADLESS
    context_recycle_pages: int = config.CONTEXT_RECYCLE_PAGES
    screenshots_dir: Path = config.SCREENSHOTS_DIR
    db_file: Path = config.CAPTURE_DB_FILE
    cache_ttl_hours: float = config.CAPTURE_CACHE_TTL_HOURS
    session_file: Optional[Path] = config.SESSION_FILE
    resource_guard: bool = True
    retry_failed_after_sec: float = 300.0   # başarısız kayıt bu süreden sonra tekrar denenebilir


class _Counters(dict):
    def inc(self, key: str, n: float = 1) -> None:
        self[key] = self.get(key, 0) + n


class CaptureEngine:
    def __init__(self, settings: Optional[CaptureSettings] = None) -> None:
        self.s = settings or CaptureSettings()
        self.s.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self.store = CaptureStore(self.s.db_file, self.s.screenshots_dir, self.s.cache_ttl_hours)
        probe = ResourceProbe(config.RESOURCE_CPU_HIGH_PCT, config.RESOURCE_MIN_FREE_MB) \
            if self.s.resource_guard else None
        self.controller = AdaptiveController(
            min_concurrency=self.s.min_concurrency,
            start_concurrency=self.s.start_concurrency,
            max_concurrency=self.s.max_concurrency,
            max_rate_per_min=self.s.max_rate_per_min,
            cooldown_base=self.s.cooldown_base,
            cooldown_cap=self.s.cooldown_cap,
            probe=probe,
        )
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._pw = None
        self._browser: Optional[Browser] = None
        self._ctx: Optional[BrowserContext] = None
        self._ctx_pages = 0
        self._ctx_refs: dict[int, int] = {}
        self._retiring: dict[int, BrowserContext] = {}
        self._needs_new_context = False
        self._ctx_lock: Optional[asyncio.Lock] = None
        self._inflight: dict[str, asyncio.Task] = {}
        self._waiters: dict[str, int] = {}
        self._user_agent = random.choice(USER_AGENTS)
        self.counters = _Counters()
        self._started_at = 0.0
        self._ext = "jpg" if self.s.image_format == "jpeg" else "png"

    # ── Yaşam döngüsü ────────────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return self._loop is not None and self._thread is not None and self._thread.is_alive()

    def start(self, timeout: float = 60.0) -> None:
        if self.running:
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._thread_main, name="capture-engine", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("Capture engine thread başlatılamadı.")
        self.submit(self._startup()).result(timeout=timeout)
        self._started_at = time.time()
        logger.info(
            f"CaptureEngine hazır (eş zamanlılık {self.s.min_concurrency}-{self.s.max_concurrency}, "
            f"tavan {self.s.max_rate_per_min:.0f}/dk, format {self.s.image_format})"
        )

    def stop(self, timeout: float = 30.0) -> None:
        if not self.running:
            return
        try:
            self.submit(self._shutdown()).result(timeout=timeout)
        except Exception as e:
            logger.warning(f"CaptureEngine kapatma uyarısı: {e}")
        loop = self._loop
        if loop:
            loop.call_soon_threadsafe(loop.stop)
        if self._thread:
            self._thread.join(timeout=10)
        self._loop = None
        self._thread = None
        logger.info(f"CaptureEngine durduruldu. {self.stats()}")

    def _thread_main(self) -> None:
        if sys.platform == "win32":
            loop: asyncio.AbstractEventLoop = asyncio.ProactorEventLoop()
        else:
            loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    def submit(self, coro) -> concurrent.futures.Future:
        if not self._loop:
            coro.close()
            raise RuntimeError("CaptureEngine çalışmıyor.")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _startup(self) -> None:
        self._ctx_lock = asyncio.Lock()
        self._pw = await async_playwright().start()
        await self._launch_browser()

    async def _shutdown(self) -> None:
        for task in list(self._inflight.values()):
            task.cancel()
        if self._inflight:
            await asyncio.gather(*self._inflight.values(), return_exceptions=True)
        if self._ctx and self.s.session_file:
            try:
                await self._ctx.storage_state(path=str(self.s.session_file))
            except Exception:
                pass
        for ctx in [self._ctx, *self._retiring.values()]:
            if ctx:
                try:
                    await ctx.close()
                except Exception:
                    pass
        self._ctx = None
        self._retiring.clear()
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._pw:
            await self._pw.stop()

    async def _launch_browser(self) -> None:
        self._browser = await self._pw.chromium.launch(headless=self.s.headless, args=LAUNCH_ARGS)
        self._ctx = None
        self._ctx_pages = 0

    async def _new_context(self) -> BrowserContext:
        kwargs: dict[str, Any] = dict(
            viewport=config.VIEWPORT,
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
        if self.s.session_file and self.s.session_file.exists():
            kwargs["storage_state"] = str(self.s.session_file)
        ctx = await self._browser.new_context(**kwargs)
        ctx.set_default_timeout(config.NAVIGATION_TIMEOUT)
        await ctx.add_init_script(INIT_SCRIPT)
        return ctx

    async def _acquire_context(self) -> BrowserContext:
        async with self._ctx_lock:  # type: ignore[union-attr]
            if self._browser is None or not self._browser.is_connected():
                logger.warning("Tarayıcı bağlantısı kopmuş; yeniden başlatılıyor.")
                self.counters.inc("browser_restarts")
                await self._launch_browser()
            rotate = (self._ctx is not None and
                      (self._needs_new_context or self._ctx_pages >= self.s.context_recycle_pages))
            if rotate:
                old = self._ctx
                self._retiring[id(old)] = old
                self._ctx = None
                await self._maybe_close_retired(id(old))
                self.counters.inc("context_rotations")
            if self._ctx is None:
                self._ctx = await self._new_context()
                self._ctx_pages = 0
                self._needs_new_context = False
            self._ctx_pages += 1
            self._ctx_refs[id(self._ctx)] = self._ctx_refs.get(id(self._ctx), 0) + 1
            return self._ctx

    async def _release_context(self, ctx: BrowserContext) -> None:
        self._ctx_refs[id(ctx)] = self._ctx_refs.get(id(ctx), 1) - 1
        await self._maybe_close_retired(id(ctx))

    async def _maybe_close_retired(self, key: int) -> None:
        if key in self._retiring and self._ctx_refs.get(key, 0) <= 0:
            ctx = self._retiring.pop(key)
            self._ctx_refs.pop(key, None)
            try:
                await ctx.close()
            except Exception:
                pass

    # ── Public API (herhangi bir thread / event loop'tan) ─────────────────────
    async def capture(self, record: PlaceRecord | dict, force: bool = False) -> CaptureOutcome:
        rec = PlaceRecord.from_dict(record) if isinstance(record, dict) else record
        return await asyncio.wrap_future(self.submit(self._capture(rec, force)))

    def capture_sync(self, record: PlaceRecord | dict, force: bool = False,
                     timeout: Optional[float] = None) -> CaptureOutcome:
        rec = PlaceRecord.from_dict(record) if isinstance(record, dict) else record
        return self.submit(self._capture(rec, force)).result(timeout=timeout)

    def is_cached(self, record: PlaceRecord) -> bool:
        return self.store.get_ok_file(record.place_key) is not None

    def stats(self) -> dict:
        elapsed = max(1e-6, time.time() - self._started_at) if self._started_at else 0.0
        c = dict(self.counters)
        captured = c.get("captured", 0)
        return {
            **c,
            "inflight": len(self._inflight),
            "elapsed_sec": round(elapsed, 1),
            "captured_per_min": round(captured / elapsed * 60, 2) if elapsed else 0.0,
            "avg_capture_sec": round(c.get("capture_time", 0) / captured, 2) if captured else None,
            "controller": self.controller.snapshot(),
        }

    # ── Engine loop içinde çalışan kısım ─────────────────────────────────────
    async def _capture(self, rec: PlaceRecord, force: bool) -> CaptureOutcome:
        t0 = time.monotonic()
        self.counters.inc("requested")
        key = rec.place_key
        if rec.url_error:
            self.counters.inc("invalid_url")
            self.store.mark_failed(key, rec.url, ErrorKind.INVALID_URL.value, rec.url_error, 0, None)
            return CaptureOutcome(key, rec.url, error=f"Geçersiz bağlantı: {rec.url_error}",
                                  error_kind=ErrorKind.INVALID_URL.value)
        if not force:
            cached = self.store.get_ok_file(key)
            if cached:
                self.counters.inc("cache_hits")
                return CaptureOutcome(key, rec.url, file_path=str(cached), from_cache=True,
                                      duration=time.monotonic() - t0)
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(self._run_with_retries(rec))
            self._inflight[key] = task
            task.add_done_callback(lambda _t, k=key: self._inflight.pop(k, None))
        else:
            self.counters.inc("dedup_joins")
        self._waiters[key] = self._waiters.get(key, 0) + 1
        try:
            out = await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.done() and self._waiters.get(key, 1) <= 1:
                task.cancel()   # bu sonucu bekleyen başka kimse yok
            raise
        finally:
            self._waiters[key] = self._waiters.get(key, 1) - 1
            if self._waiters[key] <= 0:
                self._waiters.pop(key, None)
        return CaptureOutcome(**{**out.__dict__, "duration": time.monotonic() - t0})

    async def _run_with_retries(self, rec: PlaceRecord) -> CaptureOutcome:
        ctrl = self.controller
        attempts, kind, err, timeouts = 0, ErrorKind.TRANSIENT, "", 0
        for attempt in range(self.s.max_attempts):
            attempts += 1
            ticket = await ctrl.acquire()
            t_start = time.monotonic()
            try:
                kind_or_none, err_msg, path = await self._attempt(rec)
            finally:
                await ctrl.release()
            if kind_or_none is None:
                await ctrl.on_success()
                self.counters.inc("captured")
                self.counters.inc("capture_time", time.monotonic() - t_start)
                if attempts > 1:
                    self.counters.inc("recovered_after_retry")
                self.store.mark_ok(rec.place_key, rec.url, path.name, attempts)
                return CaptureOutcome(rec.place_key, rec.url, file_path=str(path), attempts=attempts)
            kind, err = kind_or_none, err_msg or ""
            self.counters.inc(f"attempt_{kind.value}")
            if kind is ErrorKind.RATE_LIMITED:
                cooldown = ctrl.on_rate_limited(ticket)
                self._needs_new_context = True
                logger.warning(f"Rate limit/bot doğrulaması: havuz {cooldown:.0f} sn soğuyor, "
                               f"eş zamanlılık → {int(ctrl.limit)}")
            elif kind is ErrorKind.TRANSIENT:
                ctrl.on_transient(ticket)
                if err.startswith(TIMEOUT_PREFIX):
                    timeouts += 1
                    if timeouts >= self.s.max_timeout_attempts:
                        break
                if attempt + 1 < self.s.max_attempts:
                    await asyncio.sleep(backoff_delay(attempt, self.s.backoff_base, self.s.backoff_cap))
            else:
                ctrl.on_permanent()
                break
        self.counters.inc("failed")
        retry_at = None if kind is ErrorKind.PERMANENT else time.time() + self.s.retry_failed_after_sec
        self.store.mark_failed(rec.place_key, rec.url, kind.value, err, attempts, retry_at)
        logger.error(f"Yakalama başarısız ({kind.value}, {attempts} deneme) — {rec.url[:70]}: {err[:120]}")
        return CaptureOutcome(rec.place_key, rec.url, error=err[:200] or "Bilinmeyen hata",
                              error_kind=kind.value, attempts=attempts)

    async def _attempt(self, rec: PlaceRecord) -> tuple[Optional[ErrorKind], Optional[str], Optional[Path]]:
        ctx = await self._acquire_context()
        page: Optional[Page] = None
        tmp: Optional[Path] = None
        try:
            page = await ctx.new_page()
            resp = await page.goto(rec.nav_url, wait_until="domcontentloaded", timeout=self.s.page_timeout_ms)
            status = resp.status if resp else 0
            await self._handle_consent(page)
            if status == 429 or await self._is_blocked(page):
                return ErrorKind.RATE_LIMITED, f"Rate limit / bot doğrulaması (HTTP {status})", None
            if status in (400, 404, 410):
                return ErrorKind.PERMANENT, f"Sayfa bulunamadı (HTTP {status})", None
            if status >= 500:
                return ErrorKind.TRANSIENT, f"Sunucu hatası (HTTP {status})", None
            try:
                await page.wait_for_selector(config.MAPS_LOADED_SELECTOR, timeout=self.s.maps_wait_ms)
            except PlaywrightTimeout:
                pass
            if self.s.settle_ms > 0:
                await asyncio.sleep(self.s.settle_ms / 1000)
            stem = capture_file_stem(rec.place_key)
            final = self.s.screenshots_dir / f"{stem}.{self._ext}"
            tmp = self.s.screenshots_dir / f"{stem}.{uuid.uuid4().hex[:8]}.tmp.{self._ext}"
            shot_kwargs: dict[str, Any] = dict(
                path=str(tmp), full_page=False, type=self.s.image_format,
                clip={"x": 0, "y": 0, "width": config.VIEWPORT["width"], "height": config.VIEWPORT["height"]},
            )
            if self.s.image_format == "jpeg":
                shot_kwargs["quality"] = self.s.image_quality
            await page.screenshot(**shot_kwargs)
            os.replace(tmp, final)
            tmp = None
            return None, None, final
        except PlaywrightTimeout as e:
            return ErrorKind.TRANSIENT, f"{TIMEOUT_PREFIX}{str(e).splitlines()[0][:120]}", None
        except PlaywrightError as e:
            msg = str(e).splitlines()[0][:160]
            if self._browser is None or not self._browser.is_connected():
                msg = "Tarayıcı bağlantısı koptu: " + msg
            return ErrorKind.TRANSIENT, msg, None
        except OSError as e:
            return ErrorKind.TRANSIENT, f"Dosya hatası: {e}", None
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass
            await self._release_context(ctx)

    async def _handle_consent(self, page: Page) -> None:
        """Yalnızca consent sayfası/formu varsa tıkla (eski sürüm her sayfada 5 selektör deniyordu)."""
        try:
            if "consent." not in page.url and await page.locator('form[action*="consent"]').count() == 0:
                return
            btn = page.locator(", ".join(config.CONSENT_BUTTON_SELECTORS)).first
            if await btn.is_visible():
                await btn.click()
                await page.wait_for_load_state("networkidle", timeout=10_000)
                logger.info("Consent kabul edildi.")
        except Exception:
            pass

    async def _is_blocked(self, page: Page) -> bool:
        url = page.url.lower()
        if "/sorry/" in url or "captcha" in url:
            return True
        try:
            title = (await page.title()).lower()
        except Exception:
            title = ""
        if any(kw in title for kw in ("unusual traffic", "olağan dışı trafik", "robot", "captcha")):
            return True
        try:
            return await page.locator('form[action*="sorry"], #captcha-form').count() > 0
        except Exception:
            return False
