"""
Proje V1 — FastAPI Ana Uygulama
REST API + static file serving.
SOLID: Dependency Injection ile DataManager ve ScreenshotWorker bağımlılıkları yönetilir.
"""
import logging
import os
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field

# src/ klasörünü path'e ekle
sys.path.insert(0, str(Path(__file__).parent))

from config import SCREENSHOTS_DIR, STATIC_DIR, HOST, PORT, BUFFER_SIZE
from data_manager import DataManager
from edit_data_manager import EditDataManager, export_excel
from session_manager import SessionManager
from screenshot_worker import ScreenshotWorker, ScreenshotItem

# ── Loglama ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Uygulama Durumu (Singleton) ───────────────────────────────────────────────
class AppState:
    # -- Etiketleme Akisi
    data_manager: Optional[DataManager] = None
    session_manager: Optional[SessionManager] = None
    screenshot_worker: Optional[ScreenshotWorker] = None
    current_position: int = 0
    current_item: Optional[ScreenshotItem] = None
    is_started: bool = False

    # -- Duzenle Akisi
    edit_data_manager: Optional[EditDataManager] = None
    edit_session_manager: Optional[SessionManager] = None
    edit_screenshot_worker: Optional[ScreenshotWorker] = None
    edit_current_position: int = 0
    edit_current_item: Optional[ScreenshotItem] = None
    edit_is_started: bool = False

state = AppState()

# ── Lifespan (startup/shutdown) ───────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Uygulama başlatılıyor...")
    yield
    # Shutdown — etiketleme akışı
    if state.screenshot_worker:
        await state.screenshot_worker.stop()
    if state.session_manager:
        state.session_manager.stop()
    # Shutdown — düzenle akışı
    if state.edit_screenshot_worker:
        await state.edit_screenshot_worker.stop()
    if state.edit_session_manager:
        state.edit_session_manager.stop()
    logger.info("Uygulama kapatıldı.")

# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Proje V1 — Google Maps Etiketleme",
    version="1.0.0",
    lifespan=lifespan,
)

