use rusqlite::{params, Connection, OptionalExtension, Result};

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct SourceRow {
  pub id: i64,
  pub title: String,
  pub path: String,
  pub sha256: String,
  pub pages: i64,
  pub enabled: i64, // 0/1
  pub source_key: String,
}

#[derive(Debug, Clone)]
pub struct ChunkRow {
  pub page_num: i64,
  pub heading: Option<String>,
  pub body: String,
  pub loc: String,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct SearchRow {
  pub source_id: i64,
  pub source_title: String,
  pub source_path: String,
  pub page_num: i64,
  pub heading: Option<String>,
  pub snippet: String,
  pub loc: Option<String>,
}

pub fn open_db(path: &str) -> Result<Connection> {
  let conn = Connection::open(path)?;
  conn.pragma_update(None, "journal_mode", "WAL")?;
  conn.pragma_update(None, "synchronous", "NORMAL")?;
  conn.pragma_update(None, "temp_store", "MEMORY")?;
  Ok(conn)
}

fn table_exists(conn: &Connection, name: &str) -> Result<bool> {
  conn
    .query_row(
      "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=? LIMIT 1",
      params![name],
      |_| Ok(()),
    )
    .optional()
    .map(|o| o.is_some())
}

fn columns(conn: &Connection, table: &str) -> Result<Vec<String>> {
  let mut stmt = conn.prepare(&format!("PRAGMA table_info({})", table))?;
  let rows = stmt.query_map([], |row| row.get::<_, String>(1))?;
  let mut out = Vec::new();
  for r in rows {
    out.push(r?);
  }
  Ok(out)
}

fn rebuild_sources(conn: &Connection) -> Result<()> {
  conn.execute_batch(
    r#"
    BEGIN;
    CREATE TABLE IF NOT EXISTS sources_new (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      title TEXT NOT NULL,
      path TEXT NOT NULL,
      sha256 TEXT NOT NULL,
      pages INTEGER NOT NULL DEFAULT 0,
      enabled INTEGER NOT NULL DEFAULT 1,
      source_key TEXT NOT NULL
    );

    INSERT INTO sources_new (id, title, path, sha256, pages, enabled, source_key)
    SELECT
      id,
      COALESCE(title, 'Untitled'),
      COALESCE(path, ''),
      COALESCE(sha256, ''),
      COALESCE(pages, 0),
      COALESCE(enabled, 1),
      COALESCE(source_key, sha256, path, CAST(id AS TEXT))
    FROM sources;

    DROP TABLE sources;
    ALTER TABLE sources_new RENAME TO sources;

    CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_source_key ON sources(source_key);
    CREATE INDEX IF NOT EXISTS idx_sources_sha256 ON sources(sha256);
    CREATE INDEX IF NOT EXISTS idx_sources_enabled ON sources(enabled);
    COMMIT;
    "#,
  )?;
  Ok(())
}

fn rebuild_chunks(conn: &Connection) -> Result<()> {
  conn.execute_batch(
    r#"
    BEGIN;
    CREATE TABLE IF NOT EXISTS chunks_new (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      source_id INTEGER NOT NULL,
      page_num INTEGER NOT NULL DEFAULT 0,
      heading TEXT,
      body TEXT NOT NULL,
      loc TEXT,
      FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
    );

    INSERT INTO chunks_new (id, source_id, page_num, heading, body, loc)
    SELECT
      id,
      source_id,
      COALESCE(page_num, 0),
      heading,
      COALESCE(body, ''),
      loc
    FROM chunks;

    DROP TABLE chunks;
    ALTER TABLE chunks_new RENAME TO chunks;

    CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id);
    CREATE INDEX IF NOT EXISTS idx_chunks_page ON chunks(page_num);
    COMMIT;
    "#,
  )?;
  Ok(())
}

fn ensure_sources(conn: &Connection) -> Result<()> {
  if !table_exists(conn, "sources")? {
    conn.execute_batch(
      r#"
      CREATE TABLE sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        pages INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        source_key TEXT NOT NULL
      );
      CREATE UNIQUE INDEX idx_sources_source_key ON sources(source_key);
      CREATE INDEX idx_sources_sha256 ON sources(sha256);
      CREATE INDEX idx_sources_enabled ON sources(enabled);
      "#,
    )?;
    return Ok(());
  }

  let cols = columns(conn, "sources")?;
  let needs = ["title", "path", "sha256", "pages", "enabled", "source_key"];
  let missing_any = needs.iter().any(|c| !cols.iter().any(|x| x == c));
  if missing_any {
    return rebuild_sources(conn);
  }

  conn.execute_batch(
    r#"
    CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_source_key ON sources(source_key);
    CREATE INDEX IF NOT EXISTS idx_sources_sha256 ON sources(sha256);
    CREATE INDEX IF NOT EXISTS idx_sources_enabled ON sources(enabled);
    "#,
  )?;
  Ok(())
}

fn ensure_chunks(conn: &Connection) -> Result<()> {
  if !table_exists(conn, "chunks")? {
    conn.execute_batch(
      r#"
      CREATE TABLE chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER NOT NULL,
        page_num INTEGER NOT NULL DEFAULT 0,
        heading TEXT,
        body TEXT NOT NULL,
        loc TEXT,
        FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
      );
      CREATE INDEX idx_chunks_source ON chunks(source_id);
      CREATE INDEX idx_chunks_page ON chunks(page_num);
      "#,
    )?;
    return Ok(());
  }

  let cols = columns(conn, "chunks")?;
  let needs = ["source_id", "page_num", "body"];
  let missing_any = needs.iter().any(|c| !cols.iter().any(|x| x == c));
  if missing_any {
    return rebuild_chunks(conn);
  }

  conn.execute_batch(
    r#"
    CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id);
    CREATE INDEX IF NOT EXISTS idx_chunks_page ON chunks(page_num);
    "#,
  )?;
  Ok(())
}

