"""
Proje V1 — FastAPI Ana Uygulama
REST API + static file serving.

Değişiklikler (geriye dönük uyumlu):
  • Excel yükleme, tarayıcı başlatma ve kayıt işlemleri event loop'u bloke etmez
    (threadpool). Eski sürümde her etiket kaydı ~1 sn tüm sunucuyu durduruyordu.
  • Ekran görüntüleri paylaşılan, eş zamanlı CaptureEngine ile hazırlanır.
  • Yeni uç noktalar: /api/input/files (Excel analizi), /api/capture/stats,
    /api/capture/failures.
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
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field

# src/ klasörünü path'e ekle
sys.path.insert(0, str(Path(__file__).parent))

from config import SCREENSHOTS_DIR, STATIC_DIR, HOST, PORT, BUFFER_SIZE
from data_manager import DataManager
from data_model import ColumnDetectionError
from edit_data_manager import EditDataManager, export_excel
from excel_schema import scan_input_dir
from session_manager import SessionManager, current_engine
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


async def _stop_label_flow(close_data: bool) -> None:
    if state.screenshot_worker:
        await state.screenshot_worker.stop()
        state.screenshot_worker = None
    if state.session_manager:
        await run_in_threadpool(state.session_manager.stop)
        state.session_manager = None
    if close_data and state.data_manager:
        await run_in_threadpool(state.data_manager.close)
        state.data_manager = None
    state.is_started = False


async def _stop_edit_flow(close_data: bool) -> None:
    if state.edit_screenshot_worker:
        await state.edit_screenshot_worker.stop()
        state.edit_screenshot_worker = None
    if state.edit_session_manager:
        await run_in_threadpool(state.edit_session_manager.stop)
        state.edit_session_manager = None
    if state.edit_data_manager:
        state.edit_data_manager.flush()
        if close_data:
            state.edit_data_manager.close()
            state.edit_data_manager = None
    state.edit_is_started = False


# ── Lifespan (startup/shutdown) ───────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Uygulama başlatılıyor...")
    yield
    await _stop_label_flow(close_data=True)
    await _stop_edit_flow(close_data=True)
    logger.info("Uygulama kapatıldı.")


# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Proje V1 — Google Maps Etiketleme",
    version="2.0.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def disable_ui_cache(request, call_next):
    """Geliştirme sırasında eski arayüz dosyalarının tarayıcıda kalmasını önler."""
    response = await call_next(request)
    if request.url.path in ("/", "/edit") or request.url.path.startswith("/static/"):
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
    source_file: Optional[str] = None   # İsteğe bağlı: input/ içindeki belirli bir dosya


class SessionStartResponse(BaseModel):
    status: str
    total: int
    pending: int
    buffer_size: int
    start_index: int
    source_file: Optional[str] = None
    columns: Optional[Dict[str, str]] = None
    warnings: list[str] = []


class EditActionRequest(BaseModel):
    row_index: int
    action: str   # "delete" | "keep" | "skip"


class EditUndoRequest(BaseModel):
    row_index: int


def _item_response(item: ScreenshotItem, worker: ScreenshotWorker) -> dict:
    resp = {
        "status": "ok",
        "row_index": item.row_index,
        "name": item.name,
        "url": item.url,
        "position": item.position,
        "total": item.total,
        "queue_size": worker.queue_size(),
        "error": item.error,
        "from_cache": item.from_cache,
    }
    if item.file_path and Path(item.file_path).exists():
        resp["file_url"] = f"/screenshots/{Path(item.file_path).name}"
    else:
        resp["file_url"] = None
    return resp


# ── API Endpoint'leri — Etiketleme ────────────────────────────────────────────

@app.post("/api/session/start", response_model=SessionStartResponse)
async def start_session(request: Optional[SessionStartRequest] = None):
    """
    Oturumu başlatır:
    1. DataManager ile Excel'i yükler (kolonlar otomatik tespit edilir)
    2. Paylaşılan CaptureEngine'i başlatır
    3. ScreenshotWorker önündeki kayıtları eş zamanlı hazırlamaya başlar
    """
    request = request or SessionStartRequest()
    await _stop_label_flow(close_data=True)

    try:
        state.data_manager = await run_in_threadpool(DataManager, request.source_file)
        total_count = state.data_manager.get_total_count()
        start_index = request.start_index

        all_records = state.data_manager.get_place_records(start_index)
        pending_count = len(all_records)
        if pending_count == 0:
            raise HTTPException(
                status_code=404,
                detail=f"{start_index}. indeksten itibaren işlenecek kayıt bulunamadı.",
            )

        state.session_manager = SessionManager()
        await run_in_threadpool(state.session_manager.start)
        state.screenshot_worker = ScreenshotWorker(state.session_manager)
        await state.screenshot_worker.start(all_records)

        state.current_position = 0
        state.is_started = True
        schema = state.data_manager.schema
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
            source_file=schema.file,
            columns={k: m.column for k, m in schema.matches.items()},
            warnings=schema.warnings,
        )

    except (FileNotFoundError, ColumnDetectionError) as e:
        raise HTTPException(status_code=404 if isinstance(e, FileNotFoundError) else 422, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Oturum başlatma hatası: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Başlatma hatası: {str(e)}")


@app.get("/api/screenshot/next")
async def get_next_screenshot():
    """Sıradaki screenshot'ı döndürür (hazırsa anında)."""
    if not state.is_started or not state.screenshot_worker:
        raise HTTPException(status_code=400, detail="Oturum başlatılmamış. /api/session/start çağrın.")

    if state.screenshot_worker.is_empty():
        return JSONResponse({"status": "completed", "message": "Tüm kayıtlar işlendi!"})

    item = await state.screenshot_worker.get_next()
    if item is None:
        raise HTTPException(status_code=504, detail="Screenshot alınamadı (timeout).")

    state.current_item = item
    state.current_position += 1
    return JSONResponse(_item_response(item, state.screenshot_worker))


