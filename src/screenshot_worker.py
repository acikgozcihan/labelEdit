"""
Proje V1 — Screenshot Worker (Thread-pool + asyncio.Queue Buffer)
Playwright işlemleri ThreadPoolExecutor'da (sync), buffer asyncio.Queue'de.
SOLID: Single Responsibility — sadece screenshot alma ve buffer yönetimi.
"""
import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from config import BUFFER_SIZE
from session_manager import SessionManager

logger = logging.getLogger(__name__)


@dataclass
class ScreenshotItem:
    """Buffer'daki tek bir screenshot kaydı."""
    row_index: int
    position: int
    url: str
    name: str
    file_path: str
    total: int
    taken_at: float = field(default_factory=time.time)
    error: Optional[str] = None


class ScreenshotWorker:
    """
    Buffer sistemi:
    - asyncio.Queue(BUFFER_SIZE) → max 10 item tutar
    - ThreadPoolExecutor → Playwright sync işlemlerini ayrı thread'de çalıştırır
    - _producer_task → asyncio task, kuyruk dolu değilse yeni screenshot tetikler
    """

    def __init__(self, session_manager: SessionManager) -> None:
        self._session = session_manager
        self._queue: asyncio.Queue[ScreenshotItem] = asyncio.Queue(maxsize=BUFFER_SIZE)
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="screenshot")
        self._pending_records: list[dict] = []
        self._current_fetch_index: int = 0
        self._producer_task: Optional[asyncio.Task] = None
        self._running: bool = False
        self._served_items: list[ScreenshotItem] = []
        self._stats = {"taken": 0, "errors": 0}

    # ── Başlatma / Durdurma ──────────────────────────────────────────────────

    async def start(self, records: list[dict]) -> None:
        self._pending_records = records
        self._current_fetch_index = 0
        self._running = True
        self._producer_task = asyncio.create_task(
            self._producer_loop(), name="screenshot-producer"
        )
        logger.info(f"ScreenshotWorker başlatıldı. Toplam: {len(records)}")

    async def stop(self) -> None:
        self._running = False
        if self._producer_task and not self._producer_task.done():
            self._producer_task.cancel()
            try:
                await self._producer_task
            except asyncio.CancelledError:
                pass
        # Kuyruktaki dosyaları temizle
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                _cleanup_file(item.file_path)
            except asyncio.QueueEmpty:
                break
        for item in self._served_items:
            _cleanup_file(item.file_path)
        self._served_items.clear()
        self._executor.shutdown(wait=False)
        logger.info(f"ScreenshotWorker durduruldu. Stats: {self._stats}")

    # ── Public API ───────────────────────────────────────────────────────────

    async def get_next(self) -> Optional[ScreenshotItem]:
        """Sıradaki screenshot'ı döndürür (buffer'dan anlık)."""
        try:
            item = await asyncio.wait_for(self._queue.get(), timeout=60.0)
            self._queue.task_done()
            self._served_items.append(item)
            return item
        except asyncio.TimeoutError:
            logger.error("get_next timeout — 60s içinde item gelmedi.")
            return None

    def queue_size(self) -> int:
        return self._queue.qsize()

    def is_empty(self) -> bool:
        return self._queue.empty() and self._current_fetch_index >= len(self._pending_records)

    def get_stats(self) -> dict:
        return {**self._stats, "queue_size": self._queue.qsize(), "fetch_index": self._current_fetch_index}

    # ── Producer Loop ────────────────────────────────────────────────────────

    async def _producer_loop(self) -> None:
        """Sürekli çalışır: kuyrukta yer açıldığında yeni screenshot alır."""
        logger.info("Producer loop başladı.")
        loop = asyncio.get_event_loop()

        while self._running and self._current_fetch_index < len(self._pending_records):
            if self._queue.full():
                await asyncio.sleep(0.3)
                continue

            record = self._pending_records[self._current_fetch_index]
            self._current_fetch_index += 1

            # Playwright sync işlemini thread'de çalıştır
            try:
                result = await loop.run_in_executor(
                    self._executor,
                    self._session.take_screenshot,
                    record,
                )
                item = ScreenshotItem(
                    row_index=result["row_index"],
                    position=result["position"],
                    url=result["url"],
                    name=result["name"],
                    file_path=result.get("file_path", ""),
                    total=result["total"],
                    error=result.get("error"),
                )
                if item.error:
                    self._stats["errors"] += 1
                else:
                    self._stats["taken"] += 1

                await self._queue.put(item)
                logger.info(
                    f"[Buffer] pos:{item.position}/{item.total} | "
                    f"qsize:{self._queue.qsize()} | err:{item.error is not None}"
                )
            except Exception as e:
                logger.error(f"Producer hatası — {record.get('url','?')[:50]}: {e}")
                self._stats["errors"] += 1
                # Hata item'ı kuyruğa ekle (kullanıcı atlanmadan devam etsin)
                await self._queue.put(ScreenshotItem(
                    row_index=record["row_index"],
                    position=record["position"],
                    url=record["url"],
                    name=record["name"],
                    file_path="",
                    total=record["total"],
                    error=str(e)[:200],
                ))

        logger.info("Producer loop tamamlandı.")


def _cleanup_file(file_path: str) -> None:
    if file_path:
        p = Path(file_path)
        if p.exists():
            p.unlink(missing_ok=True)
