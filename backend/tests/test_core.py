"""Core backend tests. Run from backend/:  python -m pytest -q"""
from __future__ import annotations

import asyncio
import io
import sqlite3

import pytest

from codex_engine import db
from codex_engine.events import EventBroker
from codex_engine.models import ChunkRow
from codex_engine.uploads import store_upload


@pytest.fixture()
def conn(tmp_path):
    c = db.open_db(tmp_path / "test.sqlite3")
    db.init_schema(c)
    yield c
    c.close()


def _chunks(*bodies: str) -> list[ChunkRow]:
    return [ChunkRow(page_num=i + 1, heading=None, body=b, loc=f"p. {i + 1}") for i, b in enumerate(bodies)]


# ---- search -----------------------------------------------------------------

@pytest.mark.parametrize(
    "query",
    ["half-orc", "kenku's", "d&d", "+1 sword", "AC:", '"unterminated', "AND", "NOT OR", "col:thing", "(x", "necro*"],
)
def test_search_never_raises_on_user_input(conn, query):
    db.create_source_with_chunks(conn, "Book", "/b.pdf", "h1", 1, _chunks("A half-orc fighter with a +1 sword."))
    db.search(conn, query)  # must not raise sqlite3.OperationalError


def test_search_matches_punctuated_terms(conn):
    db.create_source_with_chunks(
        conn, "Book", "/b.pdf", "h1", 2,
        _chunks("The half-orc wields a +1 sword.", "A kenku's mimicry is uncanny."),
    )
    assert [r.page_num for r in db.search(conn, "half-orc")] == [1]
    assert [r.page_num for r in db.search(conn, "kenku's")] == [2]
    assert [r.page_num for r in db.search(conn, "mimic*")] == [2]
    assert [r.page_num for r in db.search(conn, '"wields a"')] == [1]


def test_empty_or_punctuation_only_query_returns_nothing(conn):
    db.create_source_with_chunks(conn, "Book", "/b.pdf", "h1", 1, _chunks("text"))
    assert db.search(conn, "") == []
    assert db.search(conn, "&& --") == []


def test_snippet_centres_on_match(conn):
    body = ("filler " * 300) + "beholder eye rays " + ("padding " * 300)
    db.create_source_with_chunks(conn, "Book", "/b.pdf", "h1", 1, _chunks(body))
    (row,) = db.search(conn, "beholder")
    assert "beholder" in row.snippet


def test_disabled_sources_are_not_searched(conn):
    sid = db.create_source_with_chunks(conn, "Book", "/b.pdf", "h1", 1, _chunks("owlbear"))
    db.set_source_enabled(conn, sid, False)
    assert db.search(conn, "owlbear") == []


# ---- ingest writes ----------------------------------------------------------

def test_replace_is_atomic_when_insert_fails(conn):
    old_id = db.create_source_with_chunks(conn, "Old", "/old.pdf", "same", 1, _chunks("original text"))
    bad = _chunks("new text")
    bad[0].body = None  # violates NOT NULL -> insert fails mid-transaction
    with pytest.raises(sqlite3.IntegrityError):
        db.create_source_with_chunks(conn, "New", "/new.pdf", "same", 1, bad, replace_source_id=old_id)
    # The original must survive, and no half-written source may exist.
    assert [s.id for s in db.list_sources(conn)] == [old_id]
    assert len(db.search(conn, "original")) == 1


def test_replace_swaps_source(conn):
    old_id = db.create_source_with_chunks(conn, "Old", "/old.pdf", "same", 1, _chunks("alpha"))
    new_id = db.create_source_with_chunks(conn, "New", "/new.pdf", "same", 1, _chunks("beta"), replace_source_id=old_id)
    assert [s.id for s in db.list_sources(conn)] == [new_id]
    assert db.search(conn, "alpha") == []
    assert len(db.search(conn, "beta")) == 1


def test_new_copy_gets_unique_source_key(conn):
    db.create_source_with_chunks(conn, "A", "/a.pdf", "same", 1, _chunks("x"))
    db.create_source_with_chunks(conn, "B", "/b.pdf", "same", 1, _chunks("x"))
    keys = sorted(s.source_key for s in db.list_sources(conn))
    assert keys == ["same", "same::copy1"]


# ---- uploads ----------------------------------------------------------------

def test_upload_same_name_different_content_does_not_overwrite(tmp_path):
    first = store_upload(tmp_path, "Core Rules.pdf", io.BytesIO(b"%PDF book one"))
    second = store_upload(tmp_path, "Core Rules.pdf", io.BytesIO(b"%PDF book two"))
    assert first != second
    assert first.read_bytes() == b"%PDF book one"
    assert second.name == "Core Rules (2).pdf"


def test_upload_identical_content_reuses_file(tmp_path):
    first = store_upload(tmp_path, "Book.pdf", io.BytesIO(b"%PDF same"))
    again = store_upload(tmp_path, "Book.pdf", io.BytesIO(b"%PDF same"))
    assert first == again
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Book.pdf"]


def test_upload_strips_directories(tmp_path):
    saved = store_upload(tmp_path, "..\\..\\evil\\Book.pdf", io.BytesIO(b"%PDF"))
    assert saved.parent == tmp_path
    assert saved.name == "Book.pdf"


# ---- events -----------------------------------------------------------------

def test_event_broker_fans_out_to_every_subscriber():
    async def scenario():
        broker = EventBroker()
        a = broker.subscribe()
        b = broker.subscribe()
        broker.publish("ingest_progress", {"id": 1})
        await asyncio.sleep(0)
        got_a = await asyncio.wait_for(a[1].get(), 1)
        got_b = await asyncio.wait_for(b[1].get(), 1)
        broker.unsubscribe(a)
        broker.unsubscribe(b)
        return got_a, got_b, broker.subscriber_count

    got_a, got_b, remaining = asyncio.run(scenario())
    assert got_a == got_b == ("ingest_progress", {"id": 1})
    assert remaining == 0


def test_snippet_marks_matched_terms_for_highlighting(conn):
    db.create_source_with_chunks(conn, "Book", "/b.pdf", "h1", 1, _chunks("The beholder fires eye rays."))
    (row,) = db.search(conn, "beholder")
    assert db.MATCH_START + "beholder" + db.MATCH_END in row.snippet


# ---- open PDF at a page -----------------------------------------------------

def test_viewer_commands_pass_the_page(tmp_path):
    from pathlib import Path

    from codex_engine.platforming import viewer_command

    pdf = tmp_path / "Book.pdf"
    acrobat = viewer_command(r"C:\Program Files\Adobe\Acrobat DC\Acrobat\Acrobat.exe", pdf, 120)
    assert acrobat[1:] == ["/A", "page=120", str(pdf)]
    assert viewer_command(r"C:\x\SumatraPDF.exe", pdf, 7)[1:] == ["-page", "7", str(pdf)]
    assert viewer_command(r"C:\x\FoxitPDFReader.exe", pdf, 7)[1:] == [str(pdf), "/A", "page=7"]
    edge = viewer_command(r"C:\x\msedge.exe", pdf, 9)
    assert edge[1].startswith("file:///") and edge[1].endswith("#page=9")
    assert viewer_command(r"C:\x\SomeOtherViewer.exe", pdf, 3) is None
    assert Path(acrobat[0]).name == "Acrobat.exe"


def test_default_viewer_lookup_on_windows():
    import sys

    import pytest

    if not sys.platform.startswith("win"):
        pytest.skip("Windows only")
    from codex_engine.platforming import default_pdf_viewer_windows

    viewer = default_pdf_viewer_windows()
    assert viewer is None or viewer.lower().endswith(".exe")
