"""
Proje V1 — Dinamik Excel Analizi ve Otomatik Kolon Tespiti

input/ klasöründeki dosyaların sayfalarını, başlık satırını ve kolon rollerini
(Google Maps bağlantısı, kontrol/durum, mekan adı, place_id) sabit kolon
isimlerine bağlı kalmadan tespit eder.

Yöntem:
  1. Başlık normalizasyonu  — Türkçe karakter katlama, camelCase/snake_case ayrıştırma,
                               küçük harf, noktalama temizliği.
  2. Çok dilli eş anlamlılar — tr/en/de/fr/es/it anahtar kelimeleri + bulanık eşleşme (difflib).
  3. İçerik analizi          — örnek hücrelerde Maps URL örüntüsü, place_id örüntüsü,
                               tekil değer oranı, ortalama uzunluk, sayısallık.
  4. Güven eşikleri          — eşik altı veya birbirine çok yakın adaylarda yanlış seçim
                               yapmak yerine açıklayıcı uyarı/hata üretilir.
  5. Elle eşleştirme         — input/kolon_eslesme.json (isteğe bağlı) her şeyi ezer.

Sonuç ortak veri modelindeki SheetSchema'dır.
"""
from __future__ import annotations

import csv
import difflib
import hashlib
import json
import logging
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any, Iterator, Optional

from config import (
    COLUMN_OVERRIDES_FILE,
    SCHEMA_CACHE_FILE,
    SCHEMA_HEADER_SCAN_ROWS,
    SCHEMA_SAMPLE_ROWS,
    list_input_files,
)
from data_model import (
    ColumnDetectionError,
    ColumnMatch,
    ColumnRole,
    PLACE_ID_RE,
    SheetSchema,
    clean_cell,
    looks_like_maps_url,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 3

# ── Normalizasyon ────────────────────────────────────────────────────────────
_TR_FOLD = str.maketrans({
    "ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
    "ü": "u", "Ü": "u", "ö": "o", "Ö": "o", "ç": "c", "Ç": "c",
    "ß": "ss",
})


def normalize_header(value: Any) -> str:
    """'Google Maps URL', 'google_maps_url', 'GoogleMapsURL', 'GOOGLE-MAPS url' → 'google maps url'."""
    if value is None:
        return ""
    s = str(value).translate(_TR_FOLD)
    s = re.sub(r"\.\d+$", "", s)                         # pandas tekrar eki: "Kolon.1"
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)        # camelCase
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)      # URLLink → URL Link
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.casefold()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def unique_columns(raw_headers: list[Any]) -> list[str]:
    """pandas.read_excel ile aynı adlandırma: boş → 'Unnamed: i', tekrar → 'Ad.1'."""
    out: list[str] = []
    seen: dict[str, int] = {}
    for i, h in enumerate(raw_headers):
        name = f"Unnamed: {i}" if h is None or (isinstance(h, str) and h == "") else str(h)
        base = name
        while name in seen:
            seen[base] += 1
            name = f"{base}.{seen[base]}"
        seen.setdefault(name, 0)
        out.append(name)
    return out


# ── Çok dilli anahtar kelime tabloları (normalize edilmiş) ───────────────────
_URL_MAP_STRONG = {"maps", "map", "gmaps", "gmap", "googlemaps", "harita", "haritasi", "haritalar",
                   "karte", "mapa", "carte", "mappa", "kartta"}
_URL_MAP_WEAK = {"konum", "location", "place", "yer", "lokasyon", "standort", "ubicacion"}
_URL_LINK = {"url", "uri", "link", "linki", "linkler", "links", "baglanti", "baglantisi", "href",
             "lien", "enlace", "verknupfung", "collegamento", "adresi", "web"}

