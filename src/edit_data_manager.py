"""
Proje V1 — Düzenle Akışı Veri Yönetimi (EditDataManager)
Kaynak Excel (cleanpoi_cevre*.xlsx) ilk açılışta SQLite veritabanına aktarılır
(veri sayfasındaki tüm kolonlar aynen korunur). Sonrasında tüm Sil / Tut işlemleri
yalnızca veritabanında yapılır; kaynak Excel'e hiç dokunulmaz.
Güncel hali istenildiğinde export_excel() ile Excel olarak üretilir.

Sil  → satır veritabanında 'silindi' olarak işaretlenir (geri alınabilir, export'a girmez).
Tut  → satır veritabanında 'tutuldu' olarak işaretlenir (bir daha incelenmez).
Atla → veritabanına dokunulmaz; satır sonraki oturumda yeniden gelir.

SOLID: Single Responsibility — sadece düzenle akışı veri I/O ve satır yönetimi.
"""
import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from config import (
    DATA_DIR,
    EDIT_DB_FILE,
    find_edit_input_file,
    URL_COLUMN,
    NAME_COLUMN,
    KONTROL_COLUMN,
)

logger = logging.getLogger(__name__)

# Olası sütun isim adayları (farklı excel formatlarını desteklemek için)
URL_CANDIDATES = [URL_COLUMN, "maps_url", "url", "maps url", "link"]
NAME_CANDIDATES = [NAME_COLUMN, "isim", "name", "başlık", "title", "identity_google_name"]
KONTROL_CANDIDATES = [KONTROL_COLUMN, "kontrol", "Kontrol"]

TABLE = "mekanlar"
STATUS_COL = "_durum"
STATUS_PENDING = "bekliyor"
STATUS_KEPT = "tutuldu"
STATUS_DELETED = "silindi"


def _q(name: str) -> str:
    """SQL tanımlayıcısını güvenle tırnaklar."""
    return '"' + str(name).replace('"', '""') + '"'


def _find_column(columns: list[str], candidates: list[str]) -> Optional[str]:
    existing = {str(c).strip().upper(): str(c) for c in columns}
    for cand in candidates:
        if cand.strip().upper() in existing:
            return existing[cand.strip().upper()]
    return None


def _find_data_sheet_name(file_path: Path) -> str:
    """URL ve KONTROL sütunlarını içeren veri sayfasını bulur."""
    target_urls = [c.strip().upper() for c in URL_CANDIDATES]
    target_kontrols = [c.strip().upper() for c in KONTROL_CANDIDATES]

    with pd.ExcelFile(file_path, engine="openpyxl") as xl:
        names = xl.sheet_names
        cols_of = {
            n: [str(c).strip().upper() for c in pd.read_excel(xl, sheet_name=n, nrows=0).columns]
            for n in names
        }
        for pref in ("Mekanlar", "mekanlar", "data", "Data"):
            if pref in names and any(u in cols_of[pref] for u in target_urls):
                return pref
        for n in names:
            if any(u in cols_of[n] for u in target_urls) and any(k in cols_of[n] for k in target_kontrols):
                return n
        for n in names:
            if any(u in cols_of[n] for u in target_urls):
                return n
        return names[0]


def _read_source(file_path: Path) -> tuple[pd.DataFrame, str]:
    ext = file_path.suffix.lower()
    if ext in (".xlsx", ".xls"):
        sheet = _find_data_sheet_name(file_path)
        logger.info(f"[EditDataManager] Veri sayfası okunuyor: {sheet}")
        return pd.read_excel(file_path, sheet_name=sheet, engine="openpyxl"), sheet
    if ext == ".csv":
        return pd.read_csv(file_path, encoding="utf-8-sig"), "Mekanlar"
    raise ValueError(f"Desteklenmeyen dosya formatı: {ext}")


