// Prevents additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod db;

use db::{ChunkRow, SearchRow, SourceRow};

use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::Path;
use std::process::Command;
use std::sync::{
  atomic::{AtomicBool, AtomicU8, Ordering},
  mpsc,
  Arc, Mutex, OnceLock,
};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use tauri::{AppHandle, Emitter, Manager};

//
// =======================
// Logging (toggleable)
// =======================
// Modes: off | console | file | both
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum LogMode {
  Off = 0,
  Console = 1,
  File = 2,
  Both = 3,
}

impl LogMode {
  fn from_str(s: &str) -> Option<Self> {
    match s {
      "off" => Some(LogMode::Off),
      "console" => Some(LogMode::Console),
      "file" => Some(LogMode::File),
      "both" => Some(LogMode::Both),
      _ => None,
    }
  }
}

struct ToggleLogger {
  mode: AtomicU8,
  file: Mutex<Option<File>>,
}

static LOGGER: OnceLock<ToggleLogger> = OnceLock::new();

fn now_ts() -> String {
  // simple ms-since-epoch timestamp (no extra deps)
  let ms = SystemTime::now()
    .duration_since(UNIX_EPOCH)
    .unwrap_or_else(|_| Duration::from_millis(0))
    .as_millis();
  format!("{}", ms)
}

impl log::Log for ToggleLogger {
  fn enabled(&self, metadata: &log::Metadata) -> bool {
    // keep everything; mode controls output targets
    metadata.level() <= log::Level::Trace
  }

  fn log(&self, record: &log::Record) {
    if !self.enabled(record.metadata()) {
      return;
    }
    let mode = self.mode.load(Ordering::Relaxed);
    if mode == LogMode::Off as u8 {
      return;
    }

    let line = format!(
      "[{}] {:<5} {}",
      now_ts(),
      record.level(),
      record.args()
    );

    // Console
    if mode == LogMode::Console as u8 || mode == LogMode::Both as u8 {
      eprintln!("{}", line);
    }

    // File
    if mode == LogMode::File as u8 || mode == LogMode::Both as u8 {
      if let Ok(mut guard) = self.file.lock() {
        if let Some(f) = guard.as_mut() {
          let _ = writeln!(f, "{}", line);
          let _ = f.flush();
        }
      }
    }
  }

  fn flush(&self) {
    if let Ok(mut guard) = self.file.lock() {
      if let Some(f) = guard.as_mut() {
        let _ = f.flush();
      }
    }
  }
}

fn init_logger_once(app: &AppHandle) {
  let logger = LOGGER.get_or_init(|| ToggleLogger {
    mode: AtomicU8::new(LogMode::Off as u8),
    file: Mutex::new(None),
  });

  // Register global logger once
  let _ = log::set_logger(logger);
  log::set_max_level(log::LevelFilter::Trace);

  // Default mode: BOTH (you can change it via set_log_mode)
  let _ = set_log_mode_internal(app, LogMode::Both);
}

fn open_log_file(app: &AppHandle) -> Option<File> {
  let dir = app.path().app_data_dir().ok()?;
  let path = dir.join("codex-engine.log");
  // Ensure dir exists
  let _ = std::fs::create_dir_all(&dir);
  OpenOptions::new().create(true).append(true).open(path).ok()
}

fn set_log_mode_internal(app: &AppHandle, mode: LogMode) -> Result<(), String> {
  let logger = LOGGER.get().ok_or_else(|| "Logger not initialized".to_string())?;
  logger.mode.store(mode as u8, Ordering::Relaxed);

  // Adjust file handle
  {
    let mut guard = logger.file.lock().map_err(|_| "Logger lock poisoned".to_string())?;
    if mode == LogMode::File || mode == LogMode::Both {
      if guard.is_none() {
        *guard = open_log_file(app);
      }
    } else {
      *guard = None;
    }
  }

  log::info!("Logging mode set to {:?}", mode);
  Ok(())
}

