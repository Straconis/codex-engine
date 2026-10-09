from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from . import db
from .formatting import FORMATTER_VERSION, PageLayout, first_heading, format_document, layout_from_pymupdf, markdown_to_plain
from .models import ChunkRow, DuplicateDetectedPayload, IngestProgress, PageContent
from .platforming import normalize_pdf_path


def file_title_from_path(path: Path) -> str:
    return path.stem or "Untitled"


def sha256_hex(path: Path, cancel: threading.Event) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            if cancel.is_set():
                raise RuntimeError("Cancelled")
            digest.update(block)
    return digest.hexdigest()


def extract_layouts(path: Path, cancel: threading.Event | None = None) -> list[PageLayout]:
    layouts: list[PageLayout] = []
    with pymupdf.open(path) as doc:
        if doc.needs_pass:
            raise RuntimeError("PDF appears to be encrypted/password-protected. Cannot ingest encrypted PDFs.")
        for page in doc:
            if cancel is not None and cancel.is_set():
                raise RuntimeError("Cancelled")
            layouts.append(layout_from_pymupdf(page))
    return layouts


def build_page_texts(path: Path, cancel: threading.Event | None = None) -> list[str]:
    """Rule-cleaned Markdown for every page of a PDF."""
    return [p.clean_md for p in build_source_content(extract_layouts(path, cancel))[0]]


def build_source_content(
    layouts: list[PageLayout], cancel: threading.Event | None = None, on_page=None
) -> tuple[list[PageContent], list[ChunkRow]]:
    """Deterministic cleanup for a whole document, plus search chunks built from it."""
    cleaned = format_document(layouts)
    pages: list[PageContent] = []
    chunks: list[ChunkRow] = []
    for index, (layout, text) in enumerate(zip(layouts, cleaned), start=1):
        if cancel is not None and cancel.is_set():
            raise RuntimeError("Cancelled")
        pages.append(PageContent(raw_text=layout.raw_text, clean_md=text, clean_version=FORMATTER_VERSION))
        chunks.extend(page_chunks(index, text))
        if on_page:
            on_page(index, len(layouts))
    return pages, chunks


def page_chunks(page_num: int, markdown: str) -> list[ChunkRow]:
    """Search chunks for one page's Markdown."""
    plain = markdown_to_plain(markdown)
    return chunk_text(page_num, first_heading(markdown) or pick_heading_from_text(plain), plain)


def rebuild_source(conn, source_id: int, path: Path) -> None:
    """Re-run extraction + cleanup for an already-ingested book (formatter upgrades)."""
    pages, chunks = build_source_content(extract_layouts(path))
    lost = db.pages_with_user_work_after(conn, source_id, len(pages))
    if lost:
        # The file at this path now has fewer pages (it was replaced by another PDF). Rebuilding
        # would delete the user's edits and AI versions on the missing pages, so don't.
        raise RuntimeError(
            f"The PDF now has {len(pages)} pages, but you edited or AI formatted page {lost[0]}; "
            "the stored pages are kept. Re-add the book to rebuild it."
        )
    # The user's own edits are kept, so search keeps finding what those pages say.
    edits = {p.page_num: p.edited_md for p in db.edited_pages(conn, source_id) if p.page_num <= len(pages)}
    chunks = [c for c in chunks if c.page_num not in edits]
    for page_num, markdown in edits.items():
        chunks.extend(page_chunks(page_num, markdown))
    db.rebuild_source_content(conn, source_id, pages, chunks)


def index_saved_edits(conn) -> None:
    """Make edits saved before 0.3.10 (which weren't searchable) searchable."""
    db.reindex_edits(conn, {(p.source_id, p.page_num): page_chunks(p.page_num, p.edited_md) for p in db.edited_pages(conn)})


def pick_heading_from_text(page_text: str) -> str | None:
    for line in page_text.splitlines():
        text = line.strip()
        if not text or len(text) > 90:
            continue
        if text.lower().startswith("page "):
            continue
        if any(ch.isalpha() for ch in text):
            return text
    return None


def chunk_text(page_num: int, heading: str | None, text: str) -> list[ChunkRow]:
    body = " ".join(text.replace("\r\n", "\n").split()).strip()
    if not body:
        return []

    max_chars = 1200
    overlap_chars = 200
    chunks: list[ChunkRow] = []
    start = 0
    while start < len(body):
        end = min(start + max_chars, len(body))
        chunks.append(ChunkRow(page_num=page_num, heading=heading, body=body[start:end], loc=f"p. {page_num}"))
        if end == len(body):
            break
        start = max(0, end - overlap_chars)
    return chunks


@dataclass
class IngestJob:
    id: int
    cancel: threading.Event = field(default_factory=threading.Event)
    duplicate_choice: str | None = None
    duplicate_event: threading.Event = field(default_factory=threading.Event)


