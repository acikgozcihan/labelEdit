"""
Proje V1 — Session & Browser Yönetimi

Geriye dönük uyumlu cephe (facade): eski SessionManager API'si (start / stop /
take_screenshot / refresh_session) korunur, ancak iş artık paylaşılan tek bir
CaptureEngine'e devredilir. Etiketleme ve düzenle akışları aynı tarayıcıyı ve
AYNI rate limit kontrolünü paylaşır (aynı hedef servise iki ayrı sayaç ile
yüklenilmez). Motor, referans sayısı sıfıra inince kapanır.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from capture_engine import CaptureEngine, CaptureSettings
from data_model import PlaceRecord

logger = logging.getLogger(__name__)

_engine_lock = threading.Lock()
_engine: Optional[CaptureEngine] = None
_engine_refs = 0


def acquire_engine(settings: Optional[CaptureSettings] = None) -> CaptureEngine:
    global _engine, _engine_refs
    with _engine_lock:
        if _engine is None or not _engine.running:
            _engine = CaptureEngine(settings)
            _engine.start()
        _engine_refs += 1
        return _engine


def release_engine() -> None:
    global _engine, _engine_refs
    with _engine_lock:
        _engine_refs = max(0, _engine_refs - 1)
        if _engine_refs == 0 and _engine is not None:
            _engine.stop()
            _engine = None


def current_engine() -> Optional[CaptureEngine]:
    return _engine


class SessionManager:
    def __init__(self) -> None:
        self.engine: Optional[CaptureEngine] = None

    def start(self) -> None:
        if self.engine is None:
            self.engine = acquire_engine()
        logger.info("SessionManager başlatıldı.")

    def stop(self) -> None:
        if self.engine is not None:
            self.engine = None
            release_engine()
        logger.info("SessionManager durduruldu.")

    def take_screenshot(self, record: dict, max_retries: int = 3) -> dict:
        """Eski senkron API: {file_path, error, ...record} döndürür."""
        if self.engine is None:
            raise RuntimeError("SessionManager başlatılmamış.")
        rec = PlaceRecord.from_dict(record)
        out = self.engine.capture_sync(rec)
        return {**record, "file_path": out.file_path, "error": out.error}

    def refresh_session(self) -> None:
        if self.engine is not None:
            self.engine._needs_new_context = True