@app.post("/api/label")
def save_label(request: LabelRequest):
    """Kullanıcının girdiği kategoriyi kaydeder (threadpool'da; event loop'u bloke etmez)."""
    if not state.data_manager:
        raise HTTPException(status_code=400, detail="DataManager hazır değil.")

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
        return JSONResponse({"status": "saved", "row_index": request.row_index,
                             "category": request.category.strip()})
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
    """Oturumu durdurur, bekleyen kayıtları dosyaya yazar ve kaynakları serbest bırakır."""
    await _stop_label_flow(close_data=True)
    return JSONResponse({"status": "stopped"})


# ── Analiz / izleme uç noktaları ─────────────────────────────────────────────

@app.get("/api/input/files")
async def list_input_analysis():
    """input/ klasöründeki dosyaların sayfa, kolon ve tespit edilen rol bilgileri."""
    return JSONResponse(await run_in_threadpool(scan_input_dir))


@app.get("/api/capture/stats")
async def capture_stats():
    engine = current_engine()
    if not engine:
        return JSONResponse({"status": "not_running"})
    return JSONResponse({"status": "running", "engine": engine.stats(), "store": engine.store.stats()})


@app.get("/api/capture/failures")
async def capture_failures(limit: int = 100):
    engine = current_engine()
    if not engine:
        return JSONResponse({"status": "not_running", "failures": []})
    return JSONResponse({"status": "running", "failures": engine.store.failures(limit)})


# ── Screenshot dosyalarını serve et ──────────────────────────────────────────
_MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


@app.get("/screenshots/{filename}")
async def serve_screenshot(filename: str):
    file_path = (SCREENSHOTS_DIR / filename).resolve()
    if file_path.parent != SCREENSHOTS_DIR.resolve() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Screenshot bulunamadı.")
    return FileResponse(str(file_path), media_type=_MEDIA.get(file_path.suffix.lower(), "application/octet-stream"))


# ── Static files + Ana sayfa ─────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/edit")
async def edit_page():
    return FileResponse(str(STATIC_DIR / "edit.html"))


# -- Duzenle Akisi API Endpoint'leri ---