_STATUS_WEIGHTS = {
    "kontrol": 1.0, "kontrolu": 1.0, "control": 1.0, "controle": 1.0, "kontrolle": 1.0,
    "check": 0.9, "checked": 0.9, "status": 0.95, "statu": 0.95, "statusu": 0.95,
    "durum": 0.95, "durumu": 0.95, "state": 0.8, "estado": 0.9, "statut": 0.9,
    "zustand": 0.9, "stato": 0.9, "review": 0.85, "inceleme": 0.85, "incelendi": 0.9,
    "onay": 0.8, "onaylandi": 0.85, "approved": 0.8, "verified": 0.8, "dogrulama": 0.8,
    "flag": 0.75, "islendi": 0.9, "islenen": 0.85, "processed": 0.9, "done": 0.8,
    "tamamlandi": 0.85, "yapildi": 0.75, "isaret": 0.7, "qc": 0.8, "prufung": 0.9,
    "verificacion": 0.85, "verification": 0.85,
}
_STATUS_FILLER = {"mi", "mu", "edildi", "edilen", "is", "the", "de", "da", "ok", "bilgisi", "kolonu"}
_STATUS_VALUE_VOCAB = {
    "ok", "tamam", "done", "evet", "hayir", "yes", "no", "x", "true", "false", "1", "0",
    "islendi", "kontrol edildi", "bekliyor", "tamamlandi", "onay", "onaylandi", "red", "silindi",
    "tutuldu", "pending", "checked", "v", "y", "n", "var", "yok",
}

_NAME_WEIGHTS = {
    "isim": 1.0, "ismi": 1.0, "isimi": 1.0, "ad": 0.85, "adi": 0.9, "name": 1.0, "names": 0.9,
    "title": 0.8, "baslik": 0.8, "unvan": 0.85, "unvani": 0.85, "nombre": 1.0, "nom": 0.9,
    "firma": 0.75, "isletme": 0.8, "mekan": 0.75, "business": 0.7, "magaza": 0.6, "sirket": 0.6,
    "bezeichnung": 0.9, "nome": 0.9,
}
_NAME_NEGATIVE = {"kategori", "category", "il", "ilce", "mahalle", "adres", "address", "url", "link",
                  "keyword", "keywordler", "ulke", "sehir", "city", "type", "tip", "tur"}

_PLACE_ID_JOINED = {"placeid", "googleplaceid", "pid", "gid", "googleid", "cid", "ftid", "featureid"}


def _fuzzy_weight(token: str, table: dict[str, float], cutoff: float = 0.84) -> float:
    if len(token) < 4:
        return 0.0
    hit = difflib.get_close_matches(token, table.keys(), n=1, cutoff=cutoff)
    return table[hit[0]] * 0.85 if hit else 0.0


def url_header_score(norm: str) -> float:
    tokens = norm.split()
    joined = norm.replace(" ", "")
    has_map = any(t in _URL_MAP_STRONG for t in tokens) or "googlemap" in joined or "gmap" in joined
    has_weak = any(t in _URL_MAP_WEAK for t in tokens)
    has_link = any(t in _URL_LINK for t in tokens)
    if has_map and has_link:
        return 1.0
    if has_map:
        return 0.75
    if has_link and has_weak:
        return 0.7
    if has_link:
        return 0.55
    if has_weak:
        return 0.35
    return 0.0


def status_header_score(norm: str) -> float:
    tokens = norm.split()
    if not tokens:
        return 0.0
    weights = [_STATUS_WEIGHTS.get(t, 0.0) for t in tokens]
    best = max(weights)
    if best == 0.0:
        best = max(_fuzzy_weight(t, _STATUS_WEIGHTS) for t in tokens)
        if best == 0.0:
            return 0.0
    other = [t for t, w in zip(tokens, weights) if w == 0.0 and t not in _STATUS_FILLER]
    return best if not other else best * 0.6


def name_header_score(norm: str) -> float:
    tokens = norm.split()
    if not tokens:
        return 0.0
    best = max(_NAME_WEIGHTS.get(t, 0.0) for t in tokens)
    if best == 0.0:
        best = max(_fuzzy_weight(t, _NAME_WEIGHTS, cutoff=0.88) for t in tokens)
    if best == 0.0:
        return 0.0
    if any(t in _NAME_NEGATIVE for t in tokens):
        best *= 0.3
    return best if len(tokens) == 1 else best * 0.85


