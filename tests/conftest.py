"""
Testler gerçek input/ data/ screenshots/ klasörlerine DOKUNMAZ: tüm yollar
geçici bir klasöre yönlendirilir (config modülü import edilmeden önce).
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="labeledit_test_"))

os.environ.update({
    "LABELEDIT_INPUT_DIR": str(TMP / "input"),
    "LABELEDIT_DATA_DIR": str(TMP / "data"),
    "LABELEDIT_SCREENSHOTS_DIR": str(TMP / "screenshots"),
    "LABELEDIT_SESSIONS_DIR": str(TMP / "sessions"),
    "LABELEDIT_EXTRA_MAPS_HOSTS": "127.0.0.1",
    "LABELEDIT_SETTLE_MS": "150",
    "LABELEDIT_MAPS_WAIT_MS": "1500",
    "LABELEDIT_PAGE_TIMEOUT_MS": "3000",
    "LABELEDIT_BACKOFF_BASE": "0.2",
    "LABELEDIT_BACKOFF_CAP": "1.0",
    "LABELEDIT_COOLDOWN_BASE": "0.5",
    "LABELEDIT_COOLDOWN_CAP": "2.0",
    "LABELEDIT_MAX_RATE_PER_MIN": "1200",
    "LABELEDIT_GET_NEXT_TIMEOUT": "30",
})
(TMP / "input").mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))


def maps_url(i: int, name: str = "Mekan") -> str:
    return (f"https://www.google.com/maps/place/{name.replace(' ', '+')}+{i}/data=!4m7!3m6!1s"
            f"0x14cab{i:05x}:0x{i:08x}!8m2!3d41.0!4d29.0!16s%2Fg%2F11")


@pytest.fixture
def input_dir() -> Path:
    import config
    d = config.INPUT_DIR
    for f in d.iterdir():
        if f.is_file():
            f.unlink()
    yield d
    for f in d.iterdir():
        if f.is_file():
            f.unlink()


def write_xlsx(path: Path, sheets: dict) -> Path:
    """sheets: {sayfa_adı: [satır, ...]} — ilk sayfa ilk sırada."""
    from openpyxl import Workbook
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(list(r))
    wb.save(path)
    return path
