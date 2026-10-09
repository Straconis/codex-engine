from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from .models import ChunkRow, PageContent, PageRow, SearchRow, SourceRow


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sources (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          title TEXT NOT NULL,
          path TEXT NOT NULL,
          sha256 TEXT NOT NULL,
          pages INTEGER NOT NULL DEFAULT 0,
          enabled INTEGER NOT NULL DEFAULT 1,
          source_key TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_source_key ON sources(source_key);
        CREATE INDEX IF NOT EXISTS idx_sources_sha256 ON sources(sha256);
        CREATE INDEX IF NOT EXISTS idx_sources_enabled ON sources(enabled);
        CREATE INDEX IF NOT EXISTS idx_sources_path ON sources(path);

        CREATE TABLE IF NOT EXISTS chunks (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source_id INTEGER NOT NULL,
          page_num INTEGER NOT NULL DEFAULT 0,
          heading TEXT,
          body TEXT NOT NULL,
          loc TEXT,
          FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
        );
        DROP INDEX IF EXISTS idx_chunks_source;  -- covered by idx_chunks_source_page
        CREATE INDEX IF NOT EXISTS idx_chunks_source_page ON chunks(source_id, page_num);
        CREATE INDEX IF NOT EXISTS idx_chunks_page ON chunks(page_num);

        -- Reader text per page. raw_text is the untouched extraction, clean_md the
        -- deterministic cleanup (clean_version = formatting.FORMATTER_VERSION that made it),
        -- ai_md the optional AI-formatted version and ai_source_hash the clean_md it came from.
        CREATE TABLE IF NOT EXISTS pages (
          source_id INTEGER NOT NULL,
          page_num INTEGER NOT NULL,
          clean_md TEXT NOT NULL,
          ai_md TEXT,
          ai_model TEXT,
          PRIMARY KEY (source_id, page_num),
          FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
        );

        -- AI output per formatted section, keyed by hash(prompt version, model, input text).
        -- Survives re-ingest/replace, so identical text is never sent to the model twice.
        CREATE TABLE IF NOT EXISTS ai_cache (
          key TEXT PRIMARY KEY,
          model TEXT NOT NULL,
          output TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    _migrate_pages(conn)
    ensure_fts(conn)


# Columns added to `pages` after it first shipped; added in place on older databases.
_PAGE_COLUMNS = {
    "raw_text": "TEXT",
    "clean_version": "INTEGER NOT NULL DEFAULT 0",
    "ai_source_hash": "TEXT",
    "ai_error": "TEXT",
    "ai_updated_at": "TEXT",
    "edited_md": "TEXT",  # the user's own correction of the page; shown before anything else
    "edited_at": "TEXT",
}


def _migrate_pages(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(pages)")}
    for name, decl in _PAGE_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE pages ADD COLUMN {name} {decl}")
    conn.commit()


def ensure_fts(conn: sqlite3.Connection) -> None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name='chunks_fts' LIMIT 1"
    ).fetchone()
    if exists:
        return
    conn.executescript(
        """
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
          body,
          heading,
          content='chunks',
          content_rowid='id'
        );

        INSERT INTO chunks_fts(rowid, body, heading)
        SELECT id, body, heading FROM chunks;

        CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
          INSERT INTO chunks_fts(rowid, body, heading) VALUES (new.id, new.body, new.heading);
        END;
        CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
          INSERT INTO chunks_fts(chunks_fts, rowid, body, heading) VALUES('delete', old.id, old.body, old.heading);
        END;
        CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
          INSERT INTO chunks_fts(chunks_fts, rowid, body, heading) VALUES('delete', old.id, old.body, old.heading);
          INSERT INTO chunks_fts(rowid, body, heading) VALUES (new.id, new.body, new.heading);
        END;
        """
    )


def list_sources(conn: sqlite3.Connection) -> list[SourceRow]:
    rows = conn.execute(
        "SELECT id, title, path, sha256, pages, enabled, source_key FROM sources ORDER BY id DESC"
    ).fetchall()
    return [SourceRow(**dict(row)) for row in rows]


def set_source_enabled(conn: sqlite3.Connection, source_id: int, enabled: bool) -> bool:
    """False if there is no such source."""
    cur = conn.execute("UPDATE sources SET enabled=? WHERE id=?", (1 if enabled else 0, source_id))
    conn.commit()
    return cur.rowcount > 0


def delete_source(conn: sqlite3.Connection, source_id: int) -> bool:
    """False if there is no such source."""
    cur = conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
    conn.commit()
    return cur.rowcount > 0


def get_source_by_hash(conn: sqlite3.Connection, sha256: str) -> SourceRow | None:
    row = conn.execute(
        "SELECT id, title, path, sha256, pages, enabled, source_key FROM sources WHERE sha256=? LIMIT 1",
        (sha256,),
    ).fetchone()
    return SourceRow(**dict(row)) if row else None


def unique_source_key(conn: sqlite3.Connection, base: str) -> str:
    key = base
    index = 0
    while True:
        exists = conn.execute("SELECT id FROM sources WHERE source_key=? LIMIT 1", (key,)).fetchone()
        if not exists:
            return key
        index += 1
        key = f"{base}::copy{index}"


def create_source_with_chunks(
    conn: sqlite3.Connection,
    title: str,
    path: str,
    sha: str,
    pages: int,
    chunks: list[ChunkRow],
    replace_source_id: int | None = None,
    page_rows: list[PageContent] | None = None,
) -> int:
    """Insert a source and all of its chunks atomically.

    If replace_source_id is given, that source is deleted in the same transaction,
    so a failed or interrupted ingest never loses the original or leaves a
    half-written source (which would otherwise poison duplicate detection).
    """
    with conn:  # BEGIN ... COMMIT, or ROLLBACK on any exception
        if replace_source_id is not None:
            conn.execute("DELETE FROM sources WHERE id=?", (replace_source_id,))
        source_key = unique_source_key(conn, sha)
        cur = conn.execute(
            "INSERT INTO sources (title, path, sha256, pages, enabled, source_key) VALUES (?,?,?,?,?,?)",
            (title, path, sha, pages, 1, source_key),
        )
        source_id = int(cur.lastrowid)
        conn.executemany(
            "INSERT INTO chunks (source_id, page_num, heading, body, loc) VALUES (?,?,?,?,?)",
            [(source_id, c.page_num, c.heading, c.body, c.loc) for c in chunks],
        )
        if page_rows is not None:
            _insert_pages(conn, source_id, page_rows)
    return source_id


def get_source(conn: sqlite3.Connection, source_id: int) -> SourceRow | None:
    row = conn.execute(
        "SELECT id, title, path, sha256, pages, enabled, source_key FROM sources WHERE id=?",
        (source_id,),
    ).fetchone()
    return SourceRow(**dict(row)) if row else None


_PAGE_SELECT = (
    "SELECT source_id, page_num, raw_text, clean_md, clean_version, ai_md, ai_model, "
    "ai_source_hash, ai_error, ai_updated_at, edited_md, edited_at FROM pages"
)


def _insert_pages(conn: sqlite3.Connection, source_id: int, page_rows: list[PageContent]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO pages (source_id, page_num, raw_text, clean_md, clean_version) VALUES (?,?,?,?,?)",
        [(source_id, i, p.raw_text, p.clean_md, p.clean_version) for i, p in enumerate(page_rows, start=1)],
    )


def rebuild_source_content(
    conn: sqlite3.Connection, source_id: int, page_rows: list[PageContent], chunks: list[ChunkRow]
) -> None:
    """Replace a source's pages and search chunks in one transaction.

    Used to upgrade books ingested by an older formatter. AI output already generated
    is kept: ai_md is carried over and flagged stale by hash if the cleaned text changed,
    and identical text is re-served from ai_cache.
    """
    with conn:
        previous = {
            row["page_num"]: row
            for row in conn.execute(
                "SELECT page_num, ai_md, ai_model, ai_source_hash, ai_error, ai_updated_at, edited_md, edited_at "
                "FROM pages WHERE source_id=?",
                (source_id,),
            )
        }
        conn.execute("DELETE FROM pages WHERE source_id=?", (source_id,))
        conn.execute("DELETE FROM chunks WHERE source_id=?", (source_id,))
        _insert_pages(conn, source_id, page_rows)
        conn.executemany(
            "UPDATE pages SET ai_md=?, ai_model=?, ai_source_hash=?, ai_error=?, ai_updated_at=?, edited_md=?, edited_at=? "
            "WHERE source_id=? AND page_num=?",
            [
                (
                    r["ai_md"], r["ai_model"], r["ai_source_hash"], r["ai_error"], r["ai_updated_at"],
                    r["edited_md"], r["edited_at"], source_id, n,
                )
                for n, r in previous.items()
            ],
        )
        conn.executemany(
            "INSERT INTO chunks (source_id, page_num, heading, body, loc) VALUES (?,?,?,?,?)",
            [(source_id, c.page_num, c.heading, c.body, c.loc) for c in chunks],
        )
        conn.execute("UPDATE sources SET pages=? WHERE id=?", (len(page_rows), source_id))


def oldest_clean_version(conn: sqlite3.Connection, source_id: int) -> int | None:
    """Lowest formatter version among a source's pages; None if it has no pages yet."""
    row = conn.execute("SELECT MIN(clean_version) AS v, COUNT(*) AS n FROM pages WHERE source_id=?", (source_id,)).fetchone()
    return None if not row or not row["n"] else int(row["v"])


def get_page(conn: sqlite3.Connection, source_id: int, page_num: int) -> PageRow | None:
    row = conn.execute(f"{_PAGE_SELECT} WHERE source_id=? AND page_num=?", (source_id, page_num)).fetchone()
    return PageRow(**dict(row)) if row else None


def pages_with_user_work_after(conn: sqlite3.Connection, source_id: int, last_page: int) -> list[int]:
    """Pages past `last_page` that hold an edit or an AI version."""
    rows = conn.execute(
        "SELECT page_num FROM pages WHERE source_id=? AND page_num>? AND (edited_md IS NOT NULL OR ai_md IS NOT NULL) "
        "ORDER BY page_num",
        (source_id, last_page),
    ).fetchall()
    return [r["page_num"] for r in rows]


def pages_with_ai(conn: sqlite3.Connection, source_id: int) -> list[PageRow]:
    rows = conn.execute(f"{_PAGE_SELECT} WHERE source_id=? AND ai_md IS NOT NULL ORDER BY page_num", (source_id,)).fetchall()
    return [PageRow(**dict(r)) for r in rows]


def set_page_ai(conn: sqlite3.Connection, source_id: int, page_num: int, ai_md: str, ai_model: str, source_hash: str, note: str | None = None) -> None:
    with conn:
        conn.execute(
            "UPDATE pages SET ai_md=?, ai_model=?, ai_source_hash=?, ai_error=?, ai_updated_at=datetime('now') "
            "WHERE source_id=? AND page_num=?",
            (ai_md, ai_model, source_hash, note, source_id, page_num),
        )


def set_page_ai_error(conn: sqlite3.Connection, source_id: int, page_num: int, error: str) -> None:
    """Record a failed AI attempt. Any earlier valid ai_md is kept."""
    with conn:
        conn.execute(
            "UPDATE pages SET ai_error=?, ai_updated_at=datetime('now') WHERE source_id=? AND page_num=?",
            (error, source_id, page_num),
        )


def set_page_edit(
    conn: sqlite3.Connection, source_id: int, page_num: int, markdown: str | None, chunks: list[ChunkRow]
) -> None:
    """Save (or with None, remove) the user's own version of a page. Other versions are untouched.

    `chunks` replace the page's search chunks in the same transaction, so search finds
    what the page now says (the edit, or the cleaned text again after a revert).
    """
    with conn:
        conn.execute(
            "UPDATE pages SET edited_md=?, edited_at=CASE WHEN ? IS NULL THEN NULL ELSE datetime('now') END "
            "WHERE source_id=? AND page_num=?",
            (markdown, markdown, source_id, page_num),
        )
        _replace_page_chunks(conn, source_id, page_num, chunks)


def _replace_page_chunks(conn: sqlite3.Connection, source_id: int, page_num: int, chunks: list[ChunkRow]) -> None:
    conn.execute("DELETE FROM chunks WHERE source_id=? AND page_num=?", (source_id, page_num))
    conn.executemany(
        "INSERT INTO chunks (source_id, page_num, heading, body, loc) VALUES (?,?,?,?,?)",
        [(source_id, c.page_num, c.heading, c.body, c.loc) for c in chunks],
    )


def edited_pages(conn: sqlite3.Connection, source_id: int | None = None) -> list[PageRow]:
    """Pages the user has corrected, for one source or all of them."""
    where, params = ("AND source_id=?", (source_id,)) if source_id is not None else ("", ())
    rows = conn.execute(f"{_PAGE_SELECT} WHERE edited_md IS NOT NULL {where} ORDER BY source_id, page_num", params).fetchall()
    return [PageRow(**dict(r)) for r in rows]


# Schema versions (PRAGMA user_version) whose upgrade needs code outside this module
# (see app._conn). 1: edits saved before 0.3.10 are in the search index.
SCHEMA_EDITS_INDEXED = 1


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def reindex_edits(conn: sqlite3.Connection, chunks_by_page: dict[tuple[int, int], list[ChunkRow]]) -> None:
    """One-time upgrade: index edits saved before 0.3.10, then record that it's done."""
    with conn:
        for (source_id, page_num), chunks in chunks_by_page.items():
            _replace_page_chunks(conn, source_id, page_num, chunks)
        conn.execute(f"PRAGMA user_version = {SCHEMA_EDITS_INDEXED}")


def ai_cache_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT output FROM ai_cache WHERE key=?", (key,)).fetchone()
    return row["output"] if row else None


def ai_cache_put(conn: sqlite3.Connection, key: str, model: str, output: str) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO ai_cache (key, model, output) VALUES (?,?,?)", (key, model, output))


def source_path_in_use(conn: sqlite3.Connection, path: str) -> bool:
    return conn.execute("SELECT 1 FROM sources WHERE path=? LIMIT 1", (path,)).fetchone() is not None


# Invisible markers around matched terms in search snippets; the UI turns them into highlights.
MATCH_START, MATCH_END = "\x02", "\x03"

_QUERY_TOKEN_RE = re.compile(r'"[^"]*"\*?|\S+')


def build_match_query(raw: str) -> str:
    """Turn free-form user input into an FTS5 MATCH expression that can't be a syntax error.

    Raw FTS5 syntax blows up on ordinary TTRPG searches like `half-orc`,
    `kenku's`, `d&d`, `+1 sword` or `AC:`. Every term becomes a quoted phrase
    (implicitly AND-ed). "quoted phrases" are kept together, and a trailing *
    keeps prefix search working (e.g. `necro*`).
    """
    terms: list[str] = []
    raw = "".join(ch if ch.isprintable() else " " for ch in raw or "")  # NUL etc. break FTS5 strings
    for token in _QUERY_TOKEN_RE.findall(raw):
        prefix = False
        if len(token) >= 3 and token.startswith('"') and token.endswith('"*'):
            phrase, prefix = token[1:-2], True  # "fire bol"* : a phrase whose last word is a prefix
        elif len(token) >= 2 and token.startswith('"') and token.endswith('"'):
            phrase = token[1:-1]
        else:
            phrase = token.replace('"', "")
            if phrase.endswith("*"):
                phrase = phrase.rstrip("*")
                prefix = True
        if not any(ch.isalnum() for ch in phrase):
            continue
        term = '"' + phrase.replace('"', '""') + '"'
        if prefix:
            term += "*"
        terms.append(term)
    return " ".join(terms)


def search(conn: sqlite3.Connection, query: str, limit: int = 50) -> list[SearchRow]:
    match = build_match_query(query)
    if not match:
        return []
    # Chunks overlap, so one page can match twice; keep each page's best chunk.
    rows = conn.execute(
        """
        WITH hits AS (
          SELECT s.id AS source_id, s.title AS source_title, s.path AS source_path,
                 c.page_num, c.heading, snippet(chunks_fts, 0, ?, ?, '…', 48) AS snippet, c.loc,
                 bm25(chunks_fts) AS rank
          FROM chunks_fts
          JOIN chunks c ON c.id = chunks_fts.rowid
          JOIN sources s ON s.id = c.source_id
          WHERE s.enabled = 1 AND chunks_fts MATCH ?
        ), ranked AS (
          SELECT *, ROW_NUMBER() OVER (PARTITION BY source_id, page_num ORDER BY rank) AS nth FROM hits
        )
        SELECT source_id, source_title, source_path, page_num, heading, snippet, loc
        FROM ranked WHERE nth = 1
        ORDER BY rank ASC
        LIMIT ?
        """,
        (MATCH_START, MATCH_END, match, limit),
    ).fetchall()
    return [SearchRow(**dict(row)) for row in rows]