def place_id_header_score(norm: str) -> float:
    tokens = norm.split()
    joined = norm.replace(" ", "")
    if joined in _PLACE_ID_JOINED or ("place" in tokens and "id" in tokens):
        return 1.0
    return 0.0


# ── İçerik profili ───────────────────────────────────────────────────────────
@dataclass
class ColumnProfile:
    index: int
    name: str
    norm: str
    sampled: int
    nonempty: int = 0
    maps: int = 0
    urls: int = 0
    numeric: int = 0
    place_ids: int = 0
    dates: int = 0
    vocab: int = 0
    total_len: int = 0
    distinct: set = field(default_factory=set)
    examples: list = field(default_factory=list)

    @property
    def fill(self) -> float:
        return self.nonempty / self.sampled if self.sampled else 0.0

    def ratio(self, n: int) -> float:
        return n / self.nonempty if self.nonempty else 0.0

    @property
    def distinct_ratio(self) -> float:
        return len(self.distinct) / self.nonempty if self.nonempty else 0.0

    @property
    def avg_len(self) -> float:
        return self.total_len / self.nonempty if self.nonempty else 0.0


_NUMERIC_RE = re.compile(r"^[+-]?\d+(?:[.,]\d+)?$")


def profile_columns(columns: list[str], rows: list[tuple]) -> list[ColumnProfile]:
    profiles = [ColumnProfile(i, c, normalize_header(c), len(rows)) for i, c in enumerate(columns)]
    for row in rows:
        for p in profiles:
            v = row[p.index] if p.index < len(row) else None
            s = clean_cell(v)
            if not s:
                continue
            p.nonempty += 1
            p.total_len += len(s)
            if len(p.distinct) < 2000:
                p.distinct.add(s.casefold())
            if len(p.examples) < 3:
                p.examples.append(s[:80])
            if isinstance(v, (datetime, date, dtime)):
                p.dates += 1
                continue
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                p.numeric += 1
            elif _NUMERIC_RE.match(s):
                p.numeric += 1
            if looks_like_maps_url(s):
                p.maps += 1
            if s.lower().startswith(("http://", "https://", "www.")):
                p.urls += 1
            if PLACE_ID_RE.match(s):
                p.place_ids += 1
            if normalize_header(s) in _STATUS_VALUE_VOCAB or s in ("✓", "✔", "✗"):
                p.vocab += 1
    return profiles


# ── Rol skorlama ─────────────────────────────────────────────────────────────
def _score_maps_url(p: ColumnProfile) -> tuple[float, str, str]:
    hs = url_header_score(p.norm)
    if p.nonempty == 0:
        return (0.5 * hs, "header", f"başlık skoru {hs:.2f}, örnek satırlar boş")
    mr = p.ratio(p.maps)
    if mr < 0.5:
        return (0.0, "content", f"Maps bağlantı oranı {mr:.0%}")
    score = 0.7 * mr + 0.3 * hs
    method = "header+content" if hs > 0 else "content"
    return (score, method, f"{p.maps}/{p.nonempty} hücre Maps bağlantısı, başlık skoru {hs:.2f}")


def _status_content_factor(p: ColumnProfile) -> float:
    if p.nonempty == 0:
        return 1.0   # durum kolonları çoğu zaman büyük ölçüde boştur → nötr
    if p.ratio(p.maps) > 0.3 or p.ratio(p.urls) > 0.3 or p.ratio(p.place_ids) > 0.3:
        return 0.2
    if p.avg_len > 60:
        return 0.3
    if p.ratio(p.numeric) > 0.8 and len(p.distinct) > 3:
        return 0.5
    if p.nonempty >= 20 and p.distinct_ratio > 0.9:
        return 0.6
    return 1.0