@app.post("/api/edit/session/start", response_model=SessionStartResponse)
async def edit_start_session(request: Optional[SessionStartRequest] = None):
    request = request or SessionStartRequest()
    await _stop_edit_flow(close_data=True)
    try:
        state.edit_data_manager = await run_in_threadpool(EditDataManager, request.source_file)
        start_index = request.start_index
        all_records = await run_in_threadpool(state.edit_data_manager.get_place_records, start_index)
        pending_count = len(all_records)
        if pending_count == 0:
            raise HTTPException(status_code=404, detail='Kayit bulunamadi.')
        state.edit_session_manager = SessionManager()
        await run_in_threadpool(state.edit_session_manager.start)
        state.edit_screenshot_worker = ScreenshotWorker(state.edit_session_manager)
        await state.edit_screenshot_worker.start(all_records)
        state.edit_current_position = 0
        state.edit_is_started = True
        schema = state.edit_data_manager.schema
        return SessionStartResponse(
            status='started', total=state.edit_data_manager.get_total_count(),
            pending=pending_count, buffer_size=BUFFER_SIZE, start_index=start_index,
            source_file=state.edit_data_manager.source_path.name,
            columns={k: m.column for k, m in schema.matches.items()},
            warnings=schema.warnings,
        )
    except HTTPException:
        raise
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ColumnDetectionError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error(f"Düzenle oturumu başlatma hatası: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/edit/screenshot/next")
async def edit_get_next_screenshot():
    worker = state.edit_screenshot_worker
    if not state.edit_is_started or not worker:
        raise HTTPException(status_code=400, detail='Oturum baslatilmamis.')
    if worker.is_empty():
        return JSONResponse({'status': 'completed', 'message': 'Tum kayitlar islendi!'})
    item = await worker.get_next()
    if item is None:
        raise HTTPException(status_code=504, detail='Screenshot alinamadi.')
    state.edit_current_item = item
    state.edit_current_position += 1
    rec = worker.peek_record(item.position)
    nxt = worker.peek_record(item.position + 1)
    resp = _item_response(item, worker)
    resp['kontrol_value'] = rec.status_value if rec else ''
    resp['next_name'] = nxt.name if nxt else None
    return JSONResponse(resp)


@app.post("/api/edit/action")
def edit_action(request: EditActionRequest):
    if not state.edit_data_manager:
        raise HTTPException(status_code=400, detail='EditDataManager hazir degil.')
    action = request.action.strip().lower()
    if action not in ('delete', 'keep', 'skip'):
        raise HTTPException(status_code=422, detail='Gecersiz action.')
    try:
        if action == 'delete':
            state.edit_data_manager.delete_row(request.row_index)
            return JSONResponse({'status': 'deleted', 'row_index': request.row_index})
        elif action == 'skip':
            state.edit_data_manager.skip_row(request.row_index)
            return JSONResponse({'status': 'skipped', 'row_index': request.row_index})
        else:
            state.edit_data_manager.keep_row(request.row_index)
            return JSONResponse({'status': 'kept', 'row_index': request.row_index})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/edit/action/undo")
def edit_undo_action(request: EditUndoRequest):
    if not state.edit_data_manager:
        raise HTTPException(status_code=400, detail='EditDataManager hazir degil.')
    try:
        undone = state.edit_data_manager.undo_action(request.row_index)
        return JSONResponse({'status': 'ok', 'undone_action': undone, 'row_index': request.row_index})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/edit/session/stop")
async def edit_stop_session():
    await _stop_edit_flow(close_data=False)
    return JSONResponse({'status': 'stopped'})


@app.get("/api/edit/status")
async def get_edit_status():
    if not state.edit_is_started:
        return JSONResponse({'status': 'not_started'})
    stats = state.edit_screenshot_worker.get_stats() if state.edit_screenshot_worker else {}
    return JSONResponse({'status': 'running', 'current_position': state.edit_current_position,
                         'pending': state.edit_data_manager.get_pending_count() if state.edit_data_manager else 0,
                         'worker_stats': stats})


@app.get("/api/edit/export")
def edit_export_excel(source_file: Optional[str] = None):
    """Veritabanındaki güncel hali (silinenler hariç) Excel olarak indirir."""
    if source_file is None and state.edit_data_manager:
        source_file = state.edit_data_manager.source_path.name
    fd, tmp = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        export_excel(Path(tmp), source_file)
    except FileNotFoundError as e:
        _cleanup_file(tmp)
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        _cleanup_file(tmp)
        raise HTTPException(status_code=500, detail=str(e))
    stem = Path(source_file).stem if source_file else "duzenle"
    filename = f"{stem}_guncel_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
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
