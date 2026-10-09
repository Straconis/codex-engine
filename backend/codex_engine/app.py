from __future__ import annotations

import asyncio
import hmac
import json
import os
import sqlite3
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import ai_format, db
from .config import APP_VERSION
from .events import EventBroker
from .formatting import FORMATTER_VERSION
from .ingest import IngestManager, index_saved_edits, page_chunks, rebuild_source
from .models import AIFormatArgs, EditPageArgs, OpenPdfArgs, PullModelArgs, ResolveDuplicateArgs, SetEnabledArgs, StartIngestArgs
from .ollama_manager import OLLAMA_SETTINGS, OllamaManager
from .platforming import app_data_dir, database_path, open_file_at_page
from .settings import SettingsStore
from .uploads import store_upload
from .updater.update_client import can_apply_updates, check_for_update, cleanup_update_files, download_installer, launch_updater

# Every state-changing request must carry this header. A custom header forces a CORS
# preflight, so random web pages open in the user's browser can't fire "simple"
# cross-site POSTs (e.g. multipart uploads) at the local API.
CLIENT_HEADER = "x-codex-engine-client"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
HEADER_EXEMPT_PATHS = {"/api/shutdown"}  # protected by its own secret token


class RequireClientHeaderMiddleware:
    """Pure ASGI (not BaseHTTPMiddleware) so SSE streaming/disconnects aren't affected."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS and scope["path"] not in HEADER_EXEMPT_PATHS:
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            if headers.get(CLIENT_HEADER) != "1":
                response = JSONResponse({"detail": "Missing Codex Engine client header."}, status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


@asynccontextmanager
async def lifespan(_app):
    # Start the managed Ollama in the background so the first "AI format" is quick.
    # Never blocks startup: AI is optional.
    threading.Thread(target=ollama.ensure_running, daemon=True).start()
    # The downloaded installer and updater copy from a previous update aren't needed anymore.
    threading.Thread(target=cleanup_update_files, daemon=True).start()
    yield
    ollama.stop()


app = FastAPI(title="Codex Engine API", lifespan=lifespan)
# Added first = innermost. CORS is added last so it wraps everything, including 403s.
app.add_middleware(RequireClientHeaderMiddleware)
# Blocks DNS-rebinding attacks (evil.example resolving to 127.0.0.1).
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
# Only the Vite dev server needs this: the desktop app loads its UI from file://, and
# Electron doesn't apply CORS to file:// pages.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:1420", "http://127.0.0.1:1420", "http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-Codex-Engine-Client"],
)


broker = EventBroker()
settings_store = SettingsStore()
ollama = OllamaManager(settings_store, state_dir=app_data_dir(), log_dir=app_data_dir() / "logs", publish=broker.publish)

_schema_lock = threading.Lock()
_schema_ready = False


def _conn():
    global _schema_ready
    conn = db.open_db(database_path())
    if not _schema_ready:
        with _schema_lock:
            if not _schema_ready:
                db.init_schema(conn)
                if db.schema_step(conn) < db.EDITS_INDEXED:
                    index_saved_edits(conn)
                _schema_ready = True
    return conn


def _emit_progress(progress):
    broker.publish("ingest_progress", progress.model_dump())


def _emit_duplicate(payload):
    broker.publish("duplicate_detected", payload.model_dump())


def _authorized_shutdown(token: str | None) -> bool:
    expected = os.environ.get("CODEX_ENGINE_SHUTDOWN_TOKEN")
    return bool(expected) and token is not None and hmac.compare_digest(token.encode(), expected.encode())


def stop_and_exit() -> None:
    """Stop the managed Ollama, then end the process at once."""
    ollama.stop()  # os._exit skips cleanup, and a managed Ollama must not outlive the app
    os._exit(0)


def _uploads_dir() -> Path:
    path = app_data_dir() / "uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


ingests = IngestManager(_conn, _emit_progress, _emit_duplicate, managed_dir=_uploads_dir())


@app.get("/api/health")
def health():
    return {"ok": True, "app": "Codex Engine", "version": APP_VERSION, "db": str(database_path())}


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
async def events(request: Request):
    sub = broker.subscribe()
    _, q = sub

    async def stream():
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    name, payload = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    # Keepalive comment; also lets us notice dead clients.
                    yield ": keepalive\n\n"
                    continue
                yield f"event: {name}\ndata: {json.dumps(payload)}\n\n"
        finally:
            broker.unsubscribe(sub)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/shutdown")
def shutdown(x_codex_engine_shutdown_token: str | None = Header(default=None)):
    if not _authorized_shutdown(x_codex_engine_shutdown_token):
        raise HTTPException(status_code=403, detail="Shutdown is not authorized.")

    threading.Timer(0.25, stop_and_exit).start()
    return {"ok": True}


@app.get("/api/update/check")
def check_update():
    try:
        return check_for_update()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


_update_lock = threading.Lock()


@app.post("/api/update/apply")
def apply_update(payload: dict | None = None):
    # Never trust an installer URL handed in by the client: re-resolve it from GitHub
    # here. The payload is accepted only for backwards compatibility and ignored.
    if not can_apply_updates():
        raise HTTPException(status_code=400, detail="Automatic updates are only available in the Windows app. Download the new version from GitHub.")
    if not _update_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="An update is already being downloaded.")
    try:
        update = check_for_update()
        if update.get("status") != "update_available":
            return update
        installer_path = download_installer(update)
        launch_updater(installer_path)
        return {**update, "status": "updater_launched", "installer_path": installer_path}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        _update_lock.release()


@app.get("/api/sources")
def list_sources():
    conn = _conn()
    try:
        return [row.model_dump() for row in db.list_sources(conn)]
    finally:
        conn.close()


@app.patch("/api/sources/{source_id}/enabled")
def set_source_enabled(source_id: int, args: SetEnabledArgs):
    conn = _conn()
    try:
        if not db.set_source_enabled(conn, source_id, args.enabled):
            raise HTTPException(status_code=404, detail="Source not found.")
        return {"ok": True}
    finally:
        conn.close()


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: int):
    conn = _conn()
    try:
        if not db.delete_source(conn, source_id):
            raise HTTPException(status_code=404, detail="Source not found.")
        return {"ok": True}
    finally:
        conn.close()


_rebuild_locks: dict[int, threading.Lock] = {}
_rebuild_locks_guard = threading.Lock()
_failed_rebuilds: dict[int, tuple[str, int]] = {}  # source id -> (path, mtime) of a rebuild that failed


def _ensure_pages_current(conn, source) -> None:
    """Lazy upgrade: build or rebuild a book's pages + search chunks from its PDF.

    Books ingested before the reader existed have no pages; books cleaned by an older
    formatter get re-cleaned. If the PDF is gone we keep whatever is stored.
    """
    version = db.oldest_clean_version(conn, source.id)
    if version is not None and version >= FORMATTER_VERSION:
        return
    with _rebuild_locks_guard:
        lock = _rebuild_locks.setdefault(source.id, threading.Lock())
    with lock:
        version = db.oldest_clean_version(conn, source.id)  # another request may have finished it
        if version is not None and version >= FORMATTER_VERSION:
            return
        path = Path(source.path)
        if not path.is_file():
            if version is None:
                raise HTTPException(status_code=404, detail=f"The original PDF is missing, so this page can't be shown: {source.path}")
            return
        attempt = (str(path), path.stat().st_mtime_ns)
        if _failed_rebuilds.get(source.id) == attempt and version is not None:
            return  # failed on this exact file before; keep the stored pages until it changes
        try:
            rebuild_source(conn, source.id, path)
        except Exception as exc:
            _failed_rebuilds[source.id] = attempt
            print(f"Rebuilding pages of source {source.id} from {path} failed: {exc}", file=sys.stderr)
            if version is None:
                raise HTTPException(status_code=400, detail=f"Could not read the PDF to build pages: {exc}") from exc
        else:
            _failed_rebuilds.pop(source.id, None)


def _page_or_404(conn, source_id: int, page_num: int):
    """The page as stored now; 404 if its book was deleted meanwhile (e.g. during AI formatting)."""
    page = db.get_page(conn, source_id, page_num)
    if not page:
        raise HTTPException(status_code=404, detail="This book was removed from the library.")
    return page


def _load_page(conn, source_id: int, page_num: int):
    source = db.get_source(conn, source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found.")
    _ensure_pages_current(conn, source)
    source = db.get_source(conn, source_id)  # page count may change on rebuild
    page = db.get_page(conn, source_id, page_num)
    if not page:
        raise HTTPException(status_code=404, detail=f"Page {page_num} is out of range (1-{source.pages}).")
    return source, page


def _ai_outdated(page) -> str | None:
    """Why a page's saved AI version can't be shown as current, or None if it can (or there is none).

    "source_changed": the cleaned text changed since. "failed_check": it no longer passes the
    current (stricter) faithfulness check.
    """
    if not page.ai_md:
        return None
    if page.ai_source_hash != ai_format.content_hash(page.clean_md):
        return "source_changed"
    if not ai_format.is_faithful(page.clean_md, page.ai_md):
        return "failed_check"
    return None


def _page_response(source, page, pending: dict | None = None):
    """Page plus what the reader should show: the user's edit, else AI, else cleaned, else raw.

    pending is set when some sections still failed after the automatic retries and the user
    needs to choose: keep retrying, use the cleaned text for them, or edit the page by hand.
    """
    # ai_check_failed tells the reader why it's outdated, so it can explain that case.
    outdated = _ai_outdated(page)
    ai_stale = outdated is not None
    ai_check_failed = outdated == "failed_check"
    if page.edited_md is not None:
        best = "edited"
    elif page.ai_md and not ai_stale:
        best = "ai"
    elif page.clean_md.strip() or not (page.raw_text or "").strip():
        best = "clean"
    else:
        best = "raw"
    return {
        **page.model_dump(),
        "ai_stale": ai_stale,
        "ai_check_failed": ai_check_failed,
        "best": best,
        "pending": pending,
        "page_count": source.pages,
        "title": source.title,
        "path": source.path,
    }


# Running AI format jobs, (source_id, page_num) -> cancel flag.
_ai_jobs: dict[tuple[int, int], threading.Event] = {}
_ai_jobs_lock = threading.Lock()


class _LiveProgress:
    """Streams AI formatting progress and plain-language log lines to the UI (SSE).

    `ai_format_progress`: part n of total, attempt, and how much of the current part's
    reply has been written (throttled). `ai_format_log`: one line per step.
    """

    def __init__(self, source_id: int, page_num: int, max_attempts: int):
        self.base = {"source_id": source_id, "page_num": page_num, "max_attempts": max_attempts}
        self.state = {"done": 0, "total": 0, "attempt": 0, "written": 0, "expected": 0}
        self._last = 0.0

    def _publish(self) -> None:
        broker.publish("ai_format_progress", {**self.base, **self.state})

    def progress(self, done: int, total: int, attempt: int) -> None:
        self.state.update(done=done, total=total, attempt=attempt)
        self._publish()

    def written(self, chars: int, expected: int) -> None:
        self.state.update(written=chars, expected=expected)
        now = time.monotonic()
        if chars == 0 or now - self._last >= 0.3:
            self._last = now
            self._publish()

    def log(self, message: str) -> None:
        broker.publish("ai_format_log", {**self.base, "message": message, "at": time.time()})


class _DbCache:
    def __init__(self, conn):
        self.conn = conn

    def get(self, key):
        return db.ai_cache_get(self.conn, key)

    def put(self, key, model, output):
        db.ai_cache_put(self.conn, key, model, output)


@app.get("/api/sources/{source_id}/pages/{page_num}")
def get_page(source_id: int, page_num: int):
    conn = _conn()
    try:
        return _page_response(*_load_page(conn, source_id, page_num))
    finally:
        conn.close()


@app.get("/api/sources/{source_id}/ai-check")
def check_book_ai(source_id: int):
    """Every page of a book whose saved AI version is outdated, and why (see _ai_outdated)."""
    conn = _conn()
    try:
        source = db.get_source(conn, source_id)
        if not source:
            raise HTTPException(status_code=404, detail="Source not found.")
        _ensure_pages_current(conn, source)
        pages = db.pages_with_ai(conn, source_id)
        outdated = [{"page_num": p.page_num, "reason": reason} for p in pages if (reason := _ai_outdated(p))]
        return {"source_id": source_id, "title": source.title, "ai_pages": len(pages), "outdated": outdated}
    finally:
        conn.close()


@app.get("/api/ai/status")
def ai_status():
    return ollama.status()


@app.get("/api/settings")
def get_settings():
    return {"settings": settings_store.load().model_dump(), "path": str(settings_store.path)}


@app.put("/api/settings")
def update_settings(changes: dict = Body(...)):
    before = settings_store.load()
    try:
        saved = settings_store.update(changes)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'settings'}: {e['msg'].removeprefix('Value error, ')}" for e in exc.errors())
        raise HTTPException(status_code=422, detail=problems) from exc
    # Restart Ollama only when a setting it runs with changed: a restart cuts off any
    # formatting or model download in progress. Stop the old one before replying, so
    # nothing that follows can reach it mid-restart; the new one starts in the background.
    if any(getattr(before, key) != getattr(saved, key) for key in OLLAMA_SETTINGS):
        ollama.apply(start=False)
        threading.Thread(target=ollama.ensure_running, daemon=True).start()
    return {"settings": saved.model_dump(), "path": str(settings_store.path)}


@app.post("/api/ai/models/pull")
def pull_model(args: PullModelArgs):
    """Download a model into the configured folder. Progress arrives as `model_pull` events."""
    try:
        return ollama.pull(args.model).__dict__
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="Model names look like 'qwen2.5:1.5b'.") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/sources/{source_id}/pages/{page_num}/ai-format")
def ai_format_page(source_id: int, page_num: int, args: AIFormatArgs | None = None):
    """Format one page with the local model (each section: 1 try + automatic retries).

    503 = AI unavailable (no Ollama / no model): the app keeps working without it.
    200 with `pending` = some sections still failed after every retry; nothing is saved yet and
        the reader asks the user. Calling again retries only those (accepted ones are cached);
        use_clean_for_failed=true keeps their cleaned text instead.
    200 with ai_error = nothing could be formatted and the user chose the cleaned text.
    """
    args = args or AIFormatArgs()
    conn = _conn()
    try:
        source, page = _load_page(conn, source_id, page_num)
        ollama.ensure_running()
        state = ollama.status()
        if not state["available"]:
            raise HTTPException(status_code=503, detail=f"AI formatting is unavailable: {state['error']}")
        config = ollama.config()
        live = _LiveProgress(source_id, page_num, config.max_attempts)
        cancel = threading.Event()
        with _ai_jobs_lock:
            # A second run would replace the first one's Cancel flag (and then remove its own).
            if (source_id, page_num) in _ai_jobs:
                raise HTTPException(status_code=409, detail="This page is already being formatted.")
            _ai_jobs[(source_id, page_num)] = cancel
        try:
            result = ai_format.format_markdown(
                page.clean_md,
                config=config,
                cache=_DbCache(conn),
                model=args.model,
                force=args.force,
                use_clean_for_failed=args.use_clean_for_failed,
                on_progress=live.progress,
                on_written=live.written,
                on_log=live.log,
                should_stop=cancel.is_set,
            )
        except ai_format.AIUnavailable as exc:
            raise HTTPException(status_code=503, detail=f"AI formatting is unavailable: {exc}") from exc
        except ai_format.AICancelled as exc:
            live.log("Cancelled. Nothing on the page was changed; parts already accepted are kept for next time.")
            raise HTTPException(status_code=409, detail="Cancelled. Nothing on the page was changed.") from exc
        finally:
            with _ai_jobs_lock:
                _ai_jobs.pop((source_id, page_num), None)
        if result.failed and not args.use_clean_for_failed:
            pending = {
                "sections": result.sections,
                "formatted": result.formatted,
                "failed": [f.__dict__ for f in result.failed],
                # Accepted sections + cleaned text for the failed ones: the starting point if the
                # user chooses to edit the page by hand.
                "draft_md": result.markdown,
            }
            return _page_response(source, _page_or_404(conn, source_id, page_num), pending)
        changes = None
        if result.formatted:
            db.set_page_ai(
                conn, source_id, page_num, result.markdown, result.model, ai_format.content_hash(page.clean_md), result.note
            )
            changes = ai_format.describe_changes(page.clean_md, result.markdown)
            live.log(changes["summary"])
        else:
            db.set_page_ai_error(conn, source_id, page_num, "The AI couldn't format this page without changing it; the cleaned text is kept.")
        response = _page_response(source, _page_or_404(conn, source_id, page_num))
        response["ai_changes"] = changes  # what this run changed, for the progress window
        return response
    finally:
        conn.close()


@app.post("/api/sources/{source_id}/pages/{page_num}/ai-format/cancel")
def cancel_ai_format(source_id: int, page_num: int):
    """Stop a running AI format of this page (the model stops generating too)."""
    with _ai_jobs_lock:
        job = _ai_jobs.get((source_id, page_num))
    if job:
        job.set()
    return {"ok": bool(job)}


@app.put("/api/sources/{source_id}/pages/{page_num}/edit")
def save_page_edit(source_id: int, page_num: int, args: EditPageArgs):
    """The user's own corrected version of a page. Raw, cleaned and AI versions are kept."""
    conn = _conn()
    try:
        source, _ = _load_page(conn, source_id, page_num)
        db.set_page_edit(conn, source_id, page_num, args.markdown, page_chunks(page_num, args.markdown))
        return _page_response(source, _page_or_404(conn, source_id, page_num))
    finally:
        conn.close()


