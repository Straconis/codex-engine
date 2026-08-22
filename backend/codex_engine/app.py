from __future__ import annotations

import json
import os
import queue
import shutil
import sys
import threading
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from . import db
from .config import APP_VERSION
from .ingest import IngestManager
from .models import OpenPdfArgs, ResolveDuplicateArgs, StartIngestArgs
from .platforming import app_data_dir, database_path, open_file_at_page
from .updater.update_client import check_for_update, download_installer, launch_updater

app = FastAPI(title="Codex Engine API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:1420", "http://127.0.0.1:1420", "http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_events: queue.Queue[tuple[str, dict]] = queue.Queue()


def _conn():
    conn = db.open_db(database_path())
    db.init_schema(conn)
    return conn


def _emit_progress(progress):
    _events.put(("ingest_progress", progress.model_dump()))


def _emit_duplicate(payload):
    _events.put(("duplicate_detected", payload.model_dump()))


def _authorized_shutdown(token: str | None) -> bool:
    expected = os.environ.get("CODEX_ENGINE_SHUTDOWN_TOKEN")
    return bool(expected) and token == expected


ingests = IngestManager(_conn, _emit_progress, _emit_duplicate)


@app.get("/api/health")
def health():
    return {"ok": True, "db": str(database_path())}

@app.get("/api/version")
def version():
    updater_path = os.environ.get("CODEX_ENGINE_UPDATER_EXE")
    return {
        "app": "Codex Engine",
        "backend_version": APP_VERSION,
        "updater_version": APP_VERSION,
        "platform": sys.platform,
        "updater_present": bool(updater_path and Path(updater_path).is_file()),
        "updater_path": updater_path,
    }


@app.get("/api/events")
def events():
    def stream():
        while True:
            name, payload = _events.get()
            yield f"event: {name}\n"
            yield f"data: {json.dumps(payload)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/api/shutdown")
def shutdown(x_codex_engine_shutdown_token: str | None = Header(default=None)):
    if not _authorized_shutdown(x_codex_engine_shutdown_token):
        raise HTTPException(status_code=403, detail="Shutdown is not authorized.")

    def stop_process():
        os._exit(0)

    threading.Timer(0.25, stop_process).start()
    return {"ok": True}


@app.get("/api/update/check")
def check_update():
    try:
        return check_for_update()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/update/apply")
def apply_update(payload: dict):
    try:
        update = payload.get("update") or check_for_update()
        if update.get("status") != "update_available":
            return update
        installer_path = download_installer(update)
        launch_updater(installer_path)
        return {**update, "status": "updater_launched", "installer_path": installer_path}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/sources")
def list_sources():
    conn = _conn()
    try:
        return [row.model_dump() for row in db.list_sources(conn)]
    finally:
        conn.close()


@app.patch("/api/sources/{source_id}/enabled")
def set_source_enabled(source_id: int, payload: dict):
    conn = _conn()
    try:
        db.set_source_enabled(conn, source_id, bool(payload.get("enabled")))
        return {"ok": True}
    finally:
        conn.close()


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: int):
    conn = _conn()
    try:
        db.delete_source(conn, source_id)
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/search")
def search(query: str):
    conn = _conn()
    try:
        return [row.model_dump() for row in db.search(conn, query, 50)]
    finally:
        conn.close()


@app.post("/api/ingest")
def start_ingest(args: StartIngestArgs):
    try:
        return {"id": ingests.start(args.path)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/ingest/upload")
def upload_and_ingest(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Upload must be a PDF file.")
    upload_dir = app_data_dir() / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    target = upload_dir / Path(file.filename).name
    with target.open("wb") as handle:
        shutil.copyfileobj(file.file, handle)
    return {"id": ingests.start(str(target)), "path": str(target)}


@app.post("/api/ingest/{ingest_id}/cancel")
def cancel_ingest(ingest_id: int):
    try:
        ingests.cancel(ingest_id)
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/ingest/duplicate")
def resolve_duplicate(args: ResolveDuplicateArgs):
    try:
        ingests.resolve_duplicate(args.ingest_id, args.action)
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/open-pdf")
def open_pdf(args: OpenPdfArgs):
    try:
        open_file_at_page(args.path, args.page)
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

