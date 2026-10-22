"""
Proje V1 — Düzenle Akışı Veri Yönetimi (EditDataManager)

Kaynak Excel ilk açılışta SQLite veritabanına aktarılır (veri sayfasındaki tüm
kolonlar aynen korunur). Sonrasında tüm Sil / Tut işlemleri yalnızca
veritabanında yapılır; kaynak Excel'e hiç dokunulmaz. Güncel hal istenildiğinde
export_excel() ile Excel olarak üretilir.

Sil  → satır 'silindi' olarak işaretlenir (geri alınabilir, export'a girmez).
Tut  → satır 'tutuldu' olarak işaretlenir (bir daha incelenmez).
Atla → veritabanına dokunulmaz; satır sonraki oturumda yeniden gelir.

Kolonlar (Maps bağlantısı, kontrol/durum, isim, place_id) sabit isimle değil
excel_schema ile içerikten tespit edilir; eşleştirme veritabanına kaydedilir.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any, Optional

from config import DATA_DIR, edit_db_path, find_edit_input_file
from data_model import ColumnRole, PlaceRecord, SheetSchema
from excel_schema import detect_columns, get_schema, iter_data_rows

logger = logging.getLogger(__name__)

TABLE = "mekanlar"
STATUS_COL = "_durum"
STATUS_PENDING = "bekliyor"
STATUS_KEPT = "tutuldu"
STATUS_DELETED = "silindi"
IMPORT_BATCH = 5000


def _q(name: str) -> str:
    """SQL tanımlayıcısını güvenle tırnaklar."""
    return '"' + str(name).replace('"', '""') + '"'


def _to_sql_value(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, float) and v != v:
        return None
    if isinstance(v, datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, (date, dtime)):
        return v.isoformat()
    if isinstance(v, str) and v == "":
        return None
    if hasattr(v, "item"):          # numpy skaler
        return v.item()
    return v


class EditDataManager:
    """Düzenle akışı için SQLite tabanlı veri yöneticisi (public API eski sürümle aynı)."""

    def __init__(self, source_file: Optional[str] = None) -> None:
        self._source_path: Path = find_edit_input_file(source_file)
        self._lock = threading.RLock()
        self._pending_indices: list[int] = []
        self._action_history: dict[int, str] = {}
        self._columns: list[str] = []
        self.schema: Optional[SheetSchema] = None

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._db_file: Path = edit_db_path(self._source_path)
        self._conn = sqlite3.connect(self._db_file, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_imported()
        self._load_pending()

    @property
    def source_path(self) -> Path:
        return self._source_path

    # ── Veritabanı kurulumu ──────────────────────────────────────────────────
    def _meta_get(self, key: str) -> Optional[str]:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _meta_set(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))

    def _ensure_imported(self) -> None:
        self._conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        table_exists = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
        ).fetchone()
        same_source = self._meta_get("source_file") == self._source_path.name

        if table_exists and same_source:
            self._columns = json.loads(self._meta_get("columns") or "[]")
            logger.info(f"[EditDataManager] Mevcut veritabanı kullanılıyor: {self._db_file.name}")
            stored = self._meta_get("schema")
            if stored:
                self.schema = SheetSchema.from_dict(json.loads(stored))
            else:
                # Eski sürümle oluşturulmuş veritabanı: kolonları kayıtlı veriden tespit et
                self.schema = self._detect_from_db()
                self._meta_set("schema", json.dumps(self.schema.to_dict(), ensure_ascii=False))
        else:
            self._import_source()
        self.schema.require(ColumnRole.MAPS_URL, ColumnRole.STATUS)
        logger.info(
            "[EditDataManager] Kolonlar: "
            + ", ".join(f"{k}='{m.column}' ({m.method}, {m.confidence:.2f})" for k, m in self.schema.matches.items())
        )

    def _detect_from_db(self) -> SheetSchema:
        total = self._conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
        step = max(1, total // 400)
        cols_sql = ",".join(_q(c) for c in self._columns)
        rows = [tuple(r) for r in self._conn.execute(
            f"SELECT {cols_sql} FROM {TABLE} WHERE id % ? = 0 ORDER BY id LIMIT 400", (step,))]
        matches, warnings, errors, cands = detect_columns(self._columns, rows)
        return SheetSchema(file=self._source_path.name, sheet=self._meta_get("sheet_name"), header_row=1,
                           columns=self._columns, raw_headers=list(self._columns), row_count=total,
                           matches=matches, warnings=warnings, errors=errors, candidates=cands)

    def _import_source(self) -> None:
        """Kaynağı akış halinde (düşük RAM) içe aktarır; tablo değişimi atomiktir."""
        schema = get_schema(self._source_path, ColumnRole.MAPS_URL, ColumnRole.STATUS)
        cols = schema.columns
        n = len(cols)
        kinds = [set() for _ in range(n)]
        tmp_cols = ", ".join(f"c{i}" for i in range(n))
        rows_written = 0
        with self._lock:
            self._conn.execute("DROP TABLE IF EXISTS _import")
            self._conn.execute(f"CREATE TABLE _import (id INTEGER PRIMARY KEY, {tmp_cols})")
            insert = f"INSERT INTO _import (id, {tmp_cols}) VALUES ({','.join('?' * (n + 1))})"
            batch: list[tuple] = []
            for idx, values in iter_data_rows(self._source_path, schema):
                if all(v is None or (isinstance(v, str) and not v.strip()) for v in values):
                    continue
                conv = []
                for i, v in enumerate(values):
                    if v is not None:
                        kinds[i].add(type(v))
                    conv.append(_to_sql_value(v))
                batch.append((idx, *conv))
                if len(batch) >= IMPORT_BATCH:
                    self._conn.executemany(insert, batch)
                    rows_written += len(batch)
                    batch.clear()
            if batch:
                self._conn.executemany(insert, batch)
                rows_written += len(batch)

            def decl(k: set) -> str:
                if k and k <= {int, bool}:
                    return "INTEGER"
                if k and k <= {int, float, bool}:
                    return "REAL"
                return "TEXT"

            datetime_cols = [c for c, k in zip(cols, kinds) if k and k <= {datetime, date}]
            col_defs = ", ".join(f"{_q(c)} {decl(k)}" for c, k in zip(cols, kinds))
            with self._conn:
                self._conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
                self._conn.execute(
                    f"CREATE TABLE {TABLE} (id INTEGER PRIMARY KEY, {col_defs}, "
                    f"{STATUS_COL} TEXT NOT NULL DEFAULT '{STATUS_PENDING}')"
                )
                self._conn.execute(
                    f"INSERT INTO {TABLE} (id, {','.join(_q(c) for c in cols)}) SELECT id, {tmp_cols} FROM _import"
                )
                self._conn.execute("DROP TABLE _import")
                self._conn.execute(f"CREATE INDEX IF NOT EXISTS idx_durum ON {TABLE}({STATUS_COL})")
                for k, v in {
                    "source_file": self._source_path.name,
                    "sheet_name": schema.sheet or "Mekanlar",
                    "columns": json.dumps(cols, ensure_ascii=False),
                    "datetime_columns": json.dumps(datetime_cols, ensure_ascii=False),
                    "imported_at": datetime.now().isoformat(timespec="seconds"),
                    "schema": json.dumps(schema.to_dict(), ensure_ascii=False),
                }.items():
                    self._conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v))
        self._columns = cols
        self.schema = schema
        logger.info(
            f"[EditDataManager] '{self._source_path.name}' veritabanına aktarıldı: "
            f"{rows_written} satır, {n} kolon (sayfa: {schema.sheet})"
        )

    # ── Kolon yardımcıları ───────────────────────────────────────────────────
    @property
    def _url_col(self) -> str:
        return self.schema.column(ColumnRole.MAPS_URL)  # type: ignore[return-value]

    @property
    def _kontrol_col(self) -> str:
        return self.schema.column(ColumnRole.STATUS)  # type: ignore[return-value]

    @property
    def _name_col(self) -> Optional[str]:
        return self.schema.column(ColumnRole.NAME)

    def _select_fields(self) -> str:
        parts = [f"{_q(self._url_col)} AS u", f"{_q(self._kontrol_col)} AS k"]
        parts.append(f"{_q(self._name_col)} AS n" if self._name_col else "NULL AS n")
        pid = self.schema.column(ColumnRole.PLACE_ID)
        parts.append(f"{_q(pid)} AS p" if pid else "NULL AS p")
        return ", ".join(parts)

    def _pending_where(self) -> str:
        k, u = _q(self._kontrol_col), _q(self._url_col)
        return (f"{STATUS_COL}='{STATUS_PENDING}' "
                f"AND TRIM(COALESCE({k},''))<>'' AND TRIM(COALESCE({u},''))<>''")

    def _load_pending(self) -> None:
        """Kontrol ve URL dolu, henüz karar verilmemiş satırları listeler."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id FROM {TABLE} WHERE {self._pending_where()} ORDER BY id").fetchall()
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
            return self._conn.execute(f"SELECT * FROM {TABLE} WHERE id=?", (row_index,)).fetchone()

    def get_kontrol_value(self, row_index: int) -> str:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_q(self._kontrol_col)} FROM {TABLE} WHERE id=?", (row_index,)).fetchone()
        if row is None or row[0] is None:
            return ""
        return str(row[0]).strip()

    @staticmethod
    def _to_record(row: sqlite3.Row) -> PlaceRecord:
        return PlaceRecord.build(row["id"], row["u"], row["n"], row["k"], row["p"])

    def get_record(self, position: int) -> Optional[dict]:
        if position >= len(self._pending_indices):
            return None
        idx = self._pending_indices[position]
        with self._lock:
            row = self._conn.execute(
                f"SELECT id, {self._select_fields()} FROM {TABLE} WHERE id=?", (idx,)).fetchone()
        if row is None:
            return None
        rec = self._to_record(row)
        rec.position, rec.total = position, len(self._pending_indices)
        return rec.to_dict()

    def get_place_records(self, start_index: int = 0) -> list[PlaceRecord]:
        """Belirli bir indeksten itibaren bekleyen kayıtlar — TEK sorgu (eskisi 2N sorgu)."""
        if start_index < 0:
            raise ValueError("Başlangıç indeksi 0 veya daha büyük olmalıdır.")
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id, {self._select_fields()} FROM {TABLE} "
                f"WHERE {self._pending_where()} AND id >= ? ORDER BY id", (start_index,)).fetchall()
        records = [self._to_record(r) for r in rows]
        for position, rec in enumerate(records):
            rec.position, rec.total = position, len(records)
        return records

    def get_records_from_index(self, start_index: int) -> list[dict]:
        return [r.to_dict() for r in self.get_place_records(start_index)]

    def _set_status(self, row_index: int, status: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(f"UPDATE {TABLE} SET {STATUS_COL}=? WHERE id=?", (status, row_index))

    def _drop_pending(self, row_index: int) -> None:
        with self._lock:
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
        with self._lock:
            if row_index not in self._pending_indices:
                self._pending_indices.append(row_index)
                self._pending_indices.sort()
        logger.info(f"[EditDataManager] İşlem geri alındı — row_index={row_index}, action={action}")
        return action

    def flush(self) -> None:
        """Her işlem anında commit edildiği için bekleyen bir şey yoktur."""
        with self._lock:
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def export_excel(out_path: Path, source_file: Optional[str] = None) -> int:
    """
    Veritabanındaki güncel hali (silinenler hariç) orijinal kolonlarla Excel'e yazar.
    Akış halinde (write_only) yazılır; büyük dosyalarda düşük RAM.
    Dönen değer: yazılan satır sayısı.
    """
    from openpyxl import Workbook
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

    db_file = edit_db_path(find_edit_input_file(source_file))
    if not db_file.exists():
        raise FileNotFoundError("Veritabanı henüz oluşturulmadı. Önce oturumu başlatın.")
    conn = sqlite3.connect(db_file)
    try:
        def get(k: str) -> Optional[str]:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
            return row[0] if row else None

        columns = json.loads(get("columns") or "[]")
        if not columns:
            raise FileNotFoundError("Veritabanı henüz oluşturulmadı. Önce oturumu başlatın.")
        dt_idx = {columns.index(c) for c in json.loads(get("datetime_columns") or "[]") if c in columns}
        sheet = get("sheet_name") or "Mekanlar"

        wb = Workbook(write_only=True)
        ws = wb.create_sheet(title=sheet[:31])
        ws.append(columns)
        cur = conn.execute(
            f"SELECT {','.join(_q(c) for c in columns)} FROM {TABLE} WHERE {STATUS_COL}<>? ORDER BY id",
            (STATUS_DELETED,))
        count = 0
        while True:
            chunk = cur.fetchmany(5000)
            if not chunk:
                break
            for row in chunk:
                out = []
                for i, v in enumerate(row):
                    if i in dt_idx and isinstance(v, str):
                        try:
                            v = datetime.fromisoformat(v)
                        except ValueError:
                            pass
                    elif isinstance(v, str):
                        v = ILLEGAL_CHARACTERS_RE.sub("", v)
                    out.append(v)
                ws.append(out)
                count += 1
        wb.save(out_path)
    finally:
        conn.close()
    return count