@app.middleware("http")
async def disable_ui_cache(request, call_next):
    """Geliştirme sırasında eski arayüz dosyalarının tarayıcıda kalmasını önler."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# ── Pydantic Modeller ─────────────────────────────────────────────────────────
class LabelRequest(BaseModel):
    row_index: int
    category: str
    extra_fields: Optional[Dict[str, str]] = None

class SessionStartRequest(BaseModel):
    start_index: int = Field(default=0, ge=0)

class SessionStartResponse(BaseModel):
    status: str
    total: int
    pending: int
    buffer_size: int
    start_index: int

class EditActionRequest(BaseModel):
    row_index: int
    action: str   # "delete" | "keep" | "skip"

class EditUndoRequest(BaseModel):
    row_index: int

# ── API Endpoint'leri ─────────────────────────────────────────────────────────

@app.post("/api/session/start", response_model=SessionStartResponse)
async def start_session(request: Optional[SessionStartRequest] = None):
    """
    Oturumu başlatır:
    1. DataManager ile Excel'i yükler
    2. SessionManager + Playwright başlatır
    3. ScreenshotWorker buffer'ı doldurur (arka planda)
    4. İlk screenshot'ı alır ve hazır olana kadar bekler
    """
    if state.is_started:
        # Yeniden başlatma
        if state.screenshot_worker:
            await state.screenshot_worker.stop()
        state.is_started = False

    try:
        # DataManager
        state.data_manager = DataManager()
        total_count = state.data_manager.get_total_count()
        start_index = request.start_index if request else 0

        # Başlangıç indeksinden itibaren işlenmemiş kayıtları al.
        all_records = state.data_manager.get_pending_records_from_index(start_index)
        pending_count = len(all_records)

        if pending_count == 0:
            raise HTTPException(
                status_code=404,
                detail=f"{start_index}. indeksten itibaren işlenecek kayıt bulunamadı.",
            )

        # SessionManager — sync (BrowserThread başlatır)
        state.session_manager = SessionManager()
        state.session_manager.start()   # sync, thread başlatır ve hazır olmasını bekler

        # ScreenshotWorker
        state.screenshot_worker = ScreenshotWorker(state.session_manager)
        await state.screenshot_worker.start(all_records)

        state.current_position = 0
        state.is_started = True

        logger.info(
            f"Oturum başlatıldı. Başlangıç indeksi: {start_index}, "
            f"Toplam: {total_count}, Bekleyen: {pending_count}"
        )
        return SessionStartResponse(
            status="started",
            total=total_count,
            pending=pending_count,
            buffer_size=BUFFER_SIZE,
            start_index=start_index,
        )

    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Oturum başlatma hatası: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Başlatma hatası: {str(e)}")


@app.get("/api/screenshot/next")
async def get_next_screenshot():
    """
    Sıradaki screenshot'ı döndürür.
    Buffer'dan anlık alır (beklemeden).
    Response: {file_url, row_index, name, position, total, error}
    """
    if not state.is_started or not state.screenshot_worker:
        raise HTTPException(status_code=400, detail="Oturum başlatılmamış. /api/session/start çağrın.")

    if state.screenshot_worker.is_empty():
        return JSONResponse({"status": "completed", "message": "Tüm kayıtlar işlendi!"})

    item = await state.screenshot_worker.get_next()
    if item is None:
        raise HTTPException(status_code=504, detail="Screenshot alınamadı (timeout).")

    state.current_item = item
    state.current_position += 1

    response = {
        "status": "ok",
        "row_index": item.row_index,
        "name": item.name,
        "url": item.url,
        "position": item.position,
        "total": item.total,
        "queue_size": state.screenshot_worker.queue_size(),
        "error": item.error,
    }

    if item.file_path and Path(item.file_path).exists():
        response["file_url"] = f"/screenshots/{Path(item.file_path).name}"
    else:
        response["file_url"] = None

    return JSONResponse(response)


@app.post("/api/label")
async def save_label(request: LabelRequest):
    """
    Kullanıcının girdiği kategoriyi Excel'e kaydeder.
    """
    if not state.data_manager:
        raise HTTPException(status_code=400, detail="DataManager hazır değil.")

    # En az bir alan dolu olmalı (kategori veya ek alanlardan biri)
    has_category = bool(request.category and request.category.strip())
    has_extra = bool(
        request.extra_fields
        and any(v.strip() for v in request.extra_fields.values() if v)
    )
    if not has_category and not has_extra:
        raise HTTPException(status_code=422, detail="En az bir alan doldurulmalıdır.")

    try:
        state.data_manager.save_category(
            request.row_index,
            request.category.strip() if request.category else "",
            extra_fields=request.extra_fields,
        )

        return JSONResponse({"status": "saved", "row_index": request.row_index, "category": request.category.strip()})

    except Exception as e:
        logger.error(f"Kaydetme hatası — row:{request.row_index}: {e}")
        raise HTTPException(status_code=500, detail=f"Kaydetme hatası: {str(e)}")


@app.get("/api/status")
async def get_status():
    """Worker ve buffer durumunu döndürür."""
    if not state.is_started:
        return JSONResponse({"status": "not_started"})

    stats = state.screenshot_worker.get_stats() if state.screenshot_worker else {}
    return JSONResponse({
        "status": "running",
        "current_position": state.current_position,
        "pending": state.data_manager.get_pending_count() if state.data_manager else 0,
        "worker_stats": stats,
    })


@app.post("/api/session/stop")
async def stop_session():
    """Oturumu durdurur ve kaynakları serbest bırakır."""
    if state.screenshot_worker:
        await state.screenshot_worker.stop()
    if state.session_manager:
        state.session_manager.stop()   # sync
    state.is_started = False
    return JSONResponse({"status": "stopped"})


# ── Screenshot dosyalarını serve et ──────────────────────────────────────────
@app.get("/screenshots/{filename}")
async def serve_screenshot(filename: str):
    file_path = SCREENSHOTS_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Screenshot bulunamadı.")
    return FileResponse(str(file_path), media_type="image/png")


# ── Static files + Ana sayfa ─────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/")
async def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


# ── Yardımcı ─────────────────────────────────────────────────────────────────

@app.get("/edit")
async def edit_page():
    return FileResponse(str(STATIC_DIR / "edit.html"))


# -- Duzenle Akisi API Endpoint'leri ---

@app.post("/api/edit/session/start", response_model=SessionStartResponse)
async def edit_start_session(request: Optional[SessionStartRequest] = None):
    if state.edit_is_started:
        if state.edit_screenshot_worker:
            await state.edit_screenshot_worker.stop()
        state.edit_is_started = False
    try:
        state.edit_data_manager = EditDataManager()
        start_index = request.start_index if request else 0
        all_records = state.edit_data_manager.get_records_from_index(start_index)
        pending_count = len(all_records)
        if pending_count == 0:
            raise HTTPException(status_code=404, detail='Kayit bulunamadi.')
        state.edit_session_manager = SessionManager()
        state.edit_session_manager.start()
        state.edit_screenshot_worker = ScreenshotWorker(state.edit_session_manager)
        await state.edit_screenshot_worker.start(all_records)
        state.edit_current_position = 0
        state.edit_is_started = True
        return SessionStartResponse(status='started',
            total=state.edit_data_manager.get_total_count(),
            pending=pending_count, buffer_size=BUFFER_SIZE, start_index=start_index)
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/edit/screenshot/next")
async def edit_get_next_screenshot():
    if not state.edit_is_started or not state.edit_screenshot_worker:
        raise HTTPException(status_code=400, detail='Oturum baslatilmamis.')
    if state.edit_screenshot_worker.is_empty():
        return JSONResponse({'status':'completed','message':'Tum kayitlar islendi!'})
    item = await state.edit_screenshot_worker.get_next()
    if item is None:
        raise HTTPException(status_code=504, detail='Screenshot alinamadi.')
    state.edit_current_item = item
    state.edit_current_position += 1
    kontrol_value = state.edit_data_manager.get_kontrol_value(item.row_index) if state.edit_data_manager else ''
    next_name = None
    if state.edit_screenshot_worker:
        records = state.edit_screenshot_worker._pending_records
        pos = item.position + 1
        if pos < len(records):
            next_name = records[pos].get("name", "")
    resp = {'status':'ok','row_index':item.row_index,'name':item.name,
        'next_name':next_name,
        'url':item.url,'kontrol_value':kontrol_value,
        'position':item.position,'total':item.total,
        'queue_size':state.edit_screenshot_worker.queue_size(),'error':item.error}
    if item.file_path and Path(item.file_path).exists():
        resp['file_url'] = f'/screenshots/{Path(item.file_path).name}'
    else:
        resp['file_url'] = None
    return JSONResponse(resp)


@app.post("/api/edit/action")
async def edit_action(request: EditActionRequest):
    if not state.edit_data_manager:
        raise HTTPException(status_code=400, detail='EditDataManager hazir degil.')
    action = request.action.strip().lower()
    if action not in ('delete','keep','skip'):
        raise HTTPException(status_code=422, detail='Gecersiz action.')
    try:
        if action == 'delete':
            state.edit_data_manager.delete_row(request.row_index)
            return JSONResponse({'status':'deleted','row_index':request.row_index})
        elif action == 'skip':
            state.edit_data_manager.skip_row(request.row_index)
            return JSONResponse({'status':'skipped','row_index':request.row_index})
        else:
            state.edit_data_manager.keep_row(request.row_index)
            return JSONResponse({'status':'kept','row_index':request.row_index})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/edit/action/undo")
async def edit_undo_action(request: EditUndoRequest):
    if not state.edit_data_manager:
        raise HTTPException(status_code=400, detail='EditDataManager hazir degil.')
    try:
        undone = state.edit_data_manager.undo_action(request.row_index)
        return JSONResponse({'status':'ok', 'undone_action': undone, 'row_index': request.row_index})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/edit/session/stop")
async def edit_stop_session():
    if state.edit_screenshot_worker:
        await state.edit_screenshot_worker.stop()
    if state.edit_session_manager:
        state.edit_session_manager.stop()
    if state.edit_data_manager:
        state.edit_data_manager.flush()
    state.edit_is_started = False
    return JSONResponse({'status':'stopped'})


@app.get("/api/edit/status")
async def get_edit_status():
    if not state.edit_is_started:
        return JSONResponse({'status':'not_started'})
    stats = state.edit_screenshot_worker.get_stats() if state.edit_screenshot_worker else {}
    return JSONResponse({'status':'running','current_position':state.edit_current_position,
        'pending':state.edit_data_manager.get_pending_count() if state.edit_data_manager else 0,
        'worker_stats':stats})

@app.get("/api/edit/export")
def edit_export_excel():
    """Veritabanındaki güncel hali (silinenler hariç) Excel olarak indirir."""
    fd, tmp = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        export_excel(Path(tmp))
    except FileNotFoundError as e:
        _cleanup_file(tmp)
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        _cleanup_file(tmp)
        raise HTTPException(status_code=500, detail=str(e))
    filename = f"cleanpoi_cevre_guncel_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    return FileResponse(
        tmp, filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        background=BackgroundTask(_cleanup_file, tmp),
    )


def _cleanup_file(file_path: str) -> None:
    p = Path(file_path)
    if p.exists():
        p.unlink(missing_ok=True)


# ── Giriş noktası ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=HOST,
        port=PORT,
        reload=False,
        log_level="info",
    )
