"""
Proje V1 — Merkezi Konfigürasyon
Tüm sabitler ve yol tanımları burada tutulur.
"""
import os
from pathlib import Path

# ── Kök dizin ───────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent

# ── Dosya yolları ────────────────────────────────────────────────────────────
INPUT_DIR = BASE_DIR / "input"
SCREENSHOTS_DIR = BASE_DIR / "screenshots"
SESSIONS_DIR = BASE_DIR / "sessions"
DATA_DIR = BASE_DIR / "data"
EDIT_DB_FILE = DATA_DIR / "edit.db"
STATIC_DIR = BASE_DIR / "static"

def find_label_input_file() -> Path:
    """Etiketleme akışı için giriş dosyası (cleanpoi_cevre ile başlamayan öncelikli)."""
    for ext in ("*.xlsx", "*.xls", "*.csv"):
        files = [f for f in INPUT_DIR.glob(ext) if not f.name.startswith("cleanpoi_cevre")]
        if files:
            return files[0]
    return find_input_file()

def find_edit_input_file() -> Path:
    """Düzenleme akışı için giriş dosyası (cleanpoi_cevre ile başlayan öncelikli)."""
    for ext in ("*.xlsx", "*.xls", "*.csv"):
        files = [f for f in INPUT_DIR.glob(ext) if f.name.startswith("cleanpoi_cevre")]
        if files:
            return files[0]
    return find_input_file()

# Giriş dosyası (varsayılan)
def find_input_file() -> Path:
    for ext in ("*.xlsx", "*.xls", "*.csv"):
        files = list(INPUT_DIR.glob(ext))
        if files:
            return files[0]
    raise FileNotFoundError(f"input/ klasöründe Excel/CSV dosyası bulunamadı: {INPUT_DIR}")

# Session (cookie) dosyası
SESSION_FILE = SESSIONS_DIR / "google_session.json"

# ── Excel sütun isimleri ─────────────────────────────────────────────────────
URL_COLUMN = "URL"                  # Google Maps linkleri
CATEGORY_COLUMN = "Cihan Kategori"  # Kullanıcının gireceği kategori
NAME_COLUMN = "identity_google_name"

# Ek sütunlar (yeni input kutuları)
EXTRA_COLUMNS = [
    "Çıkarılacak Noktalar",
    "Açık Kapalı Kontrol",
    "Pasif",
]

# Düzenle akışı — bu kolon dolu olan satırlar incelenir
KONTROL_COLUMN = "KONTROL"

# ── Buffer / Kuyruk ayarları ──────────────────────────────────────────────────
BUFFER_SIZE = 10          # Kaç screenshot önceden alınsın
BUFFER_REFILL_THRESHOLD = 5  # Buffer bu seviyeye düşünce yeniden doldurulmaya başlanır

# ── Playwright / Browser ayarları ────────────────────────────────────────────
HEADLESS = True           # True → arka planda, False → görünür browser
SCREENSHOT_TIMEOUT = 20_000   # ms — sayfa yükleme zaman aşımı
NAVIGATION_TIMEOUT = 30_000   # ms — navigasyon zaman aşımı
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