def _sql_type(series: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(series) or pd.api.types.is_integer_dtype(series):
        return "INTEGER"
    if pd.api.types.is_float_dtype(series):
        return "REAL"
    return "TEXT"


def _to_sql_value(v):
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return None
    if isinstance(v, (pd.Timestamp, datetime)):
        return v.isoformat(sep=" ")
    if hasattr(v, "item"):          # numpy skaler
        return v.item()
    return v


class EditDataManager:
    """
    Düzenle akışı için SQLite tabanlı veri yöneticisi.
    Public API, eski Excel tabanlı sürümle aynıdır.
    """

    def __init__(self) -> None:
        self._source_path: Path = find_edit_input_file()
        self._lock = threading.RLock()
        self._pending_indices: list[int] = []
        self._action_history: dict[int, str] = {}
        self._url_col: str = ""
        self._kontrol_col: str = ""
        self._name_col: Optional[str] = None
        self._columns: list[str] = []

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(EDIT_DB_FILE, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._ensure_imported()
        self._load_pending()

    # ── Veritabanı kurulumu ──────────────────────────────────────────────────

    def _meta_get(self, key: str) -> Optional[str]:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _ensure_imported(self) -> None:
        self._conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        table_exists = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
        ).fetchone()
        same_source = self._meta_get("source_file") == self._source_path.name

        if table_exists and same_source:
            self._columns = json.loads(self._meta_get("columns") or "[]")
            logger.info(f"[EditDataManager] Mevcut veritabanı kullanılıyor: {EDIT_DB_FILE.name}")
        else:
            self._import_source()

        self._url_col = _find_column(self._columns, URL_CANDIDATES) or ""
        self._kontrol_col = _find_column(self._columns, KONTROL_CANDIDATES) or ""
        self._name_col = _find_column(self._columns, NAME_CANDIDATES)
        if not self._url_col or not self._kontrol_col:
            raise KeyError(
                f"Gerekli sütun (URL/KONTROL) bulunamadı. Mevcut sütunlar: {self._columns}"
            )

    def _import_source(self) -> None:
        df, sheet = _read_source(self._source_path)
        self._columns = [str(c) for c in df.columns]
        df.columns = self._columns
        types = {c: _sql_type(df[c]) for c in self._columns}
        datetime_cols = [c for c in self._columns if pd.api.types.is_datetime64_any_dtype(df[c])]

        col_defs = ", ".join(f"{_q(c)} {types[c]}" for c in self._columns)
        with self._lock, self._conn:
            self._conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
            self._conn.execute(
                f"CREATE TABLE {TABLE} (id INTEGER PRIMARY KEY, {col_defs}, "
                f"{STATUS_COL} TEXT NOT NULL DEFAULT '{STATUS_PENDING}')"
            )
            placeholders = ",".join("?" * (len(self._columns) + 1))
            col_list = "id," + ",".join(_q(c) for c in self._columns)
            rows = (
                (int(idx), *[_to_sql_value(v) for v in rec])
                for idx, rec in zip(df.index, df.itertuples(index=False, name=None))
            )
            self._conn.executemany(
                f"INSERT INTO {TABLE} ({col_list}) VALUES ({placeholders})", rows
            )
            self._conn.execute(f"CREATE INDEX IF NOT EXISTS idx_durum ON {TABLE}({STATUS_COL})")
            for k, v in {
                "source_file": self._source_path.name,
                "sheet_name": sheet,
                "columns": json.dumps(self._columns),
                "datetime_columns": json.dumps(datetime_cols),
                "imported_at": datetime.now().isoformat(timespec="seconds"),
            }.items():
                self._conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v))
        logger.info(
            f"[EditDataManager] '{self._source_path.name}' veritabanına aktarıldı: "
            f"{len(df)} satır, {len(self._columns)} kolon"
        )

    def _load_pending(self) -> None:
        """KONTROL ve URL dolu, henüz karar verilmemiş satırları listeler."""
        k, u = _q(self._kontrol_col), _q(self._url_col)
        rows = self._conn.execute(
            f"SELECT id FROM {TABLE} WHERE {STATUS_COL}=? "
            f"AND TRIM(COALESCE({k},''))<>'' AND TRIM(COALESCE({u},''))<>'' ORDER BY id",
            (STATUS_PENDING,),
        ).fetchall()
        self._pending_indices = [r[0] for r in rows]
        logger.info(f"[EditDataManager] Düzenlenecek satır sayısı: {len(self._pending_indices)}")

    # ── Public API ────────────────────────────────────────────────────────────

    def get_pending_count(self) -> int:
        return len(self._pending_indices)

    def get_total_count(self) -> int:
        """Silinmemiş toplam satır sayısı."""
        with self._lock:
            return self._conn.execute(
                f"SELECT COUNT(*) FROM {TABLE} WHERE {STATUS_COL}<>?", (STATUS_DELETED,)
            ).fetchone()[0]

    def _get_row(self, row_index: int) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                f"SELECT * FROM {TABLE} WHERE id=?", (row_index,)
            ).fetchone()

    def get_kontrol_value(self, row_index: int) -> str:
        row = self._get_row(row_index)
        if row is None:
            return ""
        val = row[self._kontrol_col]
        return str(val).strip() if val is not None else ""

    def get_record(self, position: int) -> Optional[dict]:
        if position >= len(self._pending_indices):
            return None
        idx = self._pending_indices[position]
        row = self._get_row(idx)
        if row is None:
            return None
        name = row[self._name_col] if self._name_col else ""
        return {
            "row_index":     int(idx),
            "url":           str(row[self._url_col]).strip(),
            "name":          "" if name is None else str(name),
            "kontrol_value": self.get_kontrol_value(idx),
            "position":      position,
            "total":         len(self._pending_indices),
        }

    def get_records_from_index(self, start_index: int) -> list[dict]:
        """Belirli bir indeksten itibaren tüm bekleyen kayıtları döndürür."""
        if start_index < 0:
            raise ValueError("Başlangıç indeksi 0 veya daha büyük olmalıdır.")
        start_pos = next(
            (p for p, i in enumerate(self._pending_indices) if int(i) >= start_index),
            len(self._pending_indices),
        )
        records = [
            r for r in (self.get_record(p) for p in range(start_pos, len(self._pending_indices))) if r
        ]
        for position, record in enumerate(records):
            record["position"] = position
            record["total"] = len(records)
        return records

    def _set_status(self, row_index: int, status: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE {TABLE} SET {STATUS_COL}=? WHERE id=?", (status, row_index)
            )

    def _drop_pending(self, row_index: int) -> None:
        if row_index in self._pending_indices:
            self._pending_indices.remove(row_index)

    def delete_row(self, row_index: int) -> None:
        """Satırı veritabanında 'silindi' olarak işaretler (Excel export'a girmez)."""
        self._set_status(row_index, STATUS_DELETED)
        self._action_history[row_index] = "delete"
        self._drop_pending(row_index)
        logger.info(f"[EditDataManager] Satır silindi — row_index={row_index}")

    def keep_row(self, row_index: int) -> None:
        """Satırı veritabanında 'tutuldu' olarak işaretler."""
        self._set_status(row_index, STATUS_KEPT)
        self._action_history[row_index] = "keep"
        self._drop_pending(row_index)
        logger.info(f"[EditDataManager] Satır tutuldu — row_index={row_index}")

    def skip_row(self, row_index: int) -> None:
        """Satırı atlar; veritabanına dokunulmaz."""
        self._action_history[row_index] = "skip"
        self._drop_pending(row_index)
        logger.info(f"[EditDataManager] Satır atlandı — row_index={row_index}")

    def undo_action(self, row_index: int) -> Optional[str]:
        """Son işlemi geri alır; satırı tekrar 'bekliyor' durumuna getirir."""
        action = self._action_history.pop(row_index, None)
        if action in ("delete", "keep"):
            self._set_status(row_index, STATUS_PENDING)
        if row_index not in self._pending_indices:
            self._pending_indices.append(row_index)
            self._pending_indices.sort()
        logger.info(f"[EditDataManager] İşlem geri alındı — row_index={row_index}, action={action}")
        return action

    def flush(self) -> None:
        """Her işlem anında commit edildiği için bekleyen bir şey yoktur."""
        with self._lock:
            self._conn.commit()


def export_excel(out_path: Path) -> int:
    """
    Veritabanındaki güncel hali (silinenler hariç) orijinal kolonlarla Excel'e yazar.
    Dönen değer: yazılan satır sayısı.
    """
    if not EDIT_DB_FILE.exists():
        raise FileNotFoundError("Veritabanı henüz oluşturulmadı. Önce oturumu başlatın.")
    conn = sqlite3.connect(EDIT_DB_FILE)
    try:
        def get(k):
            row = conn.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
            return row[0] if row else None

        columns = json.loads(get("columns") or "[]")
        if not columns:
            raise FileNotFoundError("Veritabanı henüz oluşturulmadı. Önce oturumu başlatın.")
        datetime_cols = json.loads(get("datetime_columns") or "[]")
        sheet = get("sheet_name") or "Mekanlar"
        cols_sql = ",".join(_q(c) for c in columns)
        df = pd.read_sql_query(
            f"SELECT {cols_sql} FROM {TABLE} WHERE {STATUS_COL}<>? ORDER BY id",
            conn, params=(STATUS_DELETED,),
        )
    finally:
        conn.close()

    for c in datetime_cols:
        df[c] = pd.to_datetime(df[c], errors="coerce")
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name=sheet, index=False)
    return len(df)