def _score_status(p: ColumnProfile) -> tuple[float, str, str]:
    hs = status_header_score(p.norm)
    if hs > 0:
        f = _status_content_factor(p)
        return (hs * f, "header+content" if p.nonempty else "header",
                f"başlık skoru {hs:.2f}, içerik uygunluğu {f:.1f}, dolu {p.nonempty}/{p.sampled}")
    vr = p.ratio(p.vocab)
    if p.nonempty >= 5 and vr >= 0.8 and len(p.distinct) <= 6:
        return (0.56 + 0.1 * (vr - 0.8) / 0.2, "content",
                f"değerlerin {vr:.0%}'i durum sözlüğünde ({sorted(p.distinct)[:5]})")
    return (0.0, "", "")


def _score_name(p: ColumnProfile) -> tuple[float, str, str]:
    hs = name_header_score(p.norm)
    if p.nonempty:
        text_ratio = 1.0 - max(p.ratio(p.numeric), p.ratio(p.urls), p.ratio(p.dates))
        len_ok = 1.0 if 2 <= p.avg_len <= 100 else 0.3
        cs = max(0.0, text_ratio) * min(1.0, p.distinct_ratio / 0.6) * len_ok
    else:
        cs = 0.5
    if hs == 0:
        return (0.45 * cs, "content", f"içerik skoru {cs:.2f}")
    return (0.6 * hs + 0.4 * cs, "header+content", f"başlık {hs:.2f}, içerik {cs:.2f}")


def _score_place_id(p: ColumnProfile) -> tuple[float, str, str]:
    hs = place_id_header_score(p.norm)
    pr = p.ratio(p.place_ids)
    if pr >= 0.6:
        return (0.5 + 0.5 * pr * (0.6 + 0.4 * hs), "content", f"{p.place_ids}/{p.nonempty} place_id örüntüsü")
    if hs == 1.0 and p.nonempty == 0:
        return (0.55, "header", "başlık eşleşti, örnek boş")
    return (0.0, "", "")


_ROLE_RULES = [
    # rol, skorlayıcı, eşik, zorunlu-belirsizlik-marjı
    (ColumnRole.MAPS_URL, _score_maps_url, 0.35, None),
    (ColumnRole.PLACE_ID, _score_place_id, 0.55, None),
    (ColumnRole.STATUS, _score_status, 0.55, 0.12),
    (ColumnRole.NAME, _score_name, 0.5, None),
]


def detect_columns(columns: list[str], rows: list[tuple],
                   overrides: Optional[dict] = None) -> tuple[dict[str, ColumnMatch], list[str], list[str], dict]:
    """Kolon rollerini tespit eder. Dönüş: (eşleşmeler, uyarılar, hatalar, adaylar)."""
    profiles = profile_columns(columns, rows)
    matches: dict[str, ColumnMatch] = {}
    warnings: list[str] = []
    errors: list[str] = []
    candidates: dict[str, list[dict]] = {}
    used: set[int] = set()

    # 1) Elle eşleştirme
    for role_name, col_name in (overrides or {}).items():
        try:
            role = ColumnRole(role_name)
        except ValueError:
            continue
        idx = _find_column_index(columns, col_name)
        if idx is None:
            errors.append(f"kolon_eslesme.json: '{col_name}' kolonu bulunamadı ({role.value}).")
            continue
        matches[role.value] = ColumnMatch(role.value, columns[idx], idx, 1.0, "override", "elle eşleştirme")
        used.add(idx)

    # 2) Otomatik tespit
    for role, scorer, threshold, margin in _ROLE_RULES:
        if role.value in matches:
            continue
        scored = []
        for p in profiles:
            if p.index in used:
                continue
            score, method, evidence = scorer(p)
            if score > 0:
                scored.append((score, p, method, evidence))
        scored.sort(key=lambda t: (-t[0], -t[1].fill, t[1].index))
        candidates[role.value] = [
            {"column": p.name, "score": round(s, 3), "method": m, "evidence": e}
            for s, p, m, e in scored[:4]
        ]
        accepted = [t for t in scored if t[0] >= threshold]
        if not accepted:
            if role is ColumnRole.MAPS_URL:
                errors.append("Google Maps bağlantısı içeren kolon bulunamadı "
                              "(örnek hücrelerin hiçbirinde Maps URL örüntüsü yok).")
            continue
        best = accepted[0]
        if len(accepted) > 1:
            second = accepted[1]
            gap = best[0] - second[0]
            if margin is not None and gap < margin:
                errors.append(
                    f"{role.value} kolonu belirsiz: '{best[1].name}' ({best[0]:.2f}) ile "
                    f"'{second[1].name}' ({second[0]:.2f}) birbirine çok yakın; yanlış eşleştirme "
                    "yapılmadı. kolon_eslesme.json ile seçin."
                )
                continue
            if gap < 0.05:
                warnings.append(
                    f"{role.value} için birden fazla güçlü aday var: '{best[1].name}' seçildi, "
                    f"'{second[1].name}' yok sayıldı."
                )
        score, p, method, evidence = best
        if method == "content" and role is ColumnRole.STATUS:
            warnings.append(f"Durum kolonu başlıktan değil içerikten tahmin edildi: '{p.name}'.")
        if role is ColumnRole.MAPS_URL and p.nonempty == 0:
            warnings.append(f"'{p.name}' başlığı bağlantı kolonu gibi ama örnek satırlar boş.")
        if role is ColumnRole.MAPS_URL and p.nonempty and p.ratio(p.maps) < 0.9:
            warnings.append(
                f"'{p.name}' kolonundaki örnek hücrelerin {1 - p.ratio(p.maps):.0%}'i geçerli Maps "
                "bağlantısı değil; bu satırlar hatalı URL olarak raporlanacak."
            )
        matches[role.value] = ColumnMatch(role.value, p.name, p.index, round(min(score, 1.0), 3), method, evidence)
        used.add(p.index)
    return matches, warnings, errors, candidates


