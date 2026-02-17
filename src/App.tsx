import { useEffect, useMemo, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { open } from "@tauri-apps/plugin-dialog";

type SourceRow = {
  id: number;
  title: string;
  path: string;
  sha256: string;
  pages: number;
  enabled: number; // 0/1
};

type SearchRow = {
  source_id: number;
  source_title: string;
  source_path: string;
  page_num: number;
  heading: string | null;
  snippet: string;
  loc: string | null;
};

type IngestProgress = {
  id: number;
  stage: string;
  message: string;
  current: number;
  total: number;
  done: boolean;
  error?: string | null;
};

type DuplicateDetectedPayload = {
  ingest_id: number;
  new_path: string;
  new_title?: string;
  sha256?: string;
  existing_id: number;
  existing_title: string;
  existing_path: string;
};

function clampSnippet(s: string, max = 280) {
  const t = (s ?? "").replace(/\s+/g, " ").trim();
  if (t.length <= max) return t;
  return t.slice(0, max - 1) + "…";
}

function formatPath(p: string) {
  return p.length > 70 ? "…" + p.slice(-67) : p;
}

async function pickPdfPath(): Promise<string | null> {
  // Tauri v2 dialog plugin returns `FilePath` (often a string path) or null.
  const picked: any = await open({
    multiple: false,
    directory: false,
    filters: [{ name: "PDF", extensions: ["pdf"] }],
    title: "Select a PDF to ingest",
  });

  if (!picked) return null;

  // Common shapes: string, { path: string }, array variants
  if (typeof picked === "string") return picked;
  if (Array.isArray(picked)) {
    const first = picked[0];
    if (!first) return null;
    if (typeof first === "string") return first;
    if (typeof first?.path === "string") return first.path;
    return String(first);
  }
  if (typeof picked?.path === "string") return picked.path;

  return String(picked);
}

export default function App() {
  const [dark, setDark] = useState(true);

  const [sources, setSources] = useState<SourceRow[]>([]);
  const [sourcesOpen, setSourcesOpen] = useState(true);

  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchRow[]>([]);
  const [status, setStatus] = useState<string>("");

  // Ingest modal state
  const [ingestOpen, setIngestOpen] = useState(false);
  const [ingestPath, setIngestPath] = useState("");
  const [ingestId, setIngestId] = useState<number | null>(null);
  const [progress, setProgress] = useState<IngestProgress | null>(null);

  // Duplicate modal state
  const [dup, setDup] = useState<DuplicateDetectedPayload | null>(null);
  const [dupOpen, setDupOpen] = useState(false);

  // Logging controls (frontend-only UI toggle; backend logging is configured via env/tauri side)
  const [logToConsole, setLogToConsole] = useState(true);
  const [logToFile, setLogToFile] = useState(false);
  const lastLogFlush = useRef<number>(0);

  function uiLog(line: string) {
    const ts = new Date().toISOString();
    const msg = `[ui ${ts}] ${line}`;
    if (logToConsole) console.log(msg);
    // optional: forward to backend logger if implemented as a command "set_logging"
    if (logToFile) {
      const now = Date.now();
      // throttle to avoid spam if user leaves it on
      if (now - lastLogFlush.current > 250) {
        lastLogFlush.current = now;
        invoke("log_line", { args: { line: msg } }).catch(() => {});
      }
    }
  }

  async function refreshSources() {
    try {
      const rows = await invoke<SourceRow[]>("list_sources");
      setSources(rows);
    } catch (e: any) {
      setStatus(`Failed to load sources: ${String(e)}`);
    }
  }

  const enabledCount = useMemo(
    () => sources.filter((s) => (s.enabled ?? 0) === 1).length,
    [sources]
  );

  const selectedCount = sources.length;

  useEffect(() => {
    refreshSources();

    let unlistenProgress: (() => void) | null = null;
    let unlistenDup: (() => void) | null = null;

    listen<IngestProgress>("ingest_progress", (event) => {
      const p = event.payload;
      setProgress(p);
      uiLog(`ingest_progress id=${p.id} stage=${p.stage} ${p.current}/${p.total} done=${p.done} msg=${p.message}`);
      // Track ingestId if we didn't have one (rare, but helps recover if user opens modal late)
      if (ingestId == null && typeof p.id === "number") setIngestId(p.id);

      if (p.done) {
        // Always refresh sources on completion (success or error)
        refreshSources();
      }
    }).then((f) => (unlistenProgress = f));

    listen<DuplicateDetectedPayload>("duplicate_detected", (event) => {
      uiLog(`duplicate_detected for ingest_id=${event.payload.ingest_id} existing_id=${event.payload.existing_id}`);
      setDup(event.payload);
      setDupOpen(true);
      setIngestOpen(true);
      setStatus("Duplicate detected — choose what to do.");
      // Keep progress modal open; user action will resume worker.
    }).then((f) => (unlistenDup = f));

    return () => {
      unlistenProgress?.();
      unlistenDup?.();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [logToConsole, logToFile]);

  async function toggleSourceEnabled(sourceId: number, enabled: boolean) {
    try {
      await invoke("set_source_enabled", { sourceId, source_id: sourceId, enabled });
      await refreshSources();
    } catch (e: any) {
      setStatus(`Failed to update source: ${String(e)}`);
    }
  }

  async function deleteSource(sourceId: number) {
    if (!confirm("Delete this source and all its chunks?")) return;
    try {
      await invoke("delete_source", { sourceId, source_id: sourceId });
      await refreshSources();
      setResults((r) => r.filter((x) => x.source_id !== sourceId));
    } catch (e: any) {
      setStatus(`Failed to delete source: ${String(e)}`);
    }
  }

  async function runSearch() {
    const q = query.trim();
    if (!q) return;

    try {
      setStatus("Searching…");
      const rows = await invoke<SearchRow[]>("search", { query: q });
      setResults(rows);
      setStatus(rows.length ? `Found ${rows.length} result(s).` : "No results.");
    } catch (e: any) {
      setStatus(`Search failed: ${String(e)}`);
    }
  }

  async function openResult(r: SearchRow) {
    try {
      await invoke("open_pdf_at_location", {
        args: {
          path: r.source_path,
          page: r.page_num,
        },
      });
    } catch (e: any) {
      setStatus(`Open failed: ${String(e)}`);
    }
  }

  async function startIngestWithPath(path: string) {
    try {
      setStatus("Starting ingest…");
      setIngestId(null);
      setProgress({
        id: -1,
        stage: "start",
        message: "Starting…",
        current: 0,
        total: 1,
        done: false,
        error: null,
      });

      // IMPORTANT: Rust expects { args: { path } } for StartIngestArgs
      const id = await invoke<number>("start_ingest_pdf", { args: { path } });
      setIngestId(id);
      setStatus(`Ingest queued (#${id}).`);
      uiLog(`start_ingest_pdf -> ${id}`);
    } catch (e: any) {
      setStatus(`Ingest start failed: ${String(e)}`);
      setProgress({
        id: ingestId ?? -1,
        stage: "error",
        message: "Ingest failed to start",
        current: 0,
        total: 0,
        done: true,
        error: String(e),
      });
    }
  }

  async function onPickAndIngest() {
    try {
      const p = await pickPdfPath();
      if (!p) return;
      setIngestPath(p);
      setIngestOpen(true);
      await startIngestWithPath(p);
    } catch (e: any) {
      setStatus(`File picker failed: ${String(e)}`);
    }
  }

  async function cancelIngest() {
    if (ingestId == null) {
      setStatus("Cancel failed: no active ingest id yet.");
      return;
    }
    try {
      await invoke("cancel_ingest", { ingestId });
      setStatus("Cancel requested.");
    } catch (e: any) {
      setStatus(`Cancel failed: ${String(e)}`);
    }
  }

  async function resolveDuplicate(action: "discard" | "replace" | "new_copy") {
    if (!dup) return;
    try {
      // IMPORTANT: Rust expects { args: { ingest_id, action } }
      await invoke("resolve_duplicate", {
        args: { ingest_id: dup.ingest_id, action },
      });
      setDupOpen(false);
      setStatus(
        action === "discard"
          ? "Duplicate: keeping original."
          : action === "replace"
          ? "Duplicate: replacing original (rebuild)…"
          : "Duplicate: ingesting as a new copy…"
      );
    } catch (e: any) {
      setStatus(`Duplicate resolve failed: ${String(e)}`);
    }
  }

  // Basic layout: two-column that scales full window, with panels.
  return (
    <div className={dark ? "app dark" : "app light"}>
      <style>{`
        :root {
          --bg: #f5f6f8;
          --fg: #121417;
          --muted: #5a606b;
          --panel: #ffffff;
          --border: rgba(0,0,0,0.12);
          --shadow: 0 6px 22px rgba(0,0,0,0.10);
          --accent: #3b82f6;
          --danger: #ef4444;
          --ok: #16a34a;
        }
        .dark {
          --bg: #0b0f14;
          --fg: #e7eef7;
          --muted: #93a3b5;
          --panel: #0f1622;
          --border: rgba(255,255,255,0.10);
          --shadow: 0 10px 28px rgba(0,0,0,0.40);
          --accent: #60a5fa;
          --danger: #f87171;
          --ok: #34d399;
        }

        html, body, #root { height: 100%; margin: 0; }
        .app {
          height: 100%;
          display: flex;
          flex-direction: column;
          background: var(--bg);
          color: var(--fg);
          font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, "Apple Color Emoji", "Segoe UI Emoji";
        }

        .topbar {
          padding: 18px 22px;
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
        }

        .title h1 { margin: 0; font-size: 38px; letter-spacing: -0.02em; }
        .title .sub { margin-top: 4px; color: var(--muted); font-size: 13px; }

        .actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
        .btn {
          border: 1px solid var(--border);
          background: var(--panel);
          color: var(--fg);
          padding: 9px 12px;
          border-radius: 10px;
          cursor: pointer;
          box-shadow: none;
        }
        .btn:hover { border-color: rgba(255,255,255,0.20); }
        .btn.primary { background: var(--accent); border-color: transparent; color: #0b0f14; font-weight: 600; }
        .btn.danger { background: transparent; border-color: rgba(239,68,68,0.55); color: var(--danger); }
        .btn.small { padding: 6px 10px; border-radius: 9px; font-size: 12px; }
        .toggle { display:flex; align-items:center; gap:8px; color: var(--muted); font-size: 12px; }

        .content {
          flex: 1;
          padding: 0 22px 22px;
          display: grid;
          grid-template-columns: 360px 1fr;
          gap: 16px;
          min-height: 0; /* important for overflow children */
        }

        .panel {
          background: var(--panel);
          border: 1px solid var(--border);
          border-radius: 16px;
          box-shadow: var(--shadow);
          min-height: 0;
          display: flex;
          flex-direction: column;
        }
        .panelHeader {
          padding: 12px 14px;
          border-bottom: 1px solid var(--border);
          display:flex;
          align-items:center;
          justify-content: space-between;
          gap: 8px;
        }
        .panelHeader h2 { margin:0; font-size: 14px; letter-spacing: 0.02em; text-transform: uppercase; color: var(--muted); }
        .panelBody { padding: 12px 14px; overflow: auto; min-height: 0; }

        .sourceCard {
          border: 1px solid var(--border);
          border-radius: 14px;
          padding: 12px;
          margin-bottom: 10px;
          background: rgba(255,255,255,0.02);
        }
        .sourceTitle { font-weight: 700; margin: 0 0 4px 0; }
        .sourcePath { color: var(--muted); font-size: 12px; margin: 0 0 8px 0; }
        .sourceMeta { color: var(--muted); font-size: 12px; display:flex; justify-content: space-between; gap:8px; }
        .row { display:flex; align-items:center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
        .chk { display:flex; align-items:center; gap: 8px; font-size: 13px; }

        .searchBar {
          display:flex; gap: 10px; align-items: center; padding: 12px 14px; border-bottom: 1px solid var(--border);
        }
        .input {
          flex: 1;
          border: 1px solid var(--border);
          background: rgba(255,255,255,0.03);
          color: var(--fg);
          padding: 10px 12px;
          border-radius: 12px;
          outline: none;
        }
        .input::placeholder { color: rgba(147,163,181,0.8); }

        .results { padding: 12px 14px; overflow: auto; min-height: 0; }
        .resultCard {
          border: 1px solid var(--border);
          border-radius: 14px;
          padding: 12px;
          margin-bottom: 10px;
          cursor: pointer;
          background: rgba(255,255,255,0.02);
        }
        .resultCard:hover { border-color: rgba(96,165,250,0.55); }
        .resultTop { display:flex; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
        .resultHeading { font-weight: 800; }
        .resultSrc { color: var(--muted); font-weight: 600; }
        .resultMeta { color: var(--muted); font-size: 12px; margin-top: 4px; }
        .resultSnippet { margin-top: 8px; color: var(--fg); opacity: 0.92; }

        .status {
          padding: 10px 22px 0;
          color: var(--muted);
          font-size: 12px;
          min-height: 18px;
        }

        /* modal */
        .overlay {
          position: fixed;
          inset: 0;
          background: rgba(0,0,0,0.55);
          display: flex;
          align-items: center;
          justify-content: center;
          padding: 20px;
          z-index: 50;
        }
        .modal {
          width: min(820px, 96vw);
          background: var(--panel);
          border: 1px solid var(--border);
          border-radius: 18px;
          box-shadow: var(--shadow);
          overflow: hidden;
        }
        .modalHeader {
          padding: 14px 16px;
          border-bottom: 1px solid var(--border);
          display:flex;
          align-items:center;
          justify-content: space-between;
          gap: 10px;
        }
        .modalHeader h3 { margin: 0; font-size: 16px; }
        .modalBody { padding: 14px 16px; }
        .modalActions { display:flex; gap: 10px; justify-content: flex-end; flex-wrap: wrap; margin-top: 12px; }

        .progressWrap { margin-top: 10px; }
        .barOuter { height: 10px; border-radius: 999px; background: rgba(255,255,255,0.08); border:1px solid var(--border); overflow: hidden; }
        .barInner { height: 100%; background: var(--accent); width: 0%; border-radius: 999px; transition: width 140ms linear; }

        @media (max-width: 900px) {
          .content { grid-template-columns: 1fr; }
        }
      `}</style>

      <div className="topbar">
        <div className="title">
          <h1>Codex Engine</h1>
          <div className="sub">
            Rulebook library (local) • Active sources: {enabledCount}/{selectedCount}
          </div>
        </div>

        <div className="actions">
          <button className="btn primary" onClick={onPickAndIngest}>
            Ingest PDF
          </button>
          <button className="btn" onClick={refreshSources}>
            Refresh
          </button>

          <button className="btn" onClick={() => setDark((d) => !d)}>
            {dark ? "Light mode" : "Dark mode"}
          </button>

          <div className="toggle" title="UI logging toggles (optional). If backend logging exists, file logging forwards lines to Rust.">
            <label className="chk">
              <input
                type="checkbox"
                checked={logToConsole}
                onChange={(e) => setLogToConsole(e.target.checked)}
              />
              Console logs
            </label>
            <label className="chk">
              <input
                type="checkbox"
                checked={logToFile}
                onChange={(e) => setLogToFile(e.target.checked)}
              />
              File logs
            </label>
          </div>
        </div>
      </div>

      <div className="status">{status}</div>

      <div className="content">
        <div className="panel">
          <div className="panelHeader">
            <h2>Sources</h2>
            <div className="row">
              <button className="btn small" onClick={() => setSourcesOpen((v) => !v)}>
                {sourcesOpen ? "Collapse" : "Expand"}
              </button>
            </div>
          </div>

          <div className="panelBody" style={{ display: sourcesOpen ? "block" : "none" }}>
            {sources.length === 0 ? (
              <div style={{ color: "var(--muted)", fontSize: 13 }}>
                No sources yet. Ingest a PDF to begin.
              </div>
            ) : (
              sources.map((s) => (
                <div className="sourceCard" key={s.id}>
                  <div className="sourceTitle">{s.title}</div>
                  <div className="sourcePath" title={s.path}>
                    {formatPath(s.path)}
                  </div>
                  <div className="sourceMeta">
                    <span>pages: {s.pages ?? 0}</span>
                    <span>sha: {(s.sha256 ?? "").slice(0, 10)}…</span>
                  </div>

                  <div className="row" style={{ marginTop: 10 }}>
                    <label className="chk">
                      <input
                        type="checkbox"
                        checked={(s.enabled ?? 0) === 1}
                        onChange={(e) => toggleSourceEnabled(s.id, e.target.checked)}
                      />
                      Enabled
                    </label>

                    <div className="row">
                      <button
                        className="btn small danger"
                        onClick={() => deleteSource(s.id)}
                      >
                        Delete
                      </button>
                    </div>
                  </div>
                </div>
              ))
            )}
          </div>
        </div>

        <div className="panel">
          <div className="searchBar">
            <input
              className="input"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search… (e.g., kenku, mothman)"
              onKeyDown={(e) => {
                if (e.key === "Enter") runSearch();
              }}
            />
            <button className="btn primary" onClick={runSearch}>
              Search
            </button>
            <button
              className="btn"
              onClick={() => {
                setQuery("");
                setResults([]);
                setStatus("");
              }}
            >
              Clear
            </button>
          </div>

          <div className="results">
            {results.length === 0 ? (
              <div style={{ color: "var(--muted)", fontSize: 13 }}>
                No results yet.
              </div>
            ) : (
              results.map((r, idx) => (
                <div
                  className="resultCard"
                  key={`${r.source_id}-${r.page_num}-${idx}`}
                  onDoubleClick={() => openResult(r)}
                  title="Double-click to open PDF at this page"
                >
                  <div className="resultTop">
                    <div className="resultHeading">
                      {(r.heading && r.heading.trim()) || "(No heading)"}
                    </div>
                    <div className="resultSrc">— {r.source_title}</div>
                  </div>
                  <div className="resultMeta">
                    p. {r.page_num} • {formatPath(r.source_path)}
                  </div>
                  <div className="resultSnippet">{clampSnippet(r.snippet)}</div>
                </div>
              ))
            )}
          </div>
        </div>
      </div>

      {/* Ingest modal */}
      {ingestOpen && (
        <div className="overlay" onMouseDown={() => { /* click-out disabled */ }}>
          <div className="modal" onMouseDown={(e) => e.stopPropagation()}>
            <div className="modalHeader">
              <h3>Ingest PDF</h3>
              <div className="row">
                <button className="btn small" onClick={() => setIngestOpen(false)}>
                  Close
                </button>
                <button className="btn small danger" onClick={cancelIngest}>
                  Cancel ingest
                </button>
              </div>
            </div>

            <div className="modalBody">
              <div className="row" style={{ alignItems: "stretch" }}>
                <input
                  className="input"
                  value={ingestPath}
                  onChange={(e) => setIngestPath(e.target.value)}
                  placeholder="/path/to/book.pdf"
                />
                <button
                  className="btn"
                  onClick={async () => {
                    const p = await pickPdfPath();
                    if (p) setIngestPath(p);
                  }}
                >
                  Browse…
                </button>
                <button
                  className="btn primary"
                  onClick={() => startIngestWithPath(ingestPath)}
                >
                  Start ingest
                </button>
              </div>

              <div className="progressWrap">
                <div style={{ color: "var(--muted)", fontSize: 12 }}>
                  {progress
                    ? `Stage: ${progress.stage} • ${progress.message}`
                    : "Waiting…"}
                </div>

                <div style={{ marginTop: 10 }}>
                  <div className="barOuter">
                    <div
                      className="barInner"
                      style={{
                        width:
                          progress && progress.total > 0
                            ? `${Math.min(
                                100,
                                Math.round((progress.current / progress.total) * 100)
                              )}%`
                            : "0%",
                      }}
                    />
                  </div>
                  <div style={{ marginTop: 6, color: "var(--muted)", fontSize: 12 }}>
                    {progress && progress.total > 0
                      ? `${progress.current} / ${progress.total} (${Math.min(
                          100,
                          Math.round((progress.current / progress.total) * 100)
                        )}%)`
                      : "0 / 0 (0%)"}
                  </div>

                  {progress?.error ? (
                    <div style={{ marginTop: 8, color: "var(--danger)" }}>
                      {String(progress.error)}
                    </div>
                  ) : null}
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Duplicate modal */}
      {dupOpen && dup && (
        <div className="overlay">
          <div className="modal" onMouseDown={(e) => e.stopPropagation()}>
            <div className="modalHeader">
              <h3>Duplicate detected</h3>
              <button className="btn small" onClick={() => setDupOpen(false)}>
                Close
              </button>
            </div>
            <div className="modalBody">
              <div style={{ color: "var(--muted)", fontSize: 13, lineHeight: 1.4 }}>
                The file you selected matches an existing source (same SHA-256).
                Choose what you want to do:
              </div>

              <div style={{ marginTop: 10, fontSize: 13 }}>
                <div>
                  <b>Existing:</b> {dup.existing_title}
                </div>
                <div style={{ color: "var(--muted)" }} title={dup.existing_path}>
                  {dup.existing_path}
                </div>
                <div style={{ marginTop: 10 }}>
                  <b>New file:</b> {dup.new_title ?? "(selected file)"}
                </div>
                <div style={{ color: "var(--muted)" }} title={dup.new_path}>
                  {dup.new_path}
                </div>
              </div>

              <div className="modalActions">
                <button className="btn" onClick={() => resolveDuplicate("discard")}>
                  Discard new (keep existing)
                </button>
                <button className="btn danger" onClick={() => resolveDuplicate("replace")}>
                  Replace existing (rebuild)
                </button>
                <button className="btn primary" onClick={() => resolveDuplicate("new_copy")}>
                  Ingest as new copy
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

