"""
Proje V1 — Merkezi Konfigürasyon
Tüm sabitler ve yol tanımları burada tutulur.

Her ayar ortam değişkeniyle (LABELEDIT_*) ezilebilir; böylece testler ve
toplu işler gerçek veriye dokunmadan ayrı klasörlerde çalışabilir.
"""
import os
from pathlib import Path
from typing import Optional


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).resolve() if value else default


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    return float(value) if value not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    return value.strip().lower() in ("1", "true", "yes", "evet", "on")


# ── Kök dizin ───────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent

# ── Dosya yolları ────────────────────────────────────────────────────────────
INPUT_DIR = _env_path("LABELEDIT_INPUT_DIR", BASE_DIR / "input")
SCREENSHOTS_DIR = _env_path("LABELEDIT_SCREENSHOTS_DIR", BASE_DIR / "screenshots")
SESSIONS_DIR = _env_path("LABELEDIT_SESSIONS_DIR", BASE_DIR / "sessions")
DATA_DIR = _env_path("LABELEDIT_DATA_DIR", BASE_DIR / "data")
STATIC_DIR = BASE_DIR / "static"

# Ekran görüntüsü önbelleği / checkpoint veritabanı
CAPTURE_DB_FILE = DATA_DIR / "captures.db"
# Etiketleme akışı kayıt günlüğü (Excel'e yazılmadan önce kalıcı hale gelir)
LABEL_JOURNAL_FILE = DATA_DIR / "label_journal.db"
# Excel şema analizi önbelleği
SCHEMA_CACHE_FILE = DATA_DIR / "schema_cache.json"
# İsteğe bağlı elle kolon eşleştirme dosyası (otomatik tespit yetmezse)
COLUMN_OVERRIDES_FILE = INPUT_DIR / "kolon_eslesme.json"

SUPPORTED_EXTENSIONS = (".xlsx", ".xlsm", ".csv")

# Düzenle akışı kaynak dosyaları bu önekle başlar; her kaynak dosyanın kendi veritabanı vardır.
# Önek yalnızca dosya SEÇİMİ için ipucudur; kolonlar her zaman içerikten tespit edilir.
EDIT_PREFIXES = tuple(
    p for p in os.environ.get("LABELEDIT_EDIT_PREFIXES", "cleanpoi_cevre,celanpoi_").split(",") if p
)


def list_input_files() -> list[Path]:
    """input/ klasöründeki desteklenen dosyalar (Excel kilit dosyaları hariç), ada göre sıralı."""
    if not INPUT_DIR.exists():
        return []
    return sorted(
        f for f in INPUT_DIR.iterdir()
        if f.is_file()
        and f.suffix.lower() in SUPPORTED_EXTENSIONS
        and not f.name.startswith(("~", ".~"))   # Excel kilit / atomik kayıt geçici dosyaları
    )


def resolve_input_file(name: str) -> Path:
    """Kullanıcının seçtiği dosya adını input/ içinde güvenle çözer."""
    candidate = (INPUT_DIR / Path(name).name).resolve()
    if candidate.parent != INPUT_DIR.resolve() or not candidate.exists():
        raise FileNotFoundError(f"input/ klasöründe dosya bulunamadı: {name}")
    return candidate


def find_input_file() -> Path:
    files = list_input_files()
    if files:
        return files[0]
    raise FileNotFoundError(f"input/ klasöründe Excel/CSV dosyası bulunamadı: {INPUT_DIR}")


def find_label_input_file(preferred: Optional[str] = None) -> Path:
    """Etiketleme akışı için giriş dosyası (düzenle akışı dosyaları hariç)."""
    if preferred:
        return resolve_input_file(preferred)
    files = [f for f in list_input_files() if not f.name.startswith(EDIT_PREFIXES)]
    return files[0] if files else find_input_file()


def find_edit_input_file(preferred: Optional[str] = None) -> Path:
    """Düzenleme akışı için giriş dosyası (düzenle öneklerinden en son değiştirilen)."""
    if preferred:
        return resolve_input_file(preferred)
    files = [f for f in list_input_files() if f.name.startswith(EDIT_PREFIXES)]
    if files:
        return max(files, key=lambda f: f.stat().st_mtime)
    return find_input_file()


def edit_db_path(source: Path) -> Path:
    """Kaynak dosyaya özel veritabanı yolu (yeni Excel eski kararları silmesin)."""
    return DATA_DIR / f"edit_{source.stem}.db"


# Session (cookie) dosyası
SESSION_FILE = SESSIONS_DIR / "google_session.json"

# ── Etiketleme akışının YAZDIĞI kolonlar ─────────────────────────────────────
# Bunlar uygulamanın kendi çıktı alanlarıdır (dosyada yoksa oluşturulur).
# Giriş kolonları (Maps linki, isim, kontrol/durum) sabit isimle aranmaz;
# excel_schema modülü tarafından içerikten tespit edilir.
CATEGORY_COLUMN = "Cihan Kategori"
EXTRA_COLUMNS = [
    "Çıkarılacak Noktalar",
    "Açık Kapalı Kontrol",
    "Pasif",
]