def _find_column_index(columns: list[str], wanted: str) -> Optional[int]:
    if wanted in columns:
        return columns.index(wanted)
    nw = normalize_header(wanted)
    for i, c in enumerate(columns):
        if normalize_header(c) == nw:
            return i
    return None


# ── Başlık satırı tespiti ────────────────────────────────────────────────────
def _is_header_like(v: Any) -> bool:
    if not isinstance(v, str):
        return False
    s = v.strip()
    return bool(s) and len(s) <= 80 and not _NUMERIC_RE.match(s) and not looks_like_maps_url(s) \
        and not s.lower().startswith(("http://", "https://"))


def detect_header_row(rows: list[tuple]) -> int:
    """İlk satırlar içinden başlık satırını (0 tabanlı) bulur; başlık yoksa -1."""
    best_i, best_score = 0, -1.0
    for i, row in enumerate(rows[:SCHEMA_HEADER_SCAN_ROWS]):
        cells = [c for c in row if clean_cell(c)]
        if not cells:
            continue
        header_like = sum(_is_header_like(c) for c in row)
        has_maps = any(looks_like_maps_url(c) for c in row)
        score = header_like - (100 if has_maps else 0)
        if header_like >= max(2, 0.5 * len(cells)) and score > best_score:
            best_i, best_score = i, score
            break   # ilk uygun satır başlıktır
    if best_score < 0:
        first = next((r for r in rows if any(clean_cell(c) for c in r)), ())
        if any(looks_like_maps_url(c) for c in first):
            return -1
    return best_i


