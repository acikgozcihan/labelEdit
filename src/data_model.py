"""
Proje V1 — Ortak Veri Modeli (veri sözleşmesi)

Excel okuma (excel_schema), veri yöneticileri (data_manager / edit_data_manager),
ekran görüntüsü motoru (capture_engine) ve API katmanı bu modüldeki tipler
üzerinden haberleşir. Yeni bir Excel şablonu geldiğinde yalnızca kolon
tespiti değişir; diğer modüller hep aynı alanları (PlaceRecord) görür.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional
from urllib.parse import parse_qs, quote, unquote, urlsplit, urlunsplit


# ── Kolon rolleri ────────────────────────────────────────────────────────────
class ColumnRole(str, Enum):
    MAPS_URL = "maps_url"     # Google Maps bağlantısı (zorunlu)
    STATUS = "status"         # Kontrol / durum / işlendi bilgisi (düzenle akışında zorunlu)
    NAME = "name"             # Mekan adı (isteğe bağlı; yoksa URL'den türetilir)
    PLACE_ID = "place_id"     # Google place_id (isteğe bağlı; tekrarları önlemede kullanılır)


ROLE_LABELS_TR = {
    ColumnRole.MAPS_URL: "Google Maps bağlantısı",
    ColumnRole.STATUS: "Kontrol / durum",
    ColumnRole.NAME: "Mekan adı",
    ColumnRole.PLACE_ID: "Place ID",
}


class ColumnDetectionError(Exception):
    """Kolonlar güvenilir biçimde tespit edilemediğinde (yanlış eşleştirme yerine) fırlatılır."""


@dataclass(frozen=True)
class ColumnMatch:
    role: str
    column: str          # Veri tablosundaki kolon adı (pandas uyumlu, benzersiz)
    index: int           # 0 tabanlı kolon sırası
    confidence: float    # 0..1
    method: str          # header | content | header+content | override
    evidence: str = ""   # İnsan okunur gerekçe

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ColumnMatch":
        return ColumnMatch(**d)


@dataclass
class SheetSchema:
    """Bir veri sayfasının tespit edilen yapısı."""
    file: str
    sheet: Optional[str]
    header_row: int                 # Excel'deki 1 tabanlı başlık satırı (0 = başlıksız)
    columns: list[str]              # Benzersiz kolon adları (pandas ile aynı kural)
    raw_headers: list[Any]
    row_count: int                  # Yaklaşık veri satırı sayısı
    matches: dict[str, ColumnMatch] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    candidates: dict[str, list[dict]] = field(default_factory=dict)

    # Veri satırı (0 tabanlı) → Excel satırı (1 tabanlı)
    def excel_row(self, row_index: int) -> int:
        return self.header_row + 1 + row_index

    def column(self, role: ColumnRole | str) -> Optional[str]:
        m = self.matches.get(ColumnRole(role).value)
        return m.column if m else None

    def index(self, role: ColumnRole | str) -> Optional[int]:
        m = self.matches.get(ColumnRole(role).value)
        return m.index if m else None

    def require(self, *roles: ColumnRole) -> None:
        missing = [r for r in roles if r.value not in self.matches]
        if missing:
            names = ", ".join(ROLE_LABELS_TR[r] for r in missing)
            detail = "; ".join(self.errors) if self.errors else ""
            raise ColumnDetectionError(
                f"'{self.file}' ({self.sheet}) içinde gerekli kolon(lar) güvenilir biçimde "
                f"bulunamadı: {names}. {detail} Mevcut kolonlar: {self.columns}. "
                "Gerekirse input/kolon_eslesme.json ile elle eşleştirebilirsiniz."
            )

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "sheet": self.sheet,
            "header_row": self.header_row,
            "columns": self.columns,
            "raw_headers": [None if h is None else str(h) for h in self.raw_headers],
            "row_count": self.row_count,
            "matches": {k: v.to_dict() for k, v in self.matches.items()},
            "warnings": self.warnings,
            "errors": self.errors,
            "candidates": self.candidates,
        }

    @staticmethod
    def from_dict(d: dict) -> "SheetSchema":
        return SheetSchema(
            file=d["file"],
            sheet=d.get("sheet"),
            header_row=d["header_row"],
            columns=list(d["columns"]),
            raw_headers=list(d.get("raw_headers") or d["columns"]),
            row_count=d.get("row_count", 0),
            matches={k: ColumnMatch.from_dict(v) for k, v in d.get("matches", {}).items()},
            warnings=list(d.get("warnings", [])),
            errors=list(d.get("errors", [])),
            candidates=dict(d.get("candidates", {})),
        )


# ── Mekan kaydı (tüm modüllerin ortak sözleşmesi) ─────────────────────────────
@dataclass
class PlaceRecord:
    row_index: int                  # Kaynak tablodaki 0 tabanlı veri satırı
    url: str                        # Kaynaktaki ham bağlantı (değiştirilmez)
    name: str = ""
    status_value: str = ""          # Kontrol/durum kolonunun değeri
    position: int = 0               # Oturum içindeki sıra
    total: int = 0
    place_key: str = ""             # Tekrar tespiti için kanonik anahtar
    nav_url: str = ""               # Tarayıcıda açılacak normalize URL
    url_error: Optional[str] = None # Geçersiz bağlantı açıklaması

    @staticmethod
    def build(row_index: int, raw_url: Any, name: Any = None, status: Any = None,
              place_id: Any = None, extra_hosts: Optional[tuple[str, ...]] = None) -> "PlaceRecord":
        if extra_hosts is None:
            from config import EXTRA_ALLOWED_MAPS_HOSTS as extra_hosts
        url = clean_cell(raw_url)
        info = parse_maps_url(url, extra_hosts=extra_hosts)
        pid = clean_cell(place_id)
        key = info.place_key
        if pid and PLACE_ID_RE.match(pid):
            key = f"pid:{pid}"
        name_s = clean_cell(name) or info.name_hint
        return PlaceRecord(
            row_index=int(row_index),
            url=url,
            name=name_s,
            status_value=clean_cell(status),
            place_key=key or f"invalid:{url}",
            nav_url=info.url or url,
            url_error=info.error,
        )

    def to_dict(self) -> dict:
        """Geriye dönük uyumlu sözlük (eski kod 'kontrol_value' alanını kullanır)."""
        d = asdict(self)
        d["kontrol_value"] = self.status_value
        return d

    @staticmethod
    def from_dict(d: dict) -> "PlaceRecord":
        if isinstance(d, PlaceRecord):
            return d
        if d.get("place_key"):
            return PlaceRecord(
                row_index=int(d["row_index"]), url=d.get("url", ""), name=d.get("name", "") or "",
                status_value=d.get("status_value") or d.get("kontrol_value") or "",
                position=d.get("position", 0), total=d.get("total", 0),
                place_key=d["place_key"], nav_url=d.get("nav_url") or d.get("url", ""),
                url_error=d.get("url_error"),
            )
        rec = PlaceRecord.build(d["row_index"], d.get("url"), d.get("name"), d.get("kontrol_value"))
        rec.position, rec.total = d.get("position", 0), d.get("total", 0)
        return rec


# ── Yakalama sonucu ──────────────────────────────────────────────────────────
class ErrorKind(str, Enum):
    RATE_LIMITED = "rate_limited"   # Bot/captcha sayfası, HTTP 429 → havuz yavaşlar
    TRANSIENT = "transient"         # Zaman aşımı, bağlantı kopması, 5xx → tekrar denenir
    PERMANENT = "permanent"         # 404 vb. → tekrar denenmez
    INVALID_URL = "invalid_url"     # Google Maps bağlantısı değil → ağa hiç çıkılmaz
    CANCELLED = "cancelled"


@dataclass
class CaptureOutcome:
    place_key: str
    url: str
    file_path: str = ""
    error: Optional[str] = None
    error_kind: Optional[str] = None
    attempts: int = 0
    from_cache: bool = False
    duration: float = 0.0
    finished_at: float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.file_path)


# ── Hücre ve URL yardımcıları ─────────────────────────────────────────────────
def clean_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value != value:   # NaN
        return ""
    s = str(value).strip()
    return "" if s.lower() in ("nan", "none", "nat") else s


_GOOGLE_HOST_RE = re.compile(r"^(?:www\.|maps\.)?google\.(?:[a-z]{2,3})(?:\.[a-z]{2})?$")
_SHORT_HOSTS = {"maps.app.goo.gl", "goo.gl", "g.page", "g.co"}
MAPS_URL_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?(?:"
    r"google\.[a-z.]{2,8}/maps"
    r"|maps\.google\.[a-z.]{2,8}"
    r"|maps\.app\.goo\.gl/"
    r"|goo\.gl/maps"
    r"|g\.page/"
    r")",
    re.IGNORECASE,
)
_HYPERLINK_FORMULA_RE = re.compile(r'^=\s*HYPERLINK\(\s*"([^"]+)"', re.IGNORECASE)
_FEATURE_ID_RE = re.compile(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", re.IGNORECASE)
_PLACE_PATH_RE = re.compile(r"/maps/place/([^/@?]+)")
PLACE_ID_RE = re.compile(r"^(?:ChIJ|GhIJ|EhI|Ei)[A-Za-z0-9_-]{10,}$")


def looks_like_maps_url(value: Any) -> bool:
    s = clean_cell(value)
    if not s:
        return False
    if MAPS_URL_PATTERN.search(s[:300]):
        return True
    from config import EXTRA_ALLOWED_MAPS_HOSTS   # yalnızca test ortamında dolu
    return bool(EXTRA_ALLOWED_MAPS_HOSTS) and "/maps" in s and         parse_maps_url(s, extra_hosts=EXTRA_ALLOWED_MAPS_HOSTS).error is None


@dataclass(frozen=True)
class MapsUrlInfo:
    url: Optional[str]          # Normalize edilmiş, gezinilebilir URL
    place_key: Optional[str]    # Kanonik anahtar
    name_hint: str = ""
    error: Optional[str] = None


def parse_maps_url(raw: Any, extra_hosts: tuple[str, ...] = ()) -> MapsUrlInfo:
    """Google Maps bağlantısını doğrular, normalize eder ve tekrar anahtarı üretir."""
    s = clean_cell(raw)
    if not s:
        return MapsUrlInfo(None, None, error="Bağlantı boş")
    m = _HYPERLINK_FORMULA_RE.match(s)
    if m:
        s = m.group(1)
    if s.startswith("//"):
        s = "https:" + s
    if not re.match(r"^[a-z][a-z0-9+.-]*://", s, re.I):
        s = "https://" + s
    try:
        parts = urlsplit(s)
    except ValueError:
        return MapsUrlInfo(None, None, error="Bağlantı çözümlenemedi")
    if parts.scheme.lower() not in ("http", "https"):
        return MapsUrlInfo(None, None, error=f"Desteklenmeyen şema: {parts.scheme}")
    host = (parts.hostname or "").lower()
    netloc_l = parts.netloc.lower()
    path = parts.path or ""

    allowed_extra = any(netloc_l == h.lower() or host == h.lower() for h in extra_hosts)
    is_google = bool(_GOOGLE_HOST_RE.match(host)) and (
        path.startswith("/maps") or host.startswith("maps.")
    )
    is_short = host in _SHORT_HOSTS and (host != "goo.gl" or path.startswith("/maps"))
    if not (is_google or is_short or allowed_extra):
        return MapsUrlInfo(None, None, error=f"Google Maps bağlantısı değil: {host or s[:40]}")
    if any(ch in s for ch in (" ", "\n", "\t")):
        # Hücre içine yapıştırılmış boşluklar: yalnızca path'i yeniden kodla
        path = quote(unquote(path.replace(" ", "+")), safe="/:@!$&'()*+,;=-._~%")

    scheme = parts.scheme.lower() if allowed_extra else "https"
    canonical = urlunsplit((scheme, parts.netloc, path, parts.query, ""))

    name_hint = ""
    pm = _PLACE_PATH_RE.search(path)
    if pm:
        name_hint = unquote(pm.group(1).replace("+", " ")).strip()

    key: Optional[str] = None
    fm = _FEATURE_ID_RE.search(s)
    if fm:
        key = f"fid:{fm.group(1).lower()}"
    else:
        qs = parse_qs(parts.query)
        for param, prefix in (("query_place_id", "pid"), ("place_id", "pid"), ("cid", "cid"), ("ftid", "fid")):
            if qs.get(param):
                key = f"{prefix}:{qs[param][0]}"
                break
    if key is None:
        key = "url:" + canonical
    return MapsUrlInfo(canonical, key, name_hint=name_hint)


def capture_file_stem(place_key: str) -> str:
    """Anahtar → deterministik dosya adı (aynı mekan için tek dosya)."""
    return hashlib.sha1(place_key.encode("utf-8")).hexdigest()[:24]