# ── Excel analizi ────────────────────────────────────────────────────────────
SCHEMA_SAMPLE_ROWS = _env_int("LABELEDIT_SCHEMA_SAMPLE_ROWS", 400)   # içerik analizi için örnek satır
SCHEMA_HEADER_SCAN_ROWS = 15                                          # başlık satırı aranacak ilk satırlar

# ── Buffer / Kuyruk ayarları ──────────────────────────────────────────────────
BUFFER_SIZE = 10          # Kullanıcının önünde hazırlanan (prefetch) kayıt sayısı
GET_NEXT_TIMEOUT = _env_float("LABELEDIT_GET_NEXT_TIMEOUT", 90.0)  # sn — tek kayıt için azami bekleme

# ── Eş zamanlı yakalama (capture) ve rate limit ayarları ─────────────────────
CAPTURE_MIN_CONCURRENCY = _env_int("LABELEDIT_MIN_CONCURRENCY", 1)
CAPTURE_START_CONCURRENCY = _env_int("LABELEDIT_START_CONCURRENCY", 2)
CAPTURE_MAX_CONCURRENCY = _env_int("LABELEDIT_MAX_CONCURRENCY", 4)
# Sayfa açılışları için tavan hız (dakikada). Adaptif kontrol bunun ALTINDA çalışır.
CAPTURE_MAX_RATE_PER_MIN = _env_float("LABELEDIT_MAX_RATE_PER_MIN", 60.0)
CAPTURE_MAX_ATTEMPTS = _env_int("LABELEDIT_MAX_ATTEMPTS", 4)
# Zaman aşımı en pahalı hata türüdür (her deneme sayfa zaman aşımı kadar sürer)
CAPTURE_MAX_TIMEOUT_ATTEMPTS = _env_int("LABELEDIT_MAX_TIMEOUT_ATTEMPTS", 2)
BACKOFF_BASE_SEC = _env_float("LABELEDIT_BACKOFF_BASE", 2.0)
BACKOFF_CAP_SEC = _env_float("LABELEDIT_BACKOFF_CAP", 60.0)
# Rate limit (bot/captcha sayfası, HTTP 429) görülünce tüm havuz bu kadar bekler (üstel artar)
RATE_LIMIT_COOLDOWN_SEC = _env_float("LABELEDIT_COOLDOWN_BASE", 15.0)
RATE_LIMIT_COOLDOWN_CAP_SEC = _env_float("LABELEDIT_COOLDOWN_CAP", 300.0)
# Kaynak koruması: bu eşiklerin üzerinde eş zamanlılık artırılmaz
RESOURCE_CPU_HIGH_PCT = _env_float("LABELEDIT_CPU_HIGH", 90.0)
RESOURCE_MIN_FREE_MB = _env_float("LABELEDIT_MIN_FREE_MB", 700.0)
# Uzun süreli çalışmada bellek şişmesini önlemek için context bu kadar sayfadan sonra yenilenir
CONTEXT_RECYCLE_PAGES = _env_int("LABELEDIT_CONTEXT_RECYCLE", 400)

# ── Ekran görüntüsü / önbellek ───────────────────────────────────────────────
SCREENSHOT_FORMAT = os.environ.get("LABELEDIT_SCREENSHOT_FORMAT", "jpeg").lower()  # jpeg | png
SCREENSHOT_QUALITY = _env_int("LABELEDIT_SCREENSHOT_QUALITY", 85)
CAPTURE_CACHE_TTL_HOURS = _env_float("LABELEDIT_CACHE_TTL_HOURS", 24 * 14)
# Test ortamı için ek izinli host'lar (ör. "127.0.0.1:8765"); üretimde boş kalır
EXTRA_ALLOWED_MAPS_HOSTS = tuple(
    h for h in os.environ.get("LABELEDIT_EXTRA_MAPS_HOSTS", "").split(",") if h
)

# ── Playwright / Browser ayarları ────────────────────────────────────────────
HEADLESS = _env_bool("LABELEDIT_HEADLESS", True)   # True → arka planda, False → görünür browser
SCREENSHOT_TIMEOUT = _env_int("LABELEDIT_PAGE_TIMEOUT_MS", 20_000)   # ms — sayfa yükleme zaman aşımı
NAVIGATION_TIMEOUT = 30_000   # ms — varsayılan işlem zaman aşımı
MAPS_WAIT_TIMEOUT = _env_int("LABELEDIT_MAPS_WAIT_MS", 8_000)   # ms — harita öğesi bekleme
SETTLE_DELAY_MS = _env_int("LABELEDIT_SETTLE_MS", 1_500)        # ms — harita karolarının oturması
VIEWPORT = {"width": 1280, "height": 800}

# Google consent / maps yüklenme bekleme selektörleri
MAPS_LOADED_SELECTOR = "canvas, #pane, [data-value]"
CONSENT_BUTTON_SELECTORS = [
    'button[aria-label*="Accept"]',
    'button[aria-label*="Kabul"]',
    'form[action*="consent"] button',
    '#L2AGLb',   # Google consent kabul butonu ID
    '.VfPpkd-LgbsSe',  # Material consent butonu
]

# ── Sunucu ayarları ──────────────────────────────────────────────────────────
HOST = "127.0.0.1"
PORT = 8000

# ── Klasörleri oluştur ───────────────────────────────────────────────────────
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
