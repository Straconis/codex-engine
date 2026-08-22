from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from . import db
from .models import ChunkRow, DuplicateDetectedPayload, IngestProgress
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


def extract_pages(path: Path, cancel: threading.Event) -> list[str]:
    pages: list[str] = []
    with pymupdf.open(path) as doc:
        if doc.needs_pass:
            raise RuntimeError("PDF appears to be encrypted/password-protected. Cannot ingest encrypted PDFs.")
        for page in doc:
            if cancel.is_set():
                raise RuntimeError("Cancelled")
            pages.append(page.get_text("text"))
    return pages


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
    def __init__(self, conn_factory, emit_progress, emit_duplicate):
        self._conn_factory = conn_factory
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

    def _worker(self, job: IngestJob, path: Path) -> None:
        try:
            self._progress(job.id, "validate", "Validating PDF...", 0, 1)
            if job.cancel.is_set():
                raise RuntimeError("Cancelled")

            self._progress(job.id, "hash", "Hashing file (sha256)...", 0, 1)
            file_hash = sha256_hex(path, job.cancel)
            conn = self._conn_factory()
            try:
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
                        self._progress(job.id, "done", "Duplicate detected. Kept original; discarded new ingest.", 1, 1, True)
                        return
                    if job.duplicate_choice == "replace":
                        db.delete_source(conn, existing.id)

                self._progress(job.id, "extract", "Extracting text with PyMuPDF...", 0, 1)
                pages = extract_pages(path, job.cancel)
                page_count = len(pages)
                source_id = db.create_source(conn, file_title_from_path(path), str(path), file_hash, page_count, True)

                all_chunks: list[ChunkRow] = []
                self._progress(job.id, "chunk", "Chunking pages...", 0, max(1, page_count))
                for index, text in enumerate(pages, start=1):
                    if job.cancel.is_set():
                        db.delete_source(conn, source_id)
                        raise RuntimeError("Cancelled")
                    all_chunks.extend(chunk_text(index, pick_heading_from_text(text), text))
                    self._progress(job.id, "chunk", f"Chunking page {index}/{max(1, page_count)}...", index, max(1, page_count))

                self._progress(job.id, "db", f"Writing {len(all_chunks)} chunks to database...", 0, 1)
                db.insert_chunks(conn, source_id, all_chunks)
                self._progress(job.id, "done", f"Ingest complete. Pages: {page_count} - Chunks: {len(all_chunks)}", 1, 1, True)
            finally:
                conn.close()
        except Exception as exc:
            self._progress(job.id, "error", "Ingest failed", 0, 0, True, str(exc))
        finally:
            time.sleep(0.1)
            self._jobs.pop(job.id, None)

