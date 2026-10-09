"""Page storage, schema migration, lazy upgrade of existing books, and reader API fallbacks."""
from __future__ import annotations

import sqlite3

import pytest

from codex_engine import ai_format, db
from codex_engine.formatting import FORMATTER_VERSION
from codex_engine.models import ChunkRow, PageContent

pymupdf = pytest.importorskip("pymupdf")
pytest.importorskip("httpx")

HEADERS = {"X-Codex-Engine-Client": "1"}


def pages_of(*cleaned: str, version: int = FORMATTER_VERSION) -> list[PageContent]:
    return [PageContent(raw_text=f"RAW {c}", clean_md=c, clean_version=version) for c in cleaned]


def make_pdf(path, *texts: str) -> None:
    doc = pymupdf.open()
    for text in texts:
        doc.new_page(width=600, height=800).insert_text((50, 200), text, fontsize=11)
    doc.save(path)
    doc.close()


# ---- schema -----------------------------------------------------------------------

def test_old_pages_table_is_migrated_in_place(tmp_path):
    path = tmp_path / "old.sqlite3"
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE sources (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, path TEXT NOT NULL,
          sha256 TEXT NOT NULL, pages INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1, source_key TEXT NOT NULL);
        CREATE TABLE pages (source_id INTEGER NOT NULL, page_num INTEGER NOT NULL, clean_md TEXT NOT NULL,
          ai_md TEXT, ai_model TEXT, PRIMARY KEY (source_id, page_num));
        INSERT INTO sources (title, path, sha256, pages, source_key) VALUES ('B', '/b.pdf', 'h', 1, 'h');
        INSERT INTO pages VALUES (1, 1, 'kept text', 'kept ai', 'm');
        """
    )
    raw.commit()
    raw.close()

    conn = db.open_db(path)
    db.init_schema(conn)
    db.init_schema(conn)  # idempotent
    page = db.get_page(conn, 1, 1)
    assert (page.clean_md, page.ai_md, page.clean_version, page.raw_text) == ("kept text", "kept ai", 0, None)
    assert db.oldest_clean_version(conn, 1) == 0  # -> will be rebuilt lazily
    conn.close()


@pytest.fixture()
def conn(tmp_path):
    c = db.open_db(tmp_path / "t.sqlite3")
    db.init_schema(c)
    yield c
    c.close()


def test_pages_store_raw_clean_and_version_and_cascade(conn):
    sid = db.create_source_with_chunks(conn, "B", "/b.pdf", "h", 2, [], page_rows=pages_of("# One", "Two"))
    page = db.get_page(conn, sid, 1)
    assert (page.raw_text, page.clean_md, page.clean_version) == ("RAW # One", "# One", FORMATTER_VERSION)
    db.ai_cache_put(conn, "k", "m", "cached")
    db.delete_source(conn, sid)
    assert db.get_page(conn, sid, 1) is None
    assert db.ai_cache_get(conn, "k") == "cached"  # cache outlives the book (re-ingest reuses it)


def test_rebuild_replaces_chunks_and_keeps_ai_output(conn):
    sid = db.create_source_with_chunks(
        conn, "B", "/b.pdf", "h", 1, [ChunkRow(page_num=1, heading=None, body="flattened oldtext", loc="p. 1")],
        page_rows=pages_of("old", version=0),
    )
    db.set_page_ai(conn, sid, 1, "# old", "m", ai_format.content_hash("old"))
    db.rebuild_source_content(
        conn, sid, pages_of("new clean"), [ChunkRow(page_num=1, heading=None, body="new clean", loc="p. 1")]
    )
    page = db.get_page(conn, sid, 1)
    assert page.clean_md == "new clean" and page.ai_md == "# old" and page.clean_version == FORMATTER_VERSION
    assert db.search(conn, "oldtext") == [] and len(db.search(conn, "clean")) == 1


# ---- API ---------------------------------------------------------------------------

class FakeClient:
    def __init__(self, respond=lambda t: "# " + t, models=("qwen2.5:1.5b",)):
        self.respond, self.models, self.calls = respond, list(models), 0

    def list_models(self):
        return self.models

    def chat(self, model, messages, max_tokens=None, temperature=None, on_text=None, should_stop=None):
        if should_stop and should_stop():
            from codex_engine.ai_format import AICancelled

            raise AICancelled("Cancelled")
        self.calls += 1
        reply = self.respond(messages[1]["content"])
        if on_text:
            on_text(len(reply))
        return reply


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_ENGINE_DB", str(tmp_path / "api.sqlite3"))
    from fastapi.testclient import TestClient

    from codex_engine import app as app_module

    app_module._schema_ready = False
    fake = FakeClient()
    monkeypatch.setattr(ai_format, "OllamaClient", lambda config: fake)
    return TestClient(app_module.app), app_module, fake


def add_source(app_module, path, *cleaned, version=FORMATTER_VERSION, with_pages=True):
    conn = app_module._conn()
    try:
        return db.create_source_with_chunks(
            conn, "Book", str(path), "h", len(cleaned), [],
            page_rows=pages_of(*cleaned, version=version) if with_pages else None,
        )
    finally:
        conn.close()


SECTION = "The rain fell in torrents, except at occasional intervals, when it was checked by a violent gust of wind."


def test_reader_prefers_ai_then_clean(api, tmp_path):
    client, app_module, fake = api
    sid = add_source(app_module, tmp_path / "b.pdf", SECTION)

    page = client.get(f"/api/sources/{sid}/pages/1").json()
    assert page["best"] == "clean" and page["ai_md"] is None and page["raw_text"] == f"RAW {SECTION}"

    page = client.post(f"/api/sources/{sid}/pages/1/ai-format", json={}, headers=HEADERS).json()
    assert page["best"] == "ai" and page["ai_md"] == "# " + SECTION and page["ai_model"] == "qwen2.5:1.5b"
    assert client.get(f"/api/sources/{sid}/pages/1").json()["best"] == "ai"  # stored


def test_ai_format_uses_cache_and_force_regenerates(api, tmp_path):
    client, app_module, fake = api
    a = add_source(app_module, tmp_path / "a.pdf", SECTION)
    b = add_source(app_module, tmp_path / "b.pdf", SECTION)  # same text in another book
    client.post(f"/api/sources/{a}/pages/1/ai-format", json={}, headers=HEADERS)
    client.post(f"/api/sources/{b}/pages/1/ai-format", json={}, headers=HEADERS)
    assert fake.calls == 1
    client.post(f"/api/sources/{b}/pages/1/ai-format", json={"force": True}, headers=HEADERS)
    assert fake.calls == 2


def test_failing_ai_asks_the_user_and_keeps_previous_ai(api, tmp_path):
    client, app_module, fake = api
    sid = add_source(app_module, tmp_path / "b.pdf", SECTION)
    client.post(f"/api/sources/{sid}/pages/1/ai-format", json={}, headers=HEADERS)

    fake.respond = lambda t: "A summary of the weather."
    before = fake.calls
    page = client.post(f"/api/sources/{sid}/pages/1/ai-format", json={"force": True}, headers=HEADERS).json()
    assert fake.calls - before == 4  # 1 try + 3 automatic retries
    assert page["pending"]["failed"][0]["attempts"] == 4 and "length" in page["pending"]["failed"][0]["reason"]
    assert page["ai_md"] == "# " + SECTION and page["best"] == "ai"  # nothing saved yet; earlier output intact

    # "Use cleaned text": the earlier accepted AI version (cached) is kept rather than thrown away.
    page = client.post(f"/api/sources/{sid}/pages/1/ai-format", json={"use_clean_for_failed": True}, headers=HEADERS).json()
    assert page["pending"] is None and page["ai_md"] == "# " + SECTION and page["best"] == "ai"


def test_nothing_formatted_and_user_keeps_cleaned_text(api, tmp_path):
    client, app_module, fake = api
    sid = add_source(app_module, tmp_path / "b.pdf", SECTION)
    fake.respond = lambda t: "A summary of the weather."
    assert client.post(f"/api/sources/{sid}/pages/1/ai-format", json={}, headers=HEADERS).json()["pending"]
    page = client.post(f"/api/sources/{sid}/pages/1/ai-format", json={"use_clean_for_failed": True}, headers=HEADERS).json()
    assert page["pending"] is None and page["ai_md"] is None and page["best"] == "clean"
    assert "cleaned text is kept" in page["ai_error"]


def test_use_cleaned_text_saves_the_sections_that_did_work(api, tmp_path, monkeypatch):
    client, app_module, fake = api
    monkeypatch.setenv("CODEX_ENGINE_AI_SECTION_CHARS", "120")
    first, second = ("First part " + "alpha " * 20).strip(), ("Second part " + "beta " * 20).strip()
    sid = add_source(app_module, tmp_path / "b.pdf", f"{first}\n\n{second}")
    fake.respond = lambda t: "nope" if t.startswith("Second") else "# " + t

    page = client.post(f"/api/sources/{sid}/pages/1/ai-format", json={}, headers=HEADERS).json()
    assert page["pending"]["formatted"] == 1 and [f["index"] for f in page["pending"]["failed"]] == [2]
    assert page["pending"]["draft_md"] == f"# {first}\n\n{second}"  # starting point for a hand edit
    assert page["ai_md"] is None

    calls = fake.calls
    page = client.post(f"/api/sources/{sid}/pages/1/ai-format", json={"use_clean_for_failed": True}, headers=HEADERS).json()
    assert fake.calls == calls  # no new model calls: the good section is cached, the bad one keeps cleaned text
    assert page["best"] == "ai" and page["ai_md"] == f"# {first}\n\n{second}"
    assert "1 of 2 section(s) kept as cleaned text" in page["ai_error"]


def test_manual_edit_wins_and_can_be_reverted(api, tmp_path):
    client, app_module, fake = api
    sid = add_source(app_module, tmp_path / "b.pdf", SECTION)
    client.post(f"/api/sources/{sid}/pages/1/ai-format", json={}, headers=HEADERS)

    page = client.put(f"/api/sources/{sid}/pages/1/edit", json={"markdown": "## My fixed page"}, headers=HEADERS).json()
    assert page["best"] == "edited" and page["edited_md"] == "## My fixed page" and page["edited_at"]
    assert page["ai_md"] == "# " + SECTION and page["clean_md"] == SECTION  # other versions untouched
    assert client.put(f"/api/sources/{sid}/pages/1/edit", json={"markdown": "x"}).status_code == 403  # CSRF header

    page = client.delete(f"/api/sources/{sid}/pages/1/edit", headers=HEADERS).json()
    assert page["best"] == "ai" and page["edited_md"] is None and page["edited_at"] is None


def test_manual_edit_survives_a_formatter_upgrade(conn):
    sid = db.create_source_with_chunks(conn, "B", "/b.pdf", "h", 1, [], page_rows=pages_of("old", version=0))
    db.set_page_edit(conn, sid, 1, "## Hand-fixed", [])
    db.rebuild_source_content(conn, sid, pages_of("new clean"), [])
    assert db.get_page(conn, sid, 1).edited_md == "## Hand-fixed"


def search_pages(client, query):
    return [r["page_num"] for r in client.get("/api/search", params={"query": query}).json()]


def test_manual_edits_are_searchable_and_revert_restores_the_cleaned_text(api, tmp_path):
    client, app_module, _ = api
    pdf = tmp_path / "b.pdf"
    make_pdf(pdf, "The wyvern nests in the cliffs.", "Second page.")
    sid = add_source(app_module, pdf, "x", "y", with_pages=False)
    client.get(f"/api/sources/{sid}/pages/1")  # builds pages and search chunks
    assert search_pages(client, "wyvern") == [1]

    client.put(f"/api/sources/{sid}/pages/1/edit", json={"markdown": "## Basilisk\nIts gaze turns flesh to stone."}, headers=HEADERS)
    assert search_pages(client, "basilisk") == [1] and search_pages(client, "wyvern") == []
    assert search_pages(client, "second") == [2]  # other pages untouched

    client.delete(f"/api/sources/{sid}/pages/1/edit", headers=HEADERS)
    assert search_pages(client, "wyvern") == [1] and search_pages(client, "basilisk") == []


def test_search_keeps_finding_edits_after_a_formatter_upgrade(api, tmp_path):
    client, app_module, _ = api
    pdf = tmp_path / "b.pdf"
    make_pdf(pdf, "The wyvern nests in the cliffs.")
    sid = add_source(app_module, pdf, "x", with_pages=False)
    client.put(f"/api/sources/{sid}/pages/1/edit", json={"markdown": "The basilisk nests here."}, headers=HEADERS)
    conn = app_module._conn()
    conn.execute("UPDATE pages SET clean_version=0 WHERE source_id=?", (sid,))
    conn.commit()
    conn.close()

    assert client.get(f"/api/sources/{sid}/pages/1").json()["edited_md"] == "The basilisk nests here."  # rebuilt
    assert search_pages(client, "basilisk") == [1] and search_pages(client, "wyvern") == []


def test_edits_saved_before_0_3_10_are_indexed_once_on_upgrade(api, tmp_path):
    client, app_module, _ = api
    sid = add_source(app_module, tmp_path / "b.pdf", "The wyvern nests in the cliffs.")
    conn = app_module._conn()
    conn.execute("INSERT INTO chunks (source_id, page_num, heading, body, loc) VALUES (?,1,NULL,'The wyvern nests in the cliffs.','p. 1')", (sid,))
    conn.execute("UPDATE pages SET edited_md='The basilisk nests here.' WHERE source_id=?", (sid,))  # old-style edit
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()

    app_module._schema_ready = False  # next start
    assert search_pages(client, "basilisk") == [1] and search_pages(client, "wyvern") == []
    conn = app_module._conn()
    assert db.schema_step(conn) == db.EDITS_INDEXED
    conn.close()


def test_ollama_unavailable_is_503_not_500(api, tmp_path, monkeypatch):
    client, app_module, fake = api
    sid = add_source(app_module, tmp_path / "b.pdf", SECTION)
    fake.models = []
    r = client.post(f"/api/sources/{sid}/pages/1/ai-format", json={}, headers=HEADERS)
    assert r.status_code == 503 and "unavailable" in r.json()["detail"]
    status = client.get("/api/ai/status").json()
    assert status["available"] is False and status["error"]
    assert client.get(f"/api/sources/{sid}/pages/1").json()["best"] == "clean"  # reader unaffected


def test_ai_output_goes_stale_when_clean_text_changes(api, tmp_path):
    client, app_module, fake = api
    sid = add_source(app_module, tmp_path / "b.pdf", SECTION)
    client.post(f"/api/sources/{sid}/pages/1/ai-format", json={}, headers=HEADERS)
    conn = app_module._conn()
    conn.execute("UPDATE pages SET clean_md='different now' WHERE source_id=?", (sid,))
    conn.commit()
    conn.close()
    page = client.get(f"/api/sources/{sid}/pages/1").json()
    assert page["ai_stale"] is True and page["ai_check_failed"] is False and page["best"] == "clean"


def test_saved_ai_output_that_fails_the_current_check_is_outdated(api, tmp_path):
    client, app_module, _ = api
    sid = add_source(app_module, tmp_path / "b.pdf", SECTION)
    client.post(f"/api/sources/{sid}/pages/1/ai-format", json={}, headers=HEADERS)
    conn = app_module._conn()
    conn.execute("UPDATE pages SET ai_md = ai_md || ' not' WHERE source_id=?", (sid,))  # saved by an older, looser check
    conn.commit()
    conn.close()
    page = client.get(f"/api/sources/{sid}/pages/1").json()
    assert page["ai_stale"] is True and page["ai_check_failed"] is True and page["best"] == "clean"
    # Reformatting replaces it with output that passes.
    page = client.post(f"/api/sources/{sid}/pages/1/ai-format", json={}, headers=HEADERS).json()
    assert page["ai_check_failed"] is False and page["best"] == "ai"


def test_book_check_lists_outdated_ai_pages_and_why(api, tmp_path):
    client, app_module, _ = api
    sid = add_source(app_module, tmp_path / "b.pdf", SECTION, SECTION, SECTION)
    for page in (1, 2, 3):
        client.post(f"/api/sources/{sid}/pages/{page}/ai-format", json={}, headers=HEADERS)
    conn = app_module._conn()
    conn.execute("UPDATE pages SET ai_md = ai_md || ' not' WHERE source_id=? AND page_num=1", (sid,))
    conn.execute("UPDATE pages SET clean_md = 'different now' WHERE source_id=? AND page_num=3", (sid,))
    conn.commit()
    conn.close()
    report = client.get(f"/api/sources/{sid}/ai-check").json()
    assert report["ai_pages"] == 3
    assert report["outdated"] == [{"page_num": 1, "reason": "failed_check"}, {"page_num": 3, "reason": "source_changed"}]
    client.post(f"/api/sources/{sid}/pages/1/ai-format", json={}, headers=HEADERS)
    assert [p["page_num"] for p in client.get(f"/api/sources/{sid}/ai-check").json()["outdated"]] == [3]
    assert client.get("/api/sources/999/ai-check").status_code == 404


def test_raw_text_is_last_resort(api, tmp_path):
    client, app_module, _ = api
    sid = add_source(app_module, tmp_path / "b.pdf", "")
    page = client.get(f"/api/sources/{sid}/pages/1").json()
    assert page["best"] == "raw" and page["raw_text"] == "RAW "


def test_book_without_pages_is_built_lazily_with_search_upgrade(api, tmp_path):
    client, app_module, _ = api
    pdf = tmp_path / "old.pdf"
    make_pdf(pdf, "First page about owlbears.", "Second page about griffons.")
    sid = add_source(app_module, pdf, "x", "y", with_pages=False)  # ingested before the reader existed

    page = client.get(f"/api/sources/{sid}/pages/2").json()
    assert "griffons" in page["clean_md"] and page["clean_version"] == FORMATTER_VERSION
    assert [r["page_num"] for r in client.get("/api/search", params={"query": "griffons"}).json()] == [2]


def test_outdated_formatter_pages_are_rebuilt_keeping_ai(api, tmp_path):
    client, app_module, _ = api
    pdf = tmp_path / "b.pdf"
    make_pdf(pdf, "Fresh extraction text.")
    sid = add_source(app_module, pdf, "stale clean", version=FORMATTER_VERSION - 1)
    conn = app_module._conn()
    db.set_page_ai(conn, sid, 1, "# kept ai", "m", ai_format.content_hash("stale clean"))
    conn.close()

    page = client.get(f"/api/sources/{sid}/pages/1").json()
    assert page["clean_md"] == "Fresh extraction text."
    assert page["ai_md"] == "# kept ai" and page["ai_stale"] is True  # kept, flagged for regeneration


def test_missing_pdf_serves_stored_pages_or_explains(api, tmp_path):
    client, app_module, _ = api
    old = add_source(app_module, tmp_path / "gone.pdf", "stored text", version=0)
    assert client.get(f"/api/sources/{old}/pages/1").json()["clean_md"] == "stored text"

    never = add_source(app_module, tmp_path / "gone2.pdf", "x", with_pages=False)
    r = client.get(f"/api/sources/{never}/pages/1")
    assert r.status_code == 404 and "missing" in r.json()["detail"]


def test_page_out_of_range_and_unknown_source(api, tmp_path):
    client, app_module, _ = api
    sid = add_source(app_module, tmp_path / "b.pdf", "only page")
    assert client.get(f"/api/sources/{sid}/pages/2").status_code == 404
    assert client.get("/api/sources/999/pages/1").status_code == 404


def test_ai_format_publishes_progress_events(api, tmp_path, monkeypatch):
    client, app_module, _ = api
    sid = add_source(app_module, tmp_path / "b.pdf", SECTION)
    events = []
    monkeypatch.setattr(app_module.broker, "publish", lambda name, payload: events.append((name, payload)))
    client.post(f"/api/sources/{sid}/pages/1/ai-format", json={}, headers=HEADERS)
    progress = [p for name, p in events if name == "ai_format_progress"]
    assert progress[0] == {
        "source_id": sid, "page_num": 1, "max_attempts": 4, "done": 0, "total": 1, "attempt": 1, "written": 0, "expected": 0,
    }
    assert any(p["written"] > 0 and p["expected"] == len(SECTION) for p in progress)  # reply streaming in
    assert progress[-1]["done"] == progress[-1]["total"] == 1 and progress[-1]["attempt"] == 0
    log = [p["message"] for name, p in events if name == "ai_format_log"]
    assert log[0].startswith("Using qwen2.5:1.5b") and log[-2].startswith("This page: accepted on attempt 1")
    assert log[-1].startswith("Layout changed on 1 line")  # what the accepted version changed



def test_second_run_on_the_same_page_is_refused_while_one_is_running(api, tmp_path):
    client, app_module, fake = api
    sid = add_source(app_module, tmp_path / "b.pdf", SECTION)

    def respond(text):
        second = client.post(f"/api/sources/{sid}/pages/1/ai-format", json={}, headers=HEADERS)
        assert second.status_code == 409 and "already being formatted" in second.json()["detail"]
        return text

    fake.respond = respond
    assert client.post(f"/api/sources/{sid}/pages/1/ai-format", json={}, headers=HEADERS).status_code == 200


def test_cancel_endpoint_stops_formatting_and_changes_nothing(api, tmp_path):
    client, app_module, fake = api
    sid = add_source(app_module, tmp_path / "b.pdf", SECTION)

    def respond(text):
        # The user clicks Cancel while the first attempt is running.
        assert client.post(f"/api/sources/{sid}/pages/1/ai-format/cancel", headers=HEADERS).json() == {"ok": True}
        return "A summary of the weather."

    fake.respond = respond
    r = client.post(f"/api/sources/{sid}/pages/1/ai-format", json={}, headers=HEADERS)
    assert r.status_code == 409 and "Cancelled" in r.json()["detail"]
    assert fake.calls == 1  # no retries after cancelling
    page = client.get(f"/api/sources/{sid}/pages/1").json()
    assert page["ai_md"] is None and page["best"] == "clean"
    assert client.post(f"/api/sources/{sid}/pages/1/ai-format/cancel", headers=HEADERS).json() == {"ok": False}


def test_ai_format_reports_what_changed(api, tmp_path):
    client, app_module, fake = api
    sid = add_source(app_module, tmp_path / "b.pdf", SECTION)
    page = client.post(f"/api/sources/{sid}/pages/1/ai-format", json={}, headers=HEADERS).json()
    assert page["ai_changes"]["meaningful"] is True and page["ai_changes"]["markup"] == 1  # the "# " heading

    fake.respond = lambda t: t  # model returns the page unchanged
    page = client.post(f"/api/sources/{sid}/pages/1/ai-format", json={"force": True}, headers=HEADERS).json()
    assert page["ai_changes"]["meaningful"] is False
    assert page["ai_changes"]["summary"].startswith("This page was already well formatted")


# ---- 0.3.12: fixes from the 0.3.10 sweep -----------------------------------------------------

def test_a_shorter_pdf_never_deletes_edits_on_the_missing_pages(api, tmp_path):
    client, app_module, _ = api
    pdf = tmp_path / "b.pdf"
    make_pdf(pdf, "One.", "Two.", "Three.")
    sid = add_source(app_module, pdf, "x", "y", "z", with_pages=False)
    client.get(f"/api/sources/{sid}/pages/1")
    client.put(f"/api/sources/{sid}/pages/3/edit", json={"markdown": "My page three."}, headers=HEADERS)
    make_pdf(pdf, "One.", "Two.")  # the file at that path was replaced by a shorter one
    conn = app_module._conn()
    conn.execute("UPDATE pages SET clean_version=0 WHERE source_id=?", (sid,))
    conn.commit()
    conn.close()

    assert client.get(f"/api/sources/{sid}/pages/1").status_code == 200  # stored pages still served
    assert client.get(f"/api/sources/{sid}/pages/3").json()["edited_md"] == "My page three."


def test_a_failed_rebuild_is_not_retried_on_every_page_view(api, tmp_path, monkeypatch):
    client, app_module, _ = api
    pdf = tmp_path / "b.pdf"
    make_pdf(pdf, "One.")
    sid = add_source(app_module, pdf, "stale", version=FORMATTER_VERSION - 1)
    calls = []

    def broken(conn, source_id, path):
        calls.append(source_id)
        raise RuntimeError("damaged PDF")

    monkeypatch.setattr(app_module, "rebuild_source", broken)
    for _ in range(3):
        assert client.get(f"/api/sources/{sid}/pages/1").json()["clean_md"] == "stale"
    assert calls == [sid]


def test_search_lists_each_page_once(api, tmp_path):
    client, app_module, _ = api
    body = "filler " * 160 + "owlbear " + "filler " * 160  # "owlbear" lands in two overlapping chunks
    conn = app_module._conn()
    from codex_engine.ingest import chunk_text

    chunks = chunk_text(1, None, body)
    assert sum("owlbear" in c.body for c in chunks) == 2
    db.create_source_with_chunks(conn, "Book", str(tmp_path / "b.pdf"), "h", 1, chunks, page_rows=pages_of(body))
    conn.close()
    assert [r["page_num"] for r in client.get("/api/search", params={"query": "owlbear"}).json()] == [1]


def test_deleting_a_book_while_a_page_is_formatted_is_a_404_not_a_crash(api, tmp_path):
    client, app_module, fake = api
    sid = add_source(app_module, tmp_path / "b.pdf", SECTION)

    def delete_then_answer(text):
        conn = app_module._conn()
        db.delete_source(conn, sid)
        conn.close()
        return "# " + text

    fake.respond = delete_then_answer
    r = client.post(f"/api/sources/{sid}/pages/1/ai-format", json={}, headers=HEADERS)
    assert r.status_code == 404 and "removed" in r.json()["detail"]


def test_source_toggle_and_delete_validate_their_input(api, tmp_path):
    client, app_module, _ = api
    sid = add_source(app_module, tmp_path / "b.pdf", SECTION)
    assert client.patch(f"/api/sources/{sid}/enabled", json={"enabled": "false"}, headers=HEADERS).status_code == 422
    assert client.patch(f"/api/sources/{sid}/enabled", json={"enabled": False}, headers=HEADERS).status_code == 200
    assert client.patch("/api/sources/999/enabled", json={"enabled": True}, headers=HEADERS).status_code == 404
    assert client.delete("/api/sources/999", headers=HEADERS).status_code == 404


def test_upload_rejects_non_pdfs_and_reports_a_full_disk(api, monkeypatch):
    client, app_module, _ = api
    r = client.post("/api/ingest/upload", files={"file": ("notes.PDF", b"hello", "application/pdf")}, headers=HEADERS)
    assert r.status_code == 400 and "isn't a PDF" in r.json()["detail"]

    def full(*args):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(app_module, "store_upload", full)
    r = client.post("/api/ingest/upload", files={"file": ("a.pdf", b"%PDF-1.7 x", "application/pdf")}, headers=HEADERS)
    assert r.status_code == 507 and "No space left" in r.json()["detail"]


def test_error_messages_have_no_stray_quotes(api):
    client, _, _ = api
    r = client.post("/api/ingest/12345/cancel", headers=HEADERS)
    assert r.status_code == 404 and r.json()["detail"] == "No active ingest with that id."


def test_updates_are_only_applied_by_the_windows_app_and_one_at_a_time(api, monkeypatch):
    client, app_module, _ = api
    monkeypatch.setattr(app_module, "can_apply_updates", lambda: False)
    r = client.post("/api/update/apply", json={}, headers=HEADERS)
    assert r.status_code == 400 and "Windows" in r.json()["detail"]
    monkeypatch.setattr(app_module, "can_apply_updates", lambda: True)
    app_module._update_lock.acquire()
    try:
        assert client.post("/api/update/apply", json={}, headers=HEADERS).status_code == 409
    finally:
        app_module._update_lock.release()
