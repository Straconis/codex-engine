from __future__ import annotations

import sqlite3
from pathlib import Path

from .models import ChunkRow, SearchRow, SourceRow


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
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

        CREATE TABLE IF NOT EXISTS chunks (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source_id INTEGER NOT NULL,
          page_num INTEGER NOT NULL DEFAULT 0,
          heading TEXT,
          body TEXT NOT NULL,
          loc TEXT,
          FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id);
        CREATE INDEX IF NOT EXISTS idx_chunks_page ON chunks(page_num);
        """
    )
    ensure_fts(conn)


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


def set_source_enabled(conn: sqlite3.Connection, source_id: int, enabled: bool) -> None:
    conn.execute("UPDATE sources SET enabled=? WHERE id=?", (1 if enabled else 0, source_id))
    conn.commit()


def delete_source(conn: sqlite3.Connection, source_id: int) -> None:
    conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
    conn.commit()


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


def create_source(conn: sqlite3.Connection, title: str, path: str, sha: str, pages: int, enabled: bool) -> int:
    source_key = unique_source_key(conn, sha)
    cur = conn.execute(
        "INSERT INTO sources (title, path, sha256, pages, enabled, source_key) VALUES (?,?,?,?,?,?)",
        (title, path, sha, pages, 1 if enabled else 0, source_key),
    )
    conn.commit()
    return int(cur.lastrowid)


def insert_chunks(conn: sqlite3.Connection, source_id: int, chunks: list[ChunkRow]) -> int:
    conn.executemany(
        "INSERT INTO chunks (source_id, page_num, heading, body, loc) VALUES (?,?,?,?,?)",
        [(source_id, c.page_num, c.heading, c.body, c.loc) for c in chunks],
    )
    conn.commit()
    return len(chunks)


def search(conn: sqlite3.Connection, query: str, limit: int = 50) -> list[SearchRow]:
    rows = conn.execute(
        """
        SELECT s.id AS source_id, s.title AS source_title, s.path AS source_path,
               c.page_num, c.heading, substr(c.body, 1, 280) AS snippet, c.loc
        FROM chunks_fts
        JOIN chunks c ON c.id = chunks_fts.rowid
        JOIN sources s ON s.id = c.source_id
        WHERE s.enabled = 1 AND chunks_fts MATCH ?
        ORDER BY bm25(chunks_fts) ASC
        LIMIT ?
        """,
        (query, limit),
    ).fetchall()
    return [SearchRow(**dict(row)) for row in rows]
