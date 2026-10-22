"""
Proje V1 — Yakalama Kaydı (checkpoint / resume / hata günlüğü)

Her mekan için son ekran görüntüsü sonucu SQLite'ta tutulur:
  • status='ok'     → dosya diskte; TTL dolana kadar tekrar yakalanmaz (önbellek).
  • status='failed' → hata türü, deneme sayısı, son hata ve bir sonraki deneme zamanı.

Süreç kesilse bile tamamlanan her yakalama anında kalıcıdır (WAL + autocommit);
yeniden başlatıldığında yalnızca eksik olanlar işlenir.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
    place_key     TEXT PRIMARY KEY,
    url           TEXT NOT NULL,
    file_name     TEXT,
    status        TEXT NOT NULL,           -- ok | failed
    error         TEXT,
    error_kind    TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    total_attempts INTEGER NOT NULL DEFAULT 0,
    updated_at    REAL NOT NULL,
    next_retry_at REAL
);
CREATE INDEX IF NOT EXISTS idx_captures_status ON captures(status);
"""


class CaptureStore:
    def __init__(self, db_file: Path, screenshots_dir: Path, ttl_hours: float) -> None:
        self.db_file = db_file
        self.dir = screenshots_dir
        self.ttl = ttl_hours * 3600
        self._lock = threading.Lock()
        db_file.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_file, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── Okuma ────────────────────────────────────────────────────────────────
    def get_ok_file(self, place_key: str) -> Optional[Path]:
        """Geçerli (TTL içinde ve diskte var olan) yakalama dosyası."""
        with self._lock:
            row = self._conn.execute(
                "SELECT file_name, updated_at FROM captures WHERE place_key=? AND status='ok'",
                (place_key,),
            ).fetchone()
        if not row or not row[0]:
            return None
        if self.ttl > 0 and time.time() - row[1] > self.ttl:
            return None
        path = self.dir / row[0]
        return path if path.exists() else None

    def get(self, place_key: str) -> Optional[dict]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM captures WHERE place_key=?", (place_key,))
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
        return dict(zip(cols, row)) if row else None

    def failures(self, limit: int = 100) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT place_key, url, error_kind, error, attempts, total_attempts, updated_at, next_retry_at "
                "FROM captures WHERE status='failed' ORDER BY updated_at DESC LIMIT ?", (limit,))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def retry_blocked_until(self, place_key: str) -> Optional[float]:
        """Kalıcı hata ise inf, bekleme süresi dolmamışsa zaman damgası, yoksa None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT error_kind, next_retry_at FROM captures WHERE place_key=? AND status='failed'",
                (place_key,)).fetchone()
        if not row:
            return None
        if row[0] in ("invalid_url", "permanent"):
            return float("inf")
        if row[1] and row[1] > time.time():
            return row[1]
        return None

    def stats(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COALESCE(error_kind,''), COUNT(*) FROM captures GROUP BY 1, 2").fetchall()
        out: dict = {"ok": 0, "failed": 0, "failed_by_kind": {}}
        for status, kind, n in rows:
            out[status] = out.get(status, 0) + n
            if status == "failed":
                out["failed_by_kind"][kind] = n
        return out

    # ── Yazma ────────────────────────────────────────────────────────────────
    def mark_ok(self, place_key: str, url: str, file_name: str, attempts: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO captures (place_key, url, file_name, status, error, error_kind, attempts, "
                "total_attempts, updated_at, next_retry_at) VALUES (?,?,?,'ok',NULL,NULL,?,?,?,NULL) "
                "ON CONFLICT(place_key) DO UPDATE SET url=excluded.url, file_name=excluded.file_name, "
                "status='ok', error=NULL, error_kind=NULL, attempts=excluded.attempts, "
                "total_attempts=captures.total_attempts+excluded.attempts, updated_at=excluded.updated_at, "
                "next_retry_at=NULL",
                (place_key, url, file_name, attempts, attempts, time.time()),
            )

    def mark_failed(self, place_key: str, url: str, kind: str, error: str, attempts: int,
                    next_retry_at: Optional[float]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO captures (place_key, url, file_name, status, error, error_kind, attempts, "
                "total_attempts, updated_at, next_retry_at) VALUES (?,?,NULL,'failed',?,?,?,?,?,?) "
                "ON CONFLICT(place_key) DO UPDATE SET url=excluded.url, status='failed', "
                "error=excluded.error, error_kind=excluded.error_kind, attempts=excluded.attempts, "
                "total_attempts=captures.total_attempts+excluded.attempts, updated_at=excluded.updated_at, "
                "next_retry_at=excluded.next_retry_at "
                "WHERE captures.status <> 'ok' OR captures.file_name IS NULL",
                (place_key, url, error[:500], kind, attempts, attempts, time.time(), next_retry_at),
            )

    # ── Temizlik ─────────────────────────────────────────────────────────────
    def prune_expired(self, dry_run: bool = True) -> list[Path]:
        """TTL'i dolmuş yakalamaların dosyalarını siler (kayıtlar 'süresi doldu' olur)."""
        if self.ttl <= 0:
            return []
        cutoff = time.time() - self.ttl
        with self._lock:
            rows = self._conn.execute(
                "SELECT place_key, file_name FROM captures WHERE status='ok' AND updated_at < ?",
                (cutoff,)).fetchall()
        files = [self.dir / f for _, f in rows if f]
        if not dry_run:
            for p in files:
                p.unlink(missing_ok=True)
            with self._lock:
                self._conn.executemany("DELETE FROM captures WHERE place_key=?", [(k,) for k, _ in rows])
        return files

    def known_files(self) -> set[str]:
        with self._lock:
            return {r[0] for r in self._conn.execute(
                "SELECT file_name FROM captures WHERE file_name IS NOT NULL")}