#[tauri::command]
fn set_log_mode(app: AppHandle, mode: String) -> Result<(), String> {
  let mode = LogMode::from_str(mode.trim()).ok_or_else(|| "Invalid log mode. Use: off|console|file|both".to_string())?;
  set_log_mode_internal(&app, mode)
}

//
// =======================
// Ingest state
// =======================
static CANCEL_MAP: OnceLock<Mutex<HashMap<i64, Arc<AtomicBool>>>> = OnceLock::new();
static NEXT_INGEST_ID: OnceLock<Mutex<i64>> = OnceLock::new();

// When we hit a duplicate, we park the ingest worker and wait for resolve_duplicate().
static DUP_WAITERS: OnceLock<Mutex<HashMap<i64, mpsc::Sender<DupAction>>>> = OnceLock::new();

fn cancel_map() -> &'static Mutex<HashMap<i64, Arc<AtomicBool>>> {
  CANCEL_MAP.get_or_init(|| Mutex::new(HashMap::new()))
}

fn dup_waiters() -> &'static Mutex<HashMap<i64, mpsc::Sender<DupAction>>> {
  DUP_WAITERS.get_or_init(|| Mutex::new(HashMap::new()))
}

fn next_ingest_id() -> i64 {
  let m = NEXT_INGEST_ID.get_or_init(|| Mutex::new(1));
  let mut g = m.lock().unwrap();
  let id = *g;
  *g += 1;
  id
}

fn app_db_path(app: &AppHandle) -> String {
  let dir = app.path().app_data_dir().expect("app_data_dir");
  let p = dir.join("codex-engine.sqlite3");
  p.to_string_lossy().to_string()
}