class IngestManager:
    def __init__(self, conn_factory, emit_progress, emit_duplicate, managed_dir: Path | None = None):
        self._conn_factory = conn_factory
        # Files under managed_dir (the uploads folder) are owned by the app and may be
        # cleaned up when an ingest is discarded and nothing references them.
        self._managed_dir = managed_dir.resolve() if managed_dir else None
        self._emit_progress = emit_progress
        self._emit_duplicate = emit_duplicate
        self._next_id = 1
        self._jobs: dict[int, IngestJob] = {}
        self._lock = threading.Lock()

    def start(self, path_text: str) -> int:
        path = normalize_pdf_path(path_text)
        with self._lock:
            ingest_id = self._next_id
            self._next_id += 1
            job = IngestJob(ingest_id)
            self._jobs[ingest_id] = job
        thread = threading.Thread(target=self._worker, args=(job, path), daemon=True)
        thread.start()
        self._progress(ingest_id, "queued", f"Queued (#{ingest_id}). Hashing / validating...", 0, 1)
        return ingest_id

    def cancel(self, ingest_id: int) -> None:
        job = self._jobs.get(ingest_id)
        if not job:
            raise KeyError("No active ingest with that id.")
        job.cancel.set()
        job.duplicate_event.set()

    def resolve_duplicate(self, ingest_id: int, action: str) -> None:
        if action not in {"discard", "replace", "new_copy"}:
            raise ValueError("Invalid action.")
        job = self._jobs.get(ingest_id)
        if not job:
            raise KeyError("No pending duplicate decision for that ingest_id.")
        job.duplicate_choice = action
        job.duplicate_event.set()

    def _progress(self, ingest_id: int, stage: str, message: str, current: int, total: int, done: bool = False, error: str | None = None) -> None:
        self._emit_progress(IngestProgress(id=ingest_id, stage=stage, message=message, current=current, total=total, done=done, error=error))

    def _discard_managed_file(self, conn, path: Path) -> None:
        if not self._managed_dir:
            return
        try:
            if path.resolve().parent != self._managed_dir:
                return
            if db.source_path_in_use(conn, str(path)):
                return
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _worker(self, job: IngestJob, path: Path) -> None:
        try:
            self._progress(job.id, "validate", "Validating PDF...", 0, 1)
            if job.cancel.is_set():
                raise RuntimeError("Cancelled")

            self._progress(job.id, "hash", "Hashing file (sha256)...", 0, 1)
            file_hash = sha256_hex(path, job.cancel)
            conn = self._conn_factory()
            try:
                replace_source_id: int | None = None
                existing = db.get_source_by_hash(conn, file_hash)
                if existing:
                    payload = DuplicateDetectedPayload(
                        ingest_id=job.id,
                        new_path=str(path),
                        new_title=file_title_from_path(path),
                        sha256=file_hash,
                        existing_id=existing.id,
                        existing_title=existing.title,
                        existing_path=existing.path,
                    )
                    self._emit_duplicate(payload)
                    self._progress(job.id, "duplicate", "Duplicate detected. Waiting for your choice...", 0, 1)
                    while not job.duplicate_event.wait(0.2):
                        if job.cancel.is_set():
                            raise RuntimeError("Cancelled")
                    if job.cancel.is_set():
                        raise RuntimeError("Cancelled")
                    if job.duplicate_choice == "discard":
                        self._discard_managed_file(conn, path)
                        self._progress(job.id, "done", "Duplicate detected. Kept original; discarded new ingest.", 1, 1, True)
                        return
                    if job.duplicate_choice == "replace":
                        # Deleted only once the new copy is fully extracted and written.
                        replace_source_id = existing.id

                self._progress(job.id, "extract", "Extracting text with PyMuPDF...", 0, 1)
                layouts = extract_layouts(path, job.cancel)
                page_count = len(layouts)
                total = max(1, page_count)
                self._progress(job.id, "chunk", "Cleaning up layout and chunking pages...", 0, total)
                pages, all_chunks = build_source_content(
                    layouts,
                    job.cancel,
                    lambda index, n: self._progress(job.id, "chunk", f"Chunking page {index}/{n}...", index, n),
                )

                if job.cancel.is_set():
                    raise RuntimeError("Cancelled")
                self._progress(job.id, "db", f"Writing {len(all_chunks)} chunks to database...", 0, 1)
                db.create_source_with_chunks(
                    conn,
                    file_title_from_path(path),
                    str(path),
                    file_hash,
                    page_count,
                    all_chunks,
                    replace_source_id=replace_source_id,
                    page_rows=pages,
                )
                self._progress(job.id, "done", f"Ingest complete. Pages: {page_count} - Chunks: {len(all_chunks)}", 1, 1, True)
            finally:
                conn.close()
        except Exception as exc:
            self._progress(job.id, "error", "Ingest failed", 0, 0, True, str(exc))
        finally:
            time.sleep(0.1)
            self._jobs.pop(job.id, None)