fn ensure_fts(conn: &Connection) -> Result<()> {
  if !table_exists(conn, "chunks_fts")? {
    conn.execute_batch(
      r#"
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
      "#,
    )?;
  }
  Ok(())
}

pub fn init_schema(conn: &Connection) -> Result<()> {
  conn.execute_batch("PRAGMA foreign_keys=ON;")?;
  ensure_sources(conn)?;
  ensure_chunks(conn)?;
  ensure_fts(conn)?;
  Ok(())
}

pub fn list_sources(conn: &Connection) -> Result<Vec<SourceRow>> {
  let mut stmt = conn.prepare(
    "SELECT id, title, path, sha256, pages, enabled, source_key FROM sources ORDER BY id DESC",
  )?;
  let rows = stmt.query_map([], |r| {
    Ok(SourceRow {
      id: r.get(0)?,
      title: r.get(1)?,
      path: r.get(2)?,
      sha256: r.get(3)?,
      pages: r.get(4)?,
      enabled: r.get(5)?,
      source_key: r.get(6)?,
    })
  })?;

  let mut out = Vec::new();
  for r in rows {
    out.push(r?);
  }
  Ok(out)
}

pub fn set_source_enabled(conn: &mut Connection, source_id: i64, enabled: bool) -> Result<()> {
  conn.execute("UPDATE sources SET enabled=? WHERE id=?", params![if enabled { 1 } else { 0 }, source_id])?;
  Ok(())
}

pub fn delete_source(conn: &mut Connection, source_id: i64) -> Result<()> {
  conn.execute("DELETE FROM sources WHERE id=?", params![source_id])?;
  Ok(())
}

pub fn get_source_by_hash(conn: &Connection, sha256: &str) -> Result<Option<SourceRow>> {
  conn
    .query_row(
      "SELECT id, title, path, sha256, pages, enabled, source_key FROM sources WHERE sha256=? LIMIT 1",
      params![sha256],
      |r| {
        Ok(SourceRow {
          id: r.get(0)?,
          title: r.get(1)?,
          path: r.get(2)?,
          sha256: r.get(3)?,
          pages: r.get(4)?,
          enabled: r.get(5)?,
          source_key: r.get(6)?,
        })
      },
    )
    .optional()
}

fn unique_source_key(conn: &Connection, base: &str) -> Result<String> {
  let mut key = base.to_string();
  let mut i = 0u32;
  loop {
    let exists: Option<i64> = conn
      .query_row("SELECT id FROM sources WHERE source_key=? LIMIT 1", params![&key], |r| r.get(0))
      .optional()?;
    if exists.is_none() {
      return Ok(key);
    }
    i += 1;
    key = format!("{}::copy{}", base, i);
  }
}

pub fn create_source(conn: &mut Connection, title: &str, path: &str, sha: &str, pages: i64, enabled: bool) -> Result<i64> {
  let source_key = unique_source_key(conn, sha)?;
  conn.execute(
    "INSERT INTO sources (title, path, sha256, pages, enabled, source_key) VALUES (?,?,?,?,?,?)",
    params![title, path, sha, pages, if enabled { 1 } else { 0 }, source_key],
  )?;
  Ok(conn.last_insert_rowid())
}

pub fn update_source(conn: &Connection, source_id: i64, title: &str, path: &str, sha: &str, pages: i64, enabled: bool) -> Result<()> {
  conn.execute(
    "UPDATE sources SET title=?, path=?, sha256=?, pages=?, enabled=? WHERE id=?",
    params![title, path, sha, pages, if enabled { 1 } else { 0 }, source_id],
  )?;
  Ok(())
}

pub fn insert_chunks(conn: &mut Connection, source_id: i64, chunks: &[ChunkRow]) -> Result<i64> {
  let tx = conn.transaction()?;
  {
    let mut stmt = tx.prepare("INSERT INTO chunks (source_id, page_num, heading, body, loc) VALUES (?,?,?,?,?)")?;
    for c in chunks {
      stmt.execute(params![source_id, c.page_num, c.heading.as_deref(), c.body, c.loc])?;
    }
  }
  tx.commit()?;
  Ok(chunks.len() as i64)
}

pub fn search(conn: &Connection, query: &str, limit: i64) -> Result<Vec<SearchRow>> {
  let mut stmt = conn.prepare(
    r#"
    SELECT s.id, s.title, s.path, c.page_num, c.heading,
           substr(c.body, 1, 280) AS snippet,
           c.loc
    FROM chunks_fts
    JOIN chunks c ON c.id = chunks_fts.rowid
    JOIN sources s ON s.id = c.source_id
    WHERE s.enabled = 1 AND chunks_fts MATCH ?
    ORDER BY bm25(chunks_fts) ASC
    LIMIT ?
    "#,
  )?;

  let rows = stmt.query_map(params![query, limit], |r| {
    Ok(SearchRow {
      source_id: r.get(0)?,
      source_title: r.get(1)?,
      source_path: r.get(2)?,
      page_num: r.get(3)?,
      heading: r.get(4)?,
      snippet: r.get(5)?,
      loc: r.get(6)?,
    })
  })?;

  let mut out = Vec::new();
  for r in rows {
    out.push(r?);
  }
  Ok(out)
}

