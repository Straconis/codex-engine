"""Adding books: progress messages, cancelling, empty PDFs, and cleaning up uploaded copies."""
from __future__ import annotations

import threading

import pytest

pymupdf = pytest.importorskip("pymupdf")

from codex_engine import db  # noqa: E402
from codex_engine.ingest import IngestManager  # noqa: E402


def make_pdf(path, pages: int) -> None:
    doc = pymupdf.open()
    for n in range(pages):
        doc.new_page().insert_text((72, 72), f"The goblin warren, level {n + 1}.", fontsize=11)
    doc.save(path)


class Recorder:
    def __init__(self):
        self.events = []
        self.finished = threading.Event()

    def progress(self, p):
        self.events.append(p)
        if p.done:
            self.finished.set()


@pytest.fixture()
def manager(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    conn = db.open_db(db_path)
    db.init_schema(conn)
    conn.close()
    recorder = Recorder()
    duplicates = []
    m = IngestManager(lambda: db.open_db(db_path), recorder.progress, duplicates.append, managed_dir=tmp_path / "uploads")
    return m, recorder, db_path


def test_progress_starts_with_queued_and_ends_added(manager, tmp_path):
    m, rec, _ = manager
    pdf = tmp_path / "warren.pdf"
    make_pdf(pdf, 3)
    m.start(str(pdf))
    assert rec.finished.wait(20)
    assert rec.events[0].stage == "queued"
    last = rec.events[-1]
    assert last.stage == "done" and last.error is None and "3 pages" in last.message
    cleaning = [e for e in rec.events if e.stage == "chunk" and e.current]
    assert [e.current for e in cleaning] == [1, 2, 3]  # cleanup reports every page


def test_cancel_is_reported_as_cancelled_not_failed(manager, tmp_path):
    m, rec, db_path = manager
    pdf = tmp_path / "warren.pdf"
    make_pdf(pdf, 2)
    m.start(str(pdf))
    ingest_id = rec.events[0].id
    try:
        m.cancel(ingest_id)
    except KeyError:
        pytest.skip("the import finished before it could be cancelled")
    assert rec.finished.wait(20)
    last = rec.events[-1]
    if last.stage == "done":
        pytest.skip("the import finished before the cancel was seen")
    assert last.stage == "cancelled" and last.error is None
    conn = db.open_db(db_path)
    try:
        assert db.list_sources(conn) == []
    finally:
        conn.close()


def test_a_pdf_without_pages_is_refused(manager, tmp_path, monkeypatch):
    from codex_engine import ingest

    m, rec, _ = manager
    pdf = tmp_path / "empty.pdf"
    make_pdf(pdf, 1)
    monkeypatch.setattr(ingest, "extract_layouts", lambda path, cancel=None: [])
    m.start(str(pdf))
    assert rec.finished.wait(20)
    assert rec.events[-1].stage == "error" and rec.events[-1].error == "This PDF has no pages."


def test_deleting_a_book_removes_its_uploaded_copy(manager, tmp_path):
    m, _, db_path = manager
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    copy, other = uploads / "warren.pdf", tmp_path / "elsewhere.pdf"
    copy.write_bytes(b"%PDF-1.7")
    other.write_bytes(b"%PDF-1.7")
    conn = db.open_db(db_path)
    try:
        m.discard_managed_file(conn, other)
        assert other.exists()  # not ours to delete
        m.discard_managed_file(conn, copy)
        assert not copy.exists()
    finally:
        conn.close()