fn file_title_from_path(path: &str) -> String {
  Path::new(path)
    .file_stem()
    .map(|s| s.to_string_lossy().to_string())
    .unwrap_or_else(|| "Untitled".to_string())
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct IngestProgress {
  id: i64,
  stage: String, // "hash"|"validate"|"extract"|"chunk"|"db"|"done"|"error"|"duplicate"
  message: String,
  current: i64,
  total: i64,
  done: bool,
  error: Option<String>,
}

fn emit_progress(app: &AppHandle, p: IngestProgress) {
  // Log first while we still own `p`
  log::info!(
    "ingest_progress id={} stage={} {}/{} done={} msg={}",
    p.id, p.stage, p.current, p.total, p.done, p.message
  );
  if let Some(e) = &p.error {
    log::error!("ingest_error id={} err={}", p.id, e);
  }

  // Then emit WITHOUT moving `p`
  let _ = app.emit("ingest_progress", &p);
}

fn sha256_hex(path: &str, cancel_flag: &AtomicBool, app: &AppHandle, ingest_id: i64) -> Result<String, String> {
  emit_progress(
    app,
    IngestProgress {
      id: ingest_id,
      stage: "hash".into(),
      message: "Hashing file (sha256)…".into(),
      current: 0,
      total: 1,
      done: false,
      error: None,
    },
  );

  if cancel_flag.load(Ordering::Relaxed) {
    return Err("Cancelled".into());
  }

  let out = Command::new("sha256sum")
    .arg(path)
    .output()
    .map_err(|_| "sha256sum not found. Please install coreutils / ensure sha256sum is available.".to_string())?;

  if !out.status.success() {
    return Err(format!("sha256sum failed: {}", String::from_utf8_lossy(&out.stderr)));
  }

  let s = String::from_utf8_lossy(&out.stdout).to_string();
  let hex = s.split_whitespace().next().unwrap_or("").to_string();
  if hex.len() < 32 {
    return Err("Failed to compute sha256.".into());
  }
  Ok(hex)
}

fn pdfinfo(path: &str) -> Result<(i64, bool), String> {
  // returns (pages, encrypted)
  let out = Command::new("pdfinfo")
    .arg(path)
    .output()
    .map_err(|_| "pdfinfo not found. Install: sudo apt install poppler-utils".to_string())?;

  if !out.status.success() {
    return Err(format!("pdfinfo failed: {}", String::from_utf8_lossy(&out.stderr)));
  }

  let txt = String::from_utf8_lossy(&out.stdout);
  let mut pages: i64 = 0;
  let mut encrypted = false;

  for line in txt.lines() {
    if let Some(rest) = line.strip_prefix("Pages:") {
      pages = rest.trim().parse::<i64>().unwrap_or(0);
    }
    if let Some(rest) = line.strip_prefix("Encrypted:") {
      let v = rest.trim().to_lowercase();
      encrypted = v.starts_with("yes") || v.starts_with("true");
    }
  }

  Ok((pages.max(0), encrypted))
}

fn pdftotext_all_pages(path: &str) -> Result<Vec<String>, String> {
  // Uses poppler's pdftotext; output includes form-feed between pages by default.
  let out = Command::new("pdftotext")
    .arg("-layout")
    .arg(path)
    .arg("-") // stdout
    .output()
    .map_err(|_| "pdftotext not found. Install: sudo apt install poppler-utils".to_string())?;

  if !out.status.success() {
    return Err(format!("pdftotext failed: {}", String::from_utf8_lossy(&out.stderr)));
  }

  let txt = String::from_utf8_lossy(&out.stdout).to_string();
  let pages: Vec<String> = txt.split('\u{000C}').map(|s| s.to_string()).collect();
  Ok(pages)
}

fn pick_heading_from_text(page_text: &str) -> Option<String> {
  // Heuristic: first meaningful line <= 90 chars, avoid very long paragraphs.
  for line in page_text.lines() {
    let l = line.trim();
    if l.is_empty() {
      continue;
    }
    if l.len() > 90 {
      continue;
    }
    let low = l.to_lowercase();
    if low.starts_with("page ") {
      continue;
    }
    if l.chars().any(|c| c.is_alphabetic()) {
      return Some(l.to_string());
    }
  }
  None
}

/// Safely slice a String by char indices (not bytes).
fn slice_by_char(s: &str, start_char: usize, end_char: usize) -> String {
  if start_char >= end_char {
    return String::new();
  }
  let mut start_byte = s.len();
  let mut end_byte = s.len();

  let mut i = 0usize;
  for (b, _) in s.char_indices() {
    if i == start_char {
      start_byte = b;
    }
    if i == end_char {
      end_byte = b;
      break;
    }
    i += 1;
  }

  if start_char == 0 {
    start_byte = 0;
  }
  if end_char >= s.chars().count() {
    end_byte = s.len();
  }

  s[start_byte..end_byte].to_string()
}

fn chunk_text(page_num: i64, heading: Option<String>, text: &str) -> Vec<ChunkRow> {
  let clean = text.replace("\r\n", "\n");
  let body = clean.split_whitespace().collect::<Vec<_>>().join(" ");
  let body = body.trim().to_string();
  if body.is_empty() {
    return vec![];
  }

  // chunk by ~1200 chars with overlap (CHAR COUNT, safe for unicode)
  let max_chars: usize = 1200;
  let overlap_chars: usize = 200;

  let total_chars = body.chars().count();
  let mut out = Vec::new();

  let mut start = 0usize;
  while start < total_chars {
    let end = (start + max_chars).min(total_chars);
    let slice = slice_by_char(&body, start, end);

    out.push(ChunkRow {
      page_num,
      heading: heading.clone(),
      body: slice,
      loc: format!("p. {}", page_num),
    });

    if end == total_chars {
      break;
    }
    start = end.saturating_sub(overlap_chars);
  }

  out
}

//
// =======================
// Tauri commands
// =======================
#[tauri::command]
fn list_sources(app: AppHandle) -> Result<Vec<SourceRow>, String> {
  let db_path = app_db_path(&app);
  let conn = db::open_db(&db_path).map_err(|e| e.to_string())?;
  db::init_schema(&conn).map_err(|e| e.to_string())?;
  db::list_sources(&conn).map_err(|e| e.to_string())
}

#[tauri::command]
fn set_source_enabled(app: AppHandle, source_id: i64, enabled: bool) -> Result<(), String> {
  let db_path = app_db_path(&app);
  let mut conn = db::open_db(&db_path).map_err(|e| e.to_string())?;
  db::init_schema(&conn).map_err(|e| e.to_string())?;
  db::set_source_enabled(&mut conn, source_id, enabled).map_err(|e| e.to_string())
}

#[tauri::command]
fn delete_source(app: AppHandle, source_id: i64) -> Result<(), String> {
  let db_path = app_db_path(&app);
  let mut conn = db::open_db(&db_path).map_err(|e| e.to_string())?;
  db::init_schema(&conn).map_err(|e| e.to_string())?;
  db::delete_source(&mut conn, source_id).map_err(|e| e.to_string())
}

#[tauri::command]
fn search(app: AppHandle, query: String) -> Result<Vec<SearchRow>, String> {
  let db_path = app_db_path(&app);
  let conn = db::open_db(&db_path).map_err(|e| e.to_string())?;
  db::init_schema(&conn).map_err(|e| e.to_string())?;
  db::search(&conn, &query, 50).map_err(|e| e.to_string())
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct StartIngestArgs {
  path: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct DuplicateDetectedPayload {
  ingest_id: i64,
  new_path: String,
  new_title: String,
  sha256: String,
  existing_id: i64,
  existing_title: String,
  existing_path: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct ResolveDuplicateArgs {
  ingest_id: i64,
  action: String, // "discard" | "replace" | "new_copy"
}

#[derive(Debug, Clone, Copy)]
enum DupAction {
  Discard,
  Replace,
  NewCopy,
}

fn parse_dup_action(s: &str) -> Option<DupAction> {
  match s {
    "discard" => Some(DupAction::Discard),
    "replace" => Some(DupAction::Replace),
    "new_copy" => Some(DupAction::NewCopy),
    _ => None,
  }
}

#[tauri::command]
fn start_ingest_pdf(app: AppHandle, args: StartIngestArgs) -> Result<i64, String> {
  let path = args.path.trim().to_string();
  if path.is_empty() {
    return Err("No file selected.".into());
  }
  if !Path::new(&path).exists() {
    return Err("File does not exist.".into());
  }
  if Path::new(&path)
    .extension()
    .and_then(|s| s.to_str())
    .unwrap_or("")
    .to_lowercase()
    != "pdf"
  {
    return Err("Not a PDF file.".into());
  }

  let ingest_id = next_ingest_id();
  let flag = Arc::new(AtomicBool::new(false));
  {
    let mut m = cancel_map().lock().unwrap();
    m.insert(ingest_id, flag.clone());
  }

  let app_handle = app.clone();
  std::thread::spawn(move || {
    if let Err(e) = ingest_worker(&app_handle, ingest_id, &path, flag.as_ref()) {
      emit_progress(
        &app_handle,
        IngestProgress {
          id: ingest_id,
          stage: "error".into(),
          message: "Ingest failed".into(),
          current: 0,
          total: 0,
          done: true,
          error: Some(e),
        },
      );
    }

    // cleanup cancel map
    let mut m = cancel_map().lock().unwrap();
    m.remove(&ingest_id);

    // cleanup any waiter (if something exploded mid-dup)
    let mut w = dup_waiters().lock().unwrap();
    w.remove(&ingest_id);

    log::info!("ingest_worker exited id={}", ingest_id);
  });

  emit_progress(
    &app,
    IngestProgress {
      id: ingest_id,
      stage: "queued".into(),
      message: format!("Queued (#{}). Hashing / validating…", ingest_id),
      current: 0,
      total: 1,
      done: false,
      error: None,
    },
  );

  Ok(ingest_id)
}

#[tauri::command]
fn cancel_ingest(_app: AppHandle, ingest_id: i64) -> Result<(), String> {
  let m = cancel_map().lock().unwrap();
  if let Some(flag) = m.get(&ingest_id) {
    flag.store(true, Ordering::Relaxed);
    log::warn!("cancel requested ingest_id={}", ingest_id);
    Ok(())
  } else {
    Err("No active ingest with that id.".to_string())
  }
}

#[tauri::command]
fn resolve_duplicate(_app: AppHandle, args: ResolveDuplicateArgs) -> Result<(), String> {
  let action = parse_dup_action(args.action.as_str()).ok_or_else(|| "Invalid action.".to_string())?;

  let mut w = dup_waiters().lock().unwrap();
  if let Some(tx) = w.remove(&args.ingest_id) {
    let _ = tx.send(action);
    log::info!("duplicate resolved ingest_id={} action={}", args.ingest_id, args.action);
    Ok(())
  } else {
    Err("No pending duplicate decision for that ingest_id.".to_string())
  }
}

fn ingest_worker(app: &AppHandle, ingest_id: i64, path: &str, cancel_flag: &AtomicBool) -> Result<(), String> {
  emit_progress(
    app,
    IngestProgress {
      id: ingest_id,
      stage: "validate".into(),
      message: "Validating PDF…".into(),
      current: 0,
      total: 1,
      done: false,
      error: None,
    },
  );

  if cancel_flag.load(Ordering::Relaxed) {
    return Err("Cancelled".into());
  }

  let (pages_from_pdfinfo, encrypted) = pdfinfo(path)?;
  if encrypted {
    return Err("PDF appears to be encrypted/password-protected. Cannot ingest encrypted PDFs.".into());
  }

  let hash = sha256_hex(path, cancel_flag, app, ingest_id)?;
  if cancel_flag.load(Ordering::Relaxed) {
    return Err("Cancelled".into());
  }

  // DB init
  let db_path = app_db_path(app);
  let mut conn = db::open_db(&db_path).map_err(|e| e.to_string())?;
  db::init_schema(&conn).map_err(|e| e.to_string())?;

  // Duplicate detection BEFORE expensive extraction
  if let Ok(Some(existing)) = db::get_source_by_hash(&conn, &hash) {
    let payload = DuplicateDetectedPayload {
      ingest_id,
      new_path: path.to_string(),
      new_title: file_title_from_path(path),
      sha256: hash.clone(),
      existing_id: existing.id,
      existing_title: existing.title,
      existing_path: existing.path,
    };

    let _ = app.emit("duplicate_detected", payload);

    emit_progress(
      app,
      IngestProgress {
        id: ingest_id,
        stage: "duplicate".into(),
        message: "Duplicate detected. Waiting for your choice…".into(),
        current: 0,
        total: 1,
        done: false,
        error: None,
      },
    );

    let (tx, rx) = mpsc::channel::<DupAction>();
    {
      let mut w = dup_waiters().lock().unwrap();
      w.insert(ingest_id, tx);
    }

    // Wait (but remain cancel-able)
    let choice: DupAction = loop {
      if cancel_flag.load(Ordering::Relaxed) {
        let mut w = dup_waiters().lock().unwrap();
        w.remove(&ingest_id);
        return Err("Cancelled".into());
      }

      match rx.recv_timeout(Duration::from_millis(200)) {
        Ok(c) => break c,
        Err(mpsc::RecvTimeoutError::Timeout) => continue,
        Err(_) => return Err("Duplicate decision channel closed.".into()),
      }
    };

    match choice {
      DupAction::Discard => {
        emit_progress(
          app,
          IngestProgress {
            id: ingest_id,
            stage: "done".into(),
            message: "Duplicate detected. Kept original; discarded new ingest.".into(),
            current: 1,
            total: 1,
            done: true,
            error: None,
          },
        );
        return Ok(());
      }
      DupAction::Replace => {
        // delete existing source + chunks, then proceed ingest fresh
        db::delete_source(&mut conn, existing.id).map_err(|e| e.to_string())?;
      }
      DupAction::NewCopy => {
        // proceed ingest as a new row even though hash matches
      }
    }
  }

  // Extract text
  emit_progress(
    app,
    IngestProgress {
      id: ingest_id,
      stage: "extract".into(),
      message: "Extracting text (pdftotext)…".into(),
      current: 0,
      total: pages_from_pdfinfo.max(1),
      done: false,
      error: None,
    },
  );

  let page_texts = pdftotext_all_pages(path)?;
  let page_count = if pages_from_pdfinfo > 0 {
    pages_from_pdfinfo
  } else {
    page_texts.len() as i64
  };

  if cancel_flag.load(Ordering::Relaxed) {
    return Err("Cancelled".into());
  }

  // Create source row now (after possible replacement delete)
  let title = file_title_from_path(path);
  let source_id = db::create_source(&mut conn, &title, path, &hash, page_count, true).map_err(|e| e.to_string())?;

  emit_progress(
    app,
    IngestProgress {
      id: ingest_id,
      stage: "chunk".into(),
      message: "Chunking pages…".into(),
      current: 0,
      total: page_count.max(1),
      done: false,
      error: None,
    },
  );

  let mut all_chunks: Vec<ChunkRow> = Vec::new();

  for (i, txt) in page_texts.iter().enumerate() {
    let page_num = (i as i64) + 1;

    if cancel_flag.load(Ordering::Relaxed) {
      let _ = db::delete_source(&mut conn, source_id);
      return Err("Cancelled".into());
    }

    let heading = pick_heading_from_text(txt);
    let chunks = chunk_text(page_num, heading, txt);
    all_chunks.extend(chunks);

    emit_progress(
      app,
      IngestProgress {
        id: ingest_id,
        stage: "chunk".into(),
        message: format!("Chunking page {}/{}…", page_num, page_count.max(1)),
        current: page_num,
        total: page_count.max(1),
        done: false,
        error: None,
      },
    );
  }

  if cancel_flag.load(Ordering::Relaxed) {
    let _ = db::delete_source(&mut conn, source_id);
    return Err("Cancelled".into());
  }

  emit_progress(
    app,
    IngestProgress {
      id: ingest_id,
      stage: "db".into(),
      message: format!("Writing {} chunks to database…", all_chunks.len()),
      current: 0,
      total: 1,
      done: false,
      error: None,
    },
  );

  db::insert_chunks(&mut conn, source_id, &all_chunks).map_err(|e| e.to_string())?;

  emit_progress(
    app,
    IngestProgress {
      id: ingest_id,
      stage: "done".into(),
      message: format!("Ingest complete. Pages: {} • Chunks: {}", page_count, all_chunks.len()),
      current: 1,
      total: 1,
      done: true,
      error: None,
    },
  );

  Ok(())
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct OpenPdfArgs {
  path: String,
  page: i64,
}

#[tauri::command]
fn open_pdf_at_location(_app: AppHandle, args: OpenPdfArgs) -> Result<(), String> {
  let path = args.path;
  let page = args.page.max(1);

  if !Path::new(&path).exists() {
    return Err("PDF path does not exist.".into());
  }

  // Most PDF viewers honor #page=N
  let uri = format!("file://{}#page={}", path, page);

  // Try opener plugin first
  if tauri_plugin_opener::open_url(uri.clone(), None::<String>).is_ok() {
    return Ok(());
  }

  // Fallback to OS commands
  #[cfg(target_os = "linux")]
  {
    let _ = Command::new("xdg-open").arg(&uri).spawn();
    return Ok(());
  }
  #[cfg(target_os = "macos")]
  {
    let _ = Command::new("open").arg(&uri).spawn();
    return Ok(());
  }
  #[cfg(target_os = "windows")]
  {
    let _ = Command::new("cmd").args(["/C", "start", "", &uri]).spawn();
    return Ok(());
  }

  Err("Open failed.".into())
}

pub fn run() {
  tauri::Builder::default()
    .setup(|app| {
      init_logger_once(&app.handle());
      Ok(())
    })
    .plugin(tauri_plugin_dialog::init())
    .plugin(tauri_plugin_opener::init())
    .invoke_handler(tauri::generate_handler![
      set_log_mode,
      list_sources,
      set_source_enabled,
      delete_source,
      search,
      start_ingest_pdf,
      cancel_ingest,
      resolve_duplicate,
      open_pdf_at_location
    ])
    .run(tauri::generate_context!())
    .expect("error while running tauri application");
}
