"""
Proje V1 — Tek Tıkla Başlatma
python run.py

Playwright browser sürecini asyncio.create_subprocess_exec() ile başlatır.
Bu yalnızca ProactorEventLoop ile çalışır (Windows varsayılanı).
SelectorEventLoop subprocess desteklemez — policy override YAPILMAMALIDIR.
"""
import sys
import subprocess
import webbrowser
from pathlib import Path
from threading import Timer

SRC_DIR = Path(__file__).parent / "src"
HOST = "127.0.0.1"
PORT = 8000


def open_browser():
    webbrowser.open(f"http://{HOST}:{PORT}")


if __name__ == "__main__":
    print("=" * 60)
    print("  Proje V1 — Google Maps Etiketleme Aracı")
    print(f"  Adres: http://{HOST}:{PORT}")
    print("  Durdurmak için: Ctrl+C")
    print("=" * 60)

    # 2 saniye sonra browser aç
    Timer(2.0, open_browser).start()

    # uvicorn'u src/ klasöründen başlat
    # --loop asyncio → varsayılan ProactorEventLoop kullanılır
    subprocess.run(
        [
            sys.executable, "-m", "uvicorn",
            "main:app",
            "--host", HOST,
            "--port", str(PORT),
            "--loop", "asyncio",
            "--reload",
            "--reload-dir", str(SRC_DIR),
            "--reload-dir", str(Path(__file__).parent / "static"),
            "--log-level", "info",
        ],
        cwd=str(SRC_DIR),
    )