# ── Dosya okuma ──────────────────────────────────────────────────────────────
def _read_csv_rows(path: Path, limit: Optional[int] = None) -> Iterator[tuple]:
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        head = fh.read(8192)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(head, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        for i, row in enumerate(csv.reader(fh, dialect)):
            if limit is not None and i >= limit:
                break
            yield tuple(c if c != "" else None for c in row)


def _open_xlsx(path: Path):
    import openpyxl
    return openpyxl.load_workbook(path, read_only=True, data_only=True)


def _sheet_head(ws, n: int) -> list[tuple]:
    return [tuple(r) for r in ws.iter_rows(min_row=1, max_row=n, values_only=True)]


def _schema_from_rows(file_name: str, sheet: Optional[str], head: list[tuple], max_row: int,
                      overrides: Optional[dict]) -> SheetSchema:
    hdr = detect_header_row(head)
    width = max((len(r) for r in head), default=0)
    if hdr >= 0:
        raw = list(head[hdr]) + [None] * (width - len(head[hdr]))
        while raw and raw[-1] is None and all(len(r) < len(raw) or r[len(raw) - 1] is None for r in head):
            raw.pop()
        data_rows = head[hdr + 1:]
        header_row = hdr + 1
    else:
        raw = [None] * width
        data_rows = head
        header_row = 0
    # Başlıksız / boş başlıklı kolonlara Excel harfi ver
    if header_row == 0:
        from openpyxl.utils import get_column_letter
        raw = [f"Kolon {get_column_letter(i + 1)}" for i in range(width)]
    columns = unique_columns(raw)
    rows = [tuple(r[:len(columns)]) + (None,) * max(0, len(columns) - len(r)) for r in data_rows]
    matches, warnings, errors, candidates = detect_columns(columns, rows, overrides)
    if header_row == 0:
        warnings.append("Başlık satırı bulunamadı; kolonlar harflerle adlandırıldı.")
    elif header_row > 1:
        warnings.append(f"Başlık satırı {header_row}. satırda tespit edildi.")
    return SheetSchema(
        file=file_name, sheet=sheet, header_row=header_row, columns=columns, raw_headers=raw,
        row_count=max(0, (max_row or 0) - header_row), matches=matches, warnings=warnings,
        errors=errors, candidates=candidates,
    )


@dataclass
class WorkbookProfile:
    file: str
    sheets: list[dict]                 # Her sayfanın özeti
    schema: Optional[SheetSchema]      # Seçilen veri sayfası
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {"file": self.file, "sheets": self.sheets, "error": self.error,
                "schema": self.schema.to_dict() if self.schema else None}

    @staticmethod
    def from_dict(d: dict) -> "WorkbookProfile":
        return WorkbookProfile(d["file"], d["sheets"],
                               SheetSchema.from_dict(d["schema"]) if d.get("schema") else None, d.get("error"))


def _analyze_uncached(path: Path, overrides: dict) -> WorkbookProfile:
    n = SCHEMA_HEADER_SCAN_ROWS + SCHEMA_SAMPLE_ROWS
    forced_sheet = overrides.get("sheet")
    role_overrides = {k: v for k, v in overrides.items() if k != "sheet"}
    schemas: list[SheetSchema] = []
    if path.suffix.lower() == ".csv":
        head = list(_read_csv_rows(path, n))
        total = sum(1 for _ in _read_csv_rows(path))
        schemas.append(_schema_from_rows(path.name, None, head, total, role_overrides))
    else:
        wb = _open_xlsx(path)
        try:
            for ws in wb.worksheets:
                if forced_sheet and ws.title != forced_sheet:
                    continue
                head = _sheet_head(ws, n)
                schemas.append(_schema_from_rows(path.name, ws.title, head, ws.max_row or len(head),
                                                 role_overrides))
        finally:
            wb.close()

    sheets = [{
        "sheet": s.sheet, "rows": s.row_count, "header_row": s.header_row,
        "columns": s.columns, "detected": {k: v.column for k, v in s.matches.items()},
    } for s in schemas]

    with_url = [s for s in schemas if ColumnRole.MAPS_URL.value in s.matches]
    if not with_url:
        msg = (f"'{path.name}' içinde Google Maps bağlantısı içeren bir sayfa bulunamadı. "
               f"Sayfalar: {[s.sheet for s in schemas]}")
        best = max(schemas, key=lambda s: s.row_count, default=None)
        return WorkbookProfile(path.name, sheets, best, error=msg)
    with_url.sort(key=lambda s: (-s.matches[ColumnRole.MAPS_URL.value].confidence * min(1, s.row_count / 50),
                                 -s.row_count))
    chosen = with_url[0]
    others = [s.sheet for s in with_url[1:]]
    if others:
        chosen.warnings.append(f"Bağlantı içeren başka sayfalar da var ({others}); "
                               f"en uygun sayfa '{chosen.sheet}' seçildi.")
    return WorkbookProfile(path.name, sheets, chosen)


# ── Elle eşleştirme dosyası ──────────────────────────────────────────────────
def load_overrides(file_name: str) -> dict:
    """input/kolon_eslesme.json → {"*": {...}, "dosya.xlsx": {"sheet": "...", "maps_url": "..."}}"""
    if not COLUMN_OVERRIDES_FILE.exists():
        return {}
    try:
        data = json.loads(COLUMN_OVERRIDES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"kolon_eslesme.json okunamadı: {e}")
        return {}
    merged = dict(data.get("*", {}) or data.get("varsayilan", {}))
    merged.update(data.get(file_name, {}))
    return merged


# ── Önbellekli analiz ────────────────────────────────────────────────────────
_cache_lock = threading.Lock()
_mem_cache: dict[str, dict] = {}


def _cache_key(path: Path, overrides: dict) -> str:
    st = path.stat()
    ov = hashlib.md5(json.dumps(overrides, sort_keys=True).encode()).hexdigest()[:8]
    return f"{path.name}|{st.st_size}|{st.st_mtime_ns}|v{SCHEMA_VERSION}|{ov}"


def _load_disk_cache() -> dict:
    try:
        return json.loads(SCHEMA_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def analyze_file(path: Path, use_cache: bool = True) -> WorkbookProfile:
    """Dosyayı analiz eder (dosya değişmediyse önbellekten)."""
    path = Path(path)
    overrides = load_overrides(path.name)
    key = _cache_key(path, overrides)
    with _cache_lock:
        if use_cache:
            if key in _mem_cache:
                return WorkbookProfile.from_dict(_mem_cache[key])
            disk = _load_disk_cache()
            if key in disk:
                _mem_cache[key] = disk[key]
                return WorkbookProfile.from_dict(disk[key])
    profile = _analyze_uncached(path, overrides)
    with _cache_lock:
        _mem_cache[key] = profile.to_dict()
        disk = {k: v for k, v in _load_disk_cache().items() if not k.startswith(path.name + "|")}
        disk[key] = _mem_cache[key]
        try:
            SCHEMA_CACHE_FILE.write_text(json.dumps(disk, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            logger.warning(f"Şema önbelleği yazılamadı: {e}")
    return profile


def get_schema(path: Path, *required: ColumnRole) -> SheetSchema:
    """Seçilen veri sayfasının şemasını döndürür; gerekli roller yoksa ColumnDetectionError."""
    profile = analyze_file(path)
    if profile.schema is None:
        raise ColumnDetectionError(profile.error or f"'{path.name}' okunamadı.")
    if profile.error and ColumnRole.MAPS_URL in required:
        raise ColumnDetectionError(profile.error)
    profile.schema.require(*required)
    for w in profile.schema.warnings:
        logger.warning(f"[{path.name}] {w}")
    return profile.schema


def scan_input_dir() -> list[dict]:
    """input/ klasöründeki tüm dosyaların analiz özetleri (API / CLI için)."""
    out = []
    for f in list_input_files():
        try:
            out.append(analyze_file(f).to_dict())
        except Exception as e:   # bozuk/okunamayan dosya diğerlerini engellemesin
            out.append({"file": f.name, "error": f"Okunamadı: {e}", "sheets": [], "schema": None})
    return out


def iter_data_rows(path: Path, schema: SheetSchema) -> Iterator[tuple[int, tuple]]:
    """Veri satırlarını akış halinde döndürür: (0 tabanlı satır no, kolon değerleri)."""
    width = len(schema.columns)
    if path.suffix.lower() == ".csv":
        source = _read_csv_rows(path)
        for _ in range(schema.header_row):
            next(source, None)
        rows: Iterator = source
        wb = None
    else:
        wb = _open_xlsx(path)
        ws = wb[schema.sheet]
        rows = ws.iter_rows(min_row=schema.header_row + 1, values_only=True)
    try:
        for i, r in enumerate(rows):
            r = tuple(r[:width]) + (None,) * max(0, width - len(r))
            yield i, r
    finally:
        if wb is not None:
            wb.close()