@app.delete("/api/sources/{source_id}/pages/{page_num}/edit")
def revert_page_edit(source_id: int, page_num: int):
    conn = _conn()
    try:
        source, page = _load_page(conn, source_id, page_num)
        db.set_page_edit(conn, source_id, page_num, None, page_chunks(page_num, page.clean_md or ""))
        return _page_response(source, _page_or_404(conn, source_id, page_num))
    finally:
        conn.close()


@app.get("/api/search")
def search(query: str):
    conn = _conn()
    try:
        return [row.model_dump() for row in db.search(conn, query, 50)]
    except sqlite3.OperationalError as exc:
        raise HTTPException(status_code=400, detail=f"Search failed: {exc}") from exc
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
    head = file.file.read(1024)
    file.file.seek(0)
    if b"%PDF-" not in head:
        raise HTTPException(status_code=400, detail=f"{file.filename} isn't a PDF (it doesn't start like one).")
    try:
        target = store_upload(_uploads_dir(), file.filename, file.file)
    except OSError as exc:
        raise HTTPException(status_code=507, detail=f"Couldn't save the uploaded PDF: {exc.strerror or exc}") from exc
    return {"id": ingests.start(str(target)), "path": str(target)}


@app.post("/api/ingest/{ingest_id}/cancel")
def cancel_ingest(ingest_id: int):
    try:
        ingests.cancel(ingest_id)
        return {"ok": True}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc


@app.post("/api/ingest/duplicate")
def resolve_duplicate(args: ResolveDuplicateArgs):
    try:
        ingests.resolve_duplicate(args.ingest_id, args.action)
        return {"ok": True}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/open-pdf")
def open_pdf(args: OpenPdfArgs):
    try:
        open_file_at_page(args.path, args.page)
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
