import { useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  type AIStatus,
  type DuplicateDetectedPayload,
  type IngestProgress,
  type AIPending,
  type ModelPull,
  type ReaderView,
  type PageView,
  type SearchRow,
  type SourceRow,
  type VersionInfo,
} from "./api";
import { highlightText, markedSnippet, queryPattern } from "./highlight";
import AIProgressWindow, { type AIRun } from "./AIProgressWindow";
import ChangesView from "./ChangesView";
import Markdown from "./Markdown";
import SettingsPanel from "./SettingsPanel";
import appLogo from "../assets/codex-engine-logo-1024.png";

const FRONTEND_VERSION = import.meta.env.PACKAGE_VERSION ?? "dev";

function pickPdfFile(): Promise<File | null> {
  return new Promise((resolve) => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "application/pdf,.pdf";
    input.onchange = () => resolve(input.files?.[0] ?? null);
    input.oncancel = () => resolve(null);
    input.click();
  });
}
const VIEW_LABELS: Record<ReaderView, string> = {
  edited: "Your edit",
  ai: "AI formatted",
  clean: "Cleaned",
  raw: "Raw extraction",
  changes: "What the AI changed",
};

const SEARCH_LIMIT = 50; // the backend returns at most this many results

const THEME_KEY = "codex-engine.theme";

function loadDarkTheme(): boolean {
  try {
    return localStorage.getItem(THEME_KEY) !== "light";
  } catch {
    return true; // storage unavailable: default to dark
  }
}

export default function App() {
  const [dark, setDark] = useState(loadDarkTheme);
  useEffect(() => {
    try {
      localStorage.setItem(THEME_KEY, dark ? "dark" : "light");
    } catch {
      // remembering the theme is a convenience only
    }
  }, [dark]);

  const [sources, setSources] = useState<SourceRow[]>([]);
  const [sourcesOpen, setSourcesOpen] = useState(true);

  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchRow[]>([]);
  const [status, setStatus] = useState<string>("");
  const [checkingUpdates, setCheckingUpdates] = useState(false);
  const [versionInfo, setVersionInfo] = useState<VersionInfo | null>(null);

  // Ingest modal state
  const [ingestOpen, setIngestOpen] = useState(false);
  const [ingestPath, setIngestPath] = useState("");
  const [ingestId, setIngestId] = useState<number | null>(null);
  const [progress, setProgress] = useState<IngestProgress | null>(null);
  // Plain-language history of the current import (one line per step) and when it started.
  const [ingestLog, setIngestLog] = useState<{ at: number; message: string }[]>([]);
  const [ingestStarted, setIngestStarted] = useState<number | null>(null);
  const [ingestNow, setIngestNow] = useState(Date.now());

  // Duplicate modal state
  const [dup, setDup] = useState<DuplicateDetectedPayload | null>(null);
  const [dupOpen, setDupOpen] = useState(false);

  // Page reader state
  const [reader, setReader] = useState<{ sourceId: number; page: number } | null>(null);
  const [readerPage, setReaderPage] = useState<PageView | null>(null);
  const [readerView, setReaderView] = useState<ReaderView>("clean");
  const [readerError, setReaderError] = useState("");
  const [aiNotice, setAiNotice] = useState(""); // AI unavailable/rejected: informational, not an app error
  const [readerLoading, setReaderLoading] = useState(false);
  // The page being AI formatted (null when idle) and its live "section n of total" progress.
  // The current (or last) AI formatting run, shown in the progress window.
  const [aiRun, setAiRun] = useState<AIRun | null>(null);
  const aiBusy = Boolean(aiRun && !aiRun.finished);
  // Sections the AI still couldn't format after its automatic retries: the user decides what next.
  const [aiDecision, setAiDecision] = useState<{ sourceId: number; page: number; pending: AIPending } | null>(null);
  // Manual edit in progress (the page text being edited), and whether it differs from where it started.
  const [editing, setEditing] = useState<{ text: string; original: string } | null>(null);
  const [savingEdit, setSavingEdit] = useState(false);
  // The search the current results came from: its terms are highlighted in the reader.
  const [searchedFor, setSearchedFor] = useState("");
  const highlight = useMemo(() => queryPattern(searchedFor), [searchedFor]);
  const readerBodyRef = useRef<HTMLDivElement>(null);
  const [matchCount, setMatchCount] = useState(0);
  const [aiStatus, setAiStatus] = useState<AIStatus | null>(null);
  const readerRequest = useRef(0); // ignore responses for pages we've navigated away from

  const [settingsOpen, setSettingsOpen] = useState(false);
  const [modelPull, setModelPull] = useState<ModelPull | null>(null);

  // Logging controls for frontend diagnostics.
  const [logToConsole, setLogToConsole] = useState(false);
  const [logToFile, setLogToFile] = useState(false);
  // Refs so long-lived callbacks (the event stream) always see the current toggles
  // without tearing down and reconnecting every time a checkbox changes.
  const logToConsoleRef = useRef(logToConsole);
  const logToFileRef = useRef(logToFile);
  logToConsoleRef.current = logToConsole;
  logToFileRef.current = logToFile;

  function uiLog(line: string) {
    const ts = new Date().toISOString();
    const msg = `[ui ${ts}] ${line}`;
    if (logToConsoleRef.current) {
      console.log(msg);
      window.codexEngine?.log(msg);
    }
    if (logToFileRef.current) {
      // Written to <userData>/logs/ui.log by the Electron main process.
      window.codexEngine?.logToFile?.(msg);
    }
  }

  async function refreshSources() {
    try {
      uiLog("refresh_sources start");
      const rows = await api.listSources();
      setSources(rows);
      uiLog(`refresh_sources ok count=${rows.length}`);
    } catch (e: any) {
      uiLog(`refresh_sources failed ${String(e)}`);
      setStatus(`Failed to load sources: ${String(e)}`);
    }
  }

  const enabledCount = useMemo(
    () => sources.filter((s) => (s.enabled ?? 0) === 1).length,
    [sources]
  );

  const selectedCount = sources.length;

  useEffect(() => {
    window.codexEngine?.setConsoleOpen(logToConsole);
  }, [logToConsole]);

  useEffect(() => {
    const listener = () => setLogToConsole(false);
    window.addEventListener("codex-engine:console-closed", listener);
    return () => window.removeEventListener("codex-engine:console-closed", listener);
  }, []);

  useEffect(() => {
    refreshSources();
    api
      .getVersion()
      .then((version) => setVersionInfo({ ...version, frontend_version: FRONTEND_VERSION }))
      .catch((e: any) => setStatus(`Version check failed: ${String(e)}`));

    const events = new EventSource(api.eventsUrl);

    events.addEventListener("ingest_progress", (event) => {
      const p = JSON.parse((event as MessageEvent).data) as IngestProgress;
      setProgress(p);
      setIngestLog((log) => {
        // One line per step: per-page updates within a step replace the step's line.
        const message = p.error ? `Failed: ${p.error}` : p.done ? `Finished: ${p.message}` : p.message;
        const last = log[log.length - 1];
        const sameStep = last && !p.done && !p.error && last.message.split(/\d/)[0] === message.split(/\d/)[0];
        const entry = { at: Date.now(), message };
        return sameStep ? [...log.slice(0, -1), entry] : [...log, entry].slice(-50);
      });
      uiLog(`ingest_progress id=${p.id} stage=${p.stage} ${p.current}/${p.total} done=${p.done} msg=${p.message}`);
      if (typeof p.id === "number") setIngestId((current) => current ?? p.id);
      if (p.done) refreshSources();
    });

    events.addEventListener("duplicate_detected", (event) => {
      const payload = JSON.parse((event as MessageEvent).data) as DuplicateDetectedPayload;
      uiLog(`duplicate_detected for ingest_id=${payload.ingest_id} existing_id=${payload.existing_id}`);
      setDup(payload);
      setDupOpen(true);
      setIngestOpen(true);
      setStatus("Duplicate detected - choose what to do.");
    });

    events.addEventListener("model_pull", (event) => {
      const p = JSON.parse((event as MessageEvent).data) as ModelPull;
      setModelPull(p);
      if (p.done) uiLog(`model_pull ${p.model} ${p.error ? `failed: ${p.error}` : "done"}`);
    });

    events.addEventListener("ai_format_progress", (event) => {
      const p = JSON.parse((event as MessageEvent).data) as {
        source_id: number;
        page_num: number;
        done: number;
        total: number;
        attempt: number;
        max_attempts: number;
        written: number;
        expected: number;
      };
      setAiRun((run) =>
        run && !run.finished && run.sourceId === p.source_id && run.page === p.page_num
          ? { ...run, done: p.done, total: p.total, attempt: p.attempt, maxAttempts: p.max_attempts, written: p.written, expected: p.expected }
          : run
      );
    });

    events.addEventListener("ai_format_log", (event) => {
      const p = JSON.parse((event as MessageEvent).data) as { source_id: number; page_num: number; message: string; at: number };
      setAiRun((run) =>
        run && run.sourceId === p.source_id && run.page === p.page_num
          ? { ...run, log: [...run.log, { at: p.at * 1000, message: p.message }].slice(-200) }
          : run
      );
    });

    events.addEventListener("ai_status", (event) => {
      setAiStatus(JSON.parse((event as MessageEvent).data) as AIStatus);
    });

    events.onerror = () => {
      uiLog("backend event stream disconnected");
    };

    // Backend shutdown is owned by the Electron main process (it holds the token).
    return () => {
      events.close();
    };
  }, []);

  async function toggleSourceEnabled(sourceId: number, enabled: boolean) {
    try {
      uiLog(`set_source_enabled id=${sourceId} enabled=${enabled}`);
      await api.setSourceEnabled(sourceId, enabled);
      await refreshSources();
    } catch (e: any) {
      uiLog(`set_source_enabled failed id=${sourceId} ${String(e)}`);
      setStatus(`Failed to update source: ${String(e)}`);
    }
  }

  async function deleteSource(sourceId: number) {
    if (!confirm("Delete this source and all its chunks?")) return;
    try {
      uiLog(`delete_source id=${sourceId}`);
      await api.deleteSource(sourceId);
      await refreshSources();
      setResults((r) => r.filter((x) => x.source_id !== sourceId));
    } catch (e: any) {
      uiLog(`delete_source failed id=${sourceId} ${String(e)}`);
      setStatus(`Failed to delete source: ${String(e)}`);
    }
  }

  async function runSearch() {
    const q = query.trim();
    if (!q) return;

    try {
      uiLog(`search start query="${q}"`);
      setStatus("Searching…");
      const rows = await api.search(q);
      setResults(rows);
      setSearchedFor(q);
      setStatus(
        rows.length >= SEARCH_LIMIT
          ? `Showing the first ${SEARCH_LIMIT} results (best matches first). Add more words to narrow the search.`
          : rows.length
          ? `Found ${rows.length} result(s).`
          : "No results."
      );
      uiLog(`search ok query="${q}" results=${rows.length}`);
    } catch (e: any) {
      uiLog(`search failed query="${q}" ${String(e)}`);
      setStatus(`Search failed: ${String(e)}`);
    }
  }

  async function checkForUpdates() {
    try {
      uiLog("update_check start");
      setCheckingUpdates(true);
      setStatus("Checking GitHub for updates...");
      const update = await api.checkForUpdate();
      uiLog(`update_check result status=${update.status} current=${update.current_version} latest=${update.latest_version}`);

      if (update.status === "current") {
        setStatus(`Codex Engine is up to date (${update.current_version}).`);
        return;
      }

      if (update.status === "no_release") {
        setStatus("No GitHub release is published yet. Create a release and attach the CodexEngineSetup installer to enable updates.");
        return;
      }

      if (update.status === "missing_installer") {
        setStatus(`Version ${update.latest_version} is available, but no ${update.platform ?? "current platform"} installer asset was found${update.expected_asset ? ` (${update.expected_asset})` : ""}.`);
        return;
      }

      if (update.status === "update_available") {
        const shouldUpdate = confirm(
          `Codex Engine ${update.latest_version} is available. Download and install it now? The app will close while updating.`
        );
        if (!shouldUpdate) {
          setStatus(`Update ${update.latest_version} is available.`);
          return;
        }

        setStatus(`Downloading Codex Engine ${update.latest_version}...`);
        await api.applyUpdate();
        setStatus("Updater launched. Codex Engine will close to finish updating.");
        setTimeout(() => window.close(), 750);
      }
    } catch (e: any) {
      uiLog(`update_check failed ${String(e)}`);
      setStatus(`Update check failed: ${String(e)}`);
    } finally {
      setCheckingUpdates(false);
    }
  }

  async function openPdf(path: string, page: number) {
    try {
      uiLog(`open_pdf path="${path}" page=${page}`);
      await api.openPdfAtLocation(path, page);
    } catch (e: any) {
      uiLog(`open_pdf failed path="${path}" page=${page} ${String(e)}`);
      setReaderError(`Open failed: ${String(e)}`);
    }
  }

  async function loadReaderPage(sourceId: number, page: number) {
    const requestId = ++readerRequest.current;
    setReader({ sourceId, page });
    setReaderLoading(true);
    setReaderError("");
    setAiNotice("");
    setEditing(null);
    try {
      uiLog(`reader_load source=${sourceId} page=${page}`);
      const data = await api.getPage(sourceId, page);
      if (requestId !== readerRequest.current) return;
      setReaderPage(data);
      setReaderView(data.best);
    } catch (e: any) {
      if (requestId !== readerRequest.current) return;
      uiLog(`reader_load failed source=${sourceId} page=${page} ${String(e)}`);
      setReaderPage(null);
      setReaderError(String(e));
    } finally {
      if (requestId === readerRequest.current) setReaderLoading(false);
    }
  }

  function openReader(r: SearchRow) {
    setReaderPage(null);
    loadReaderPage(r.source_id, r.page_num);
    api.aiStatus().then(setAiStatus).catch(() => setAiStatus(null));
  }

  // Unsaved manual edits are never thrown away silently.
  function confirmDiscardEdit(): boolean {
    return !editing || editing.text === editing.original || confirm("Discard your unsaved changes to this page?");
  }

  function closeReader() {
    if (!confirmDiscardEdit()) return;
    readerRequest.current++;
    setEditing(null);
    setReader(null);
    setReaderPage(null);
  }

  function turnReaderPage(delta: number) {
    if (!reader || !readerPage || !confirmDiscardEdit()) return;
    const next = reader.page + delta;
    if (next < 1 || next > readerPage.page_count) return;
    loadReaderPage(reader.sourceId, next);
  }

  // decision: undefined = the button (first run / regenerate), "retry" = keep trying the failed
  // sections, "clean" = keep the cleaned text for them.
  async function aiFormatReaderPage(decision?: "retry" | "clean") {
    if (!reader) return;
    const { sourceId, page } = reader;
    const requestId = readerRequest.current;
    // The button on a page that already has current AI output means "regenerate": skip the cache.
    // A follow-up decision must not: the sections that already passed are reused from the cache.
    const force = !decision && Boolean(readerPage?.ai_md && !readerPage.ai_stale);
    setAiRun({
      sourceId,
      page,
      title: readerPage?.title ?? "",
      startedAt: Date.now(),
      done: 0,
      total: 0,
      attempt: 0,
      maxAttempts: 0,
      written: 0,
      expected: 0,
      log: [],
    });
    const finish = (kind: NonNullable<AIRun["finished"]>["kind"], message: string) =>
      setAiRun((run) => (run && run.sourceId === sourceId && run.page === page ? { ...run, finished: { kind, message } } : run));
    setAiDecision(null);
    setAiNotice("");
    try {
      uiLog(`ai_format start source=${sourceId} page=${page} force=${force} decision=${decision ?? ""}`);
      const data = await api.aiFormatPage(sourceId, page, { model: aiStatus?.model, force, useCleanForFailed: decision === "clean" });
      uiLog(`ai_format done source=${sourceId} page=${page} model=${data.ai_model} pending=${data.pending?.failed.length ?? 0}`);
      if (data.pending) setAiDecision({ sourceId, page, pending: data.pending });
      if (data.pending) {
        finish("decision", `Couldn't format ${data.pending.failed.length} of ${data.pending.sections} part(s) without changing the text. Your choice is waiting on the page.`);
      } else if (data.ai_error) {
        finish(data.ai_md ? "partial" : "error", data.ai_error);
      } else {
        finish(
          "ok",
          data.ai_changes
            ? data.ai_changes.meaningful
              ? `Done. ${data.ai_changes.summary} Open "Changes" in the reader to see them.`
              : data.ai_changes.summary
            : "Done. The AI-formatted page passed the word-for-word check."
        );
      }
      if (requestId !== readerRequest.current) {
        // The user kept reading elsewhere; the result is saved (or waits for them) on that page.
        setAiNotice(
          data.pending
            ? `Page ${page} needs your decision: the AI couldn't format part of it.`
            : `Page ${page} has finished AI formatting.`
        );
        return;
      }
      setReaderPage(data);
      setReaderView(data.best);
      if (data.ai_error && !data.pending) setAiNotice(data.ai_error);
    } catch (e: any) {
      // 409 = cancelled by the user; 503 = Ollama not running / no model. The reader keeps working.
      const message = String(e).replace(/^Error: /, "");
      uiLog(`ai_format failed source=${sourceId} page=${page} ${message}`);
      finish(/^Cancelled/.test(message) ? "cancelled" : "error", message);
      if (!/^Cancelled/.test(message)) setAiNotice(message);
      api.aiStatus().then(setAiStatus).catch(() => setAiStatus(null));
    }
  }

  async function cancelAiFormat() {
    if (!aiRun || aiRun.finished) return;
    try {
      await api.cancelAiFormat(aiRun.sourceId, aiRun.page);
    } catch (e: any) {
      uiLog(`ai_format cancel failed ${String(e)}`);
    }
  }

  // A successful run's window closes itself after a few seconds; anything else stays until closed.
  useEffect(() => {
    if (aiRun?.finished?.kind !== "ok") return;
    const timer = window.setTimeout(() => setAiRun((run) => (run?.finished?.kind === "ok" ? null : run)), 6000);
    return () => window.clearTimeout(timer);
  }, [aiRun?.finished?.kind]);

  async function saveEdit() {
    if (!reader || !editing) return;
    setSavingEdit(true);
    try {
      const data = await api.saveEdit(reader.sourceId, reader.page, editing.text);
      setReaderPage(data);
      setReaderView("edited");
      setEditing(null);
      setAiDecision(null);
      uiLog(`page_edit saved source=${reader.sourceId} page=${reader.page}`);
    } catch (e: any) {
      setReaderError(`Couldn't save your edit: ${String(e).replace(/^Error: /, "")}`);
    } finally {
      setSavingEdit(false);
    }
  }

  async function revertEdit() {
    if (!reader || !confirm("Remove your edit and go back to the automatic version of this page?")) return;
    try {
      const data = await api.revertEdit(reader.sourceId, reader.page);
      setReaderPage(data);
      setReaderView(data.best);
    } catch (e: any) {
      setReaderError(`Couldn't revert: ${String(e).replace(/^Error: /, "")}`);
    }
  }

  function startEdit(text?: string) {
    if (!readerPage) return;
    const current =
      text ??
      (readerView === "edited" && readerPage.edited_md != null
        ? readerPage.edited_md
        : readerView === "ai" && readerPage.ai_md
        ? readerPage.ai_md
        : readerView === "raw" && readerPage.raw_text
        ? readerPage.raw_text
        : readerPage.clean_md);
    setEditing({ text: current, original: current });
  }

  // Jump to the first search match whenever a page (or text version) is shown.
  useEffect(() => {
    const body = readerBodyRef.current;
    if (!body || !readerPage) return;
    const marks = body.querySelectorAll("mark");
    setMatchCount(marks.length);
    if (marks.length) marks[0].scrollIntoView({ block: "center" });
    else body.scrollTop = 0;
  }, [readerPage?.source_id, readerPage?.page_num, readerPage?.ai_md, readerView, highlight]);

  useEffect(() => {
    if (!reader) return;
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (target && (target.tagName === "TEXTAREA" || target.tagName === "INPUT")) return;
      if (e.key === "Escape") closeReader();
      else if (e.key === "ArrowLeft") turnReaderPage(-1);
      else if (e.key === "ArrowRight") turnReaderPage(1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  function resetIngestLog() {
    setIngestLog([]);
    setIngestStarted(Date.now());
  }

  useEffect(() => {
    if (!ingestOpen || !progress || progress.done) return;
    const timer = window.setInterval(() => setIngestNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [ingestOpen, progress?.done]);

  async function startIngestWithPath(path: string) {
    resetIngestLog();
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

      const { id } = await api.startIngest(path);
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

  async function startIngestWithFile(file: File) {
    resetIngestLog();
    try {
      setStatus("Uploading PDF...");
      setIngestId(null);
      setIngestPath(file.name);
      setIngestOpen(true);
      setProgress({
        id: -1,
        stage: "upload",
        message: "Uploading...",
        current: 0,
        total: 1,
        done: false,
        error: null,
      });
      const { id, path } = await api.uploadAndIngest(file);
      setIngestId(id);
      setIngestPath(path);
      setStatus(`Ingest queued (#${id}).`);
      uiLog(`upload_and_ingest -> ${id}`);
    } catch (e: any) {
      setStatus(`Upload failed: ${String(e)}`);
      setProgress({
        id: ingestId ?? -1,
        stage: "error",
        message: "Upload failed",
        current: 0,
        total: 0,
        done: true,
        error: String(e),
      });
    }
  }

  async function onPickAndIngest() {
    try {
      const file = await pickPdfFile();
      if (!file) return;
      await startIngestWithFile(file);
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
      await api.cancelIngest(ingestId);
      setStatus("Cancel requested.");
    } catch (e: any) {
      setStatus(`Cancel failed: ${String(e)}`);
    }
  }

  async function resolveDuplicate(action: "discard" | "replace" | "new_copy") {
    if (!dup) return;
    try {
      await api.resolveDuplicate(dup.ingest_id, action);
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
          padding: 14px 22px;
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
        }

        .brand {
          display: flex;
          align-items: center;
          gap: 14px;
          min-width: 0;
        }
        .brandLogo {
          width: 44px;
          height: 44px;
          object-fit: contain;
          flex: 0 0 auto;
          filter: drop-shadow(0 5px 14px rgba(0,0,0,0.30));
        }
        .title { min-width: 0; }
        .title h1 { margin: 0; font-size: 28px; letter-spacing: -0.02em; line-height: 1.1; white-space: nowrap; }
        .title .sub { margin-top: 3px; color: var(--muted); font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        mark { background: rgba(250, 204, 21, 0.35); color: inherit; border-radius: 3px; padding: 0 1px; }
        .light mark { background: rgba(250, 204, 21, 0.55); }

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
        .btn:disabled { opacity: 0.55; cursor: not-allowed; }
        .btn.primary { background: var(--accent); border-color: transparent; color: #0b0f14; font-weight: 600; }
        .btn.danger { background: transparent; border-color: rgba(239,68,68,0.55); color: var(--danger); }
        .btn.small { padding: 6px 10px; border-radius: 9px; font-size: 12px; }
        .toggle { display:flex; align-items:center; gap:8px; color: var(--muted); font-size: 12px; }

        .content {
          flex: 1;
          padding: 0 22px 22px;
          display: grid;
          grid-template-columns: minmax(240px, 320px) 1fr;
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
        .resultSrc { color: var(--muted); font-weight: 600; font-size: 13px; }
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

        /* page reader */
        .reader { width: min(980px, 96vw); height: min(88vh, 1100px); display: flex; flex-direction: column; }
        .readerTitle { min-width: 0; }
        .readerTitle h3 { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .readerSub { color: var(--muted); font-size: 12px; margin-top: 2px; }
        .readerBar { padding: 10px 16px; border-bottom: 1px solid var(--border); display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
        .readerBar .spacer { flex: 1; }
        .seg { display: inline-flex; border: 1px solid var(--border); border-radius: 9px; overflow: hidden; }
        .seg button { border: 0; background: transparent; color: var(--muted); padding: 6px 10px; font-size: 12px; cursor: pointer; }
        .seg button.on { background: var(--accent); color: #0b0f14; font-weight: 600; }
        .seg button:disabled { opacity: 0.45; cursor: not-allowed; }
        .readerBody { flex: 1; overflow: auto; padding: 18px 28px 28px; min-height: 0; }
        .readerNote { color: var(--muted); font-size: 12px; }
        .readerError { color: var(--danger); font-size: 13px; margin-bottom: 10px; white-space: pre-wrap; }
        .aiWindow { position: fixed; right: 18px; bottom: 18px; width: min(440px, calc(100vw - 36px)); z-index: 60; background: var(--panel); border: 1px solid var(--accent); border-radius: 14px; box-shadow: var(--shadow); padding: 12px 14px; display: flex; flex-direction: column; gap: 8px; font-size: 13px; }
        .aiWindow.done-ok { border-color: var(--ok); }
        .aiWindow.done-error, .aiWindow.done-cancelled { border-color: var(--border); }
        .aiWindowHeader { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; }
        .aiWindowTitle { display: flex; flex-direction: column; min-width: 0; }
        .aiWindowTitle .hint { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .aiWindowStatus { display: flex; justify-content: space-between; gap: 10px; align-items: baseline; }
        .aiWindowLog { margin: 0; padding: 8px 10px; list-style: none; max-height: 150px; overflow: auto; border-radius: 8px; background: rgba(127,127,127,0.08); font-size: 12px; display: flex; flex-direction: column; gap: 3px; }
        .aiWindowActions { display: flex; justify-content: flex-end; align-items: center; gap: 8px; }
        .aiWindowActions .hint { flex: 1; }
        .barInner.indeterminate { animation: aiPulse 1.4s ease-in-out infinite; }
        @keyframes aiPulse { 0% { margin-left: 0; } 50% { margin-left: 70%; } 100% { margin-left: 0; } }
        .changesView { max-width: 90ch; margin: 0 auto; display: flex; flex-direction: column; gap: 10px; }
        .changesSummary { font-size: 13px; display: flex; flex-direction: column; gap: 4px; padding: 10px 12px; border: 1px solid var(--border); border-radius: 10px; }
        .changesText { white-space: pre-wrap; font: 13px/1.6 ui-monospace, Consolas, monospace; margin: 0; }
        .changesView del, .changesSummary del { background: rgba(239, 68, 68, 0.22); text-decoration: line-through; border-radius: 3px; }
        .changesView ins, .changesSummary ins { background: rgba(52, 211, 153, 0.25); text-decoration: none; border-radius: 3px; }
        .invisibleChar { font-size: 10px; padding: 0 3px; margin: 0 1px; border: 1px dashed var(--muted); border-radius: 4px; color: var(--muted); }
        .decision { max-width: 78ch; margin: 0 auto 14px; padding: 12px 14px; border: 1px solid var(--accent); border-radius: 12px; font-size: 13px; display: flex; flex-direction: column; gap: 6px; }
        .decisionActions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 4px; }
        .editor { max-width: 90ch; margin: 0 auto; display: flex; flex-direction: column; gap: 8px; height: 100%; }
        .editArea { flex: 1; min-height: 50vh; width: 100%; box-sizing: border-box; resize: vertical; padding: 12px; border-radius: 10px; border: 1px solid var(--border); background: rgba(127,127,127,0.06); color: var(--fg); font: 13px/1.5 ui-monospace, Consolas, monospace; }
        .aiNotice { color: var(--muted); font-size: 12px; margin: 0 auto 12px; max-width: 78ch; padding: 8px 10px; border: 1px solid var(--border); border-radius: 10px; }
        .rawText { white-space: pre-wrap; font-size: 13px; line-height: 1.5; max-width: 78ch; margin: 0 auto; font-family: ui-monospace, Consolas, monospace; }
        /* settings */
        .settings { width: min(720px, 96vw); max-height: 92vh; display: flex; flex-direction: column; }
        .settingsBody { overflow: auto; display: flex; flex-direction: column; gap: 12px; }
        .settingsGroup { border: 1px solid var(--border); border-radius: 12px; padding: 10px 12px 12px; margin: 0; display: flex; flex-direction: column; gap: 10px; min-width: 0; }
        .settingsGroup:disabled { opacity: 0.5; }
        .settingsGroup legend { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; padding: 0 6px; }
        .settingsGroup .row { flex-wrap: nowrap; }
        .field { display: flex; flex-direction: column; gap: 5px; font-size: 13px; }
        .field.narrow .input { max-width: 120px; }
        .radio { display: flex; align-items: center; gap: 8px; font-size: 13px; }
        .hint { color: var(--muted); font-size: 12px; line-height: 1.4; }
        .aiState { font-size: 13px; padding: 10px 12px; border-radius: 12px; border: 1px solid var(--border); }
        .aiState.state-running, .aiState.state-external { border-color: var(--ok); }
        .aiState.state-error, .aiState.state-not_installed { border-color: var(--danger); }
        .settingsPath { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; align-self: center; }
        .pullProgress { margin-top: 4px; display: flex; flex-direction: column; gap: 4px; }
        select.input { flex: initial; }
        .linkBtn { border: 0; background: none; color: var(--accent); cursor: pointer; padding: 0; font: inherit; text-decoration: underline; }

        .md blockquote { margin: 0 0 0.85em; padding: 2px 0 2px 14px; border-left: 3px solid var(--border); color: var(--muted); }

        .md { max-width: 78ch; margin: 0 auto; font-size: 15px; line-height: 1.6; }
        .md h2, .md h3, .md h4, .md h5, .md h6 { line-height: 1.25; margin: 1.3em 0 0.5em; }
        .md h2 { font-size: 24px; } .md h3 { font-size: 19px; } .md h4 { font-size: 16px; } .md h5, .md h6 { font-size: 15px; }
        .md > :first-child { margin-top: 0; }
        .md p { margin: 0 0 0.85em; }
        .md ul, .md ol { margin: 0 0 0.85em; padding-left: 1.4em; }
        .md li { margin: 0.2em 0; }
        .md code { font-size: 0.92em; padding: 1px 4px; border-radius: 4px; background: rgba(127,127,127,0.15); }
        .md hr { border: 0; border-top: 1px solid var(--border); margin: 1.2em 0; }
        .mdTableWrap { overflow-x: auto; margin: 0 0 1em; }
        .md table { border-collapse: collapse; font-size: 14px; }
        .md th, .md td { border: 1px solid var(--border); padding: 5px 9px; text-align: left; vertical-align: top; }
        .md th { background: rgba(127,127,127,0.10); }
        .mdEmpty { color: var(--muted); font-style: italic; text-align: center; margin-top: 40px; }

        /* The desktop window is at least 900px wide, so the two columns always fit there;
           only much narrower (browser) windows stack the panels. */
        @media (max-width: 720px) {
          .content { grid-template-columns: 1fr; }
          .readerBody { padding: 14px 16px 20px; }
        }
      `}</style>

      <div className="topbar">
        <div className="brand">
          <img className="brandLogo" src={appLogo} alt="" aria-hidden="true" />
          <div className="title">
            <h1>Codex Engine</h1>
            <div className="sub">
              Rulebook library (local) • Active sources: {enabledCount}/{selectedCount}
            </div>
          </div>
        </div>

        {/* Updates, theme and diagnostics live in Settings to keep this bar to the essentials. */}
        <div className="actions">
          <button className="btn primary" onClick={onPickAndIngest}>
            Ingest PDF
          </button>
          <button className="btn" onClick={() => setSettingsOpen(true)}>
            Settings
          </button>
        </div>
      </div>

      <div className="status">{status}</div>

      <div className="content">
        <div className="panel">
          <div className="panelHeader">
            <h2>Sources</h2>
            <div className="row">
              <button className="btn small" onClick={refreshSources} title="Reload the list of sources">
                Refresh
              </button>
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
                <div className="sourceCard" key={s.id} title={`${s.path}\nSHA-256: ${s.sha256}`}>
                  <div className="sourceTitle">{s.title}</div>
                  <div className="sourceMeta">
                    <span>{s.pages ?? 0} pages</span>
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
                  onClick={() => openReader(r)}
                  title={`Click to read this page\n${r.source_path}`}
                >
                  <div className="resultTop">
                    <div className="resultHeading">
                      {(r.heading && r.heading.trim()) || "(No heading)"}
                    </div>
                    <div className="resultSrc">
                      {r.source_title} • p. {r.page_num}
                    </div>
                  </div>
                  <div className="resultSnippet">{markedSnippet(r.snippet)}</div>
                </div>
              ))
            )}
          </div>
        </div>
      </div>

      {/* Page reader */}
      {reader && (
        <div className="overlay" onMouseDown={closeReader}>
          <div className="modal reader" onMouseDown={(e) => e.stopPropagation()}>
            <div className="modalHeader">
              <div className="readerTitle">
                <h3 title={readerPage?.path}>{readerPage?.title ?? "Loading…"}</h3>
                <div className="readerSub">
                  Page {reader.page}
                  {readerPage ? ` of ${readerPage.page_count}` : ""}
                  {readerPage ? ` • ${VIEW_LABELS[readerView]}` : ""}
                  {readerView === "ai" && readerPage?.ai_model ? ` (${readerPage.ai_model})` : ""}
                  {readerView === "ai" && readerPage?.ai_stale ? " • outdated, reformat to refresh" : ""}
                  {readerPage && searchedFor
                    ? ` • ${matchCount ? `${matchCount} match${matchCount === 1 ? "" : "es"}` : "no matches"} for “${searchedFor.replace(/"/g, "")}”`
                    : ""}
                </div>
              </div>
              <button className="btn small" onClick={closeReader}>
                Close
              </button>
            </div>

            <div className="readerBar">
              <button className="btn small" onClick={() => turnReaderPage(-1)} disabled={readerLoading || reader.page <= 1}>
                ← Prev
              </button>
              <button
                className="btn small"
                onClick={() => turnReaderPage(1)}
                disabled={readerLoading || !readerPage || reader.page >= readerPage.page_count}
              >
                Next →
              </button>

              <div className="seg" role="group" aria-label="Text version">
                {readerPage?.edited_md != null && (
                  <button
                    className={readerView === "edited" ? "on" : ""}
                    onClick={() => setReaderView("edited")}
                    title={`Your own edit of this page${readerPage.edited_at ? ` (saved ${readerPage.edited_at} UTC)` : ""}`}
                  >
                    Edited
                  </button>
                )}
                <button
                  className={readerView === "ai" ? "on" : ""}
                  onClick={() => setReaderView("ai")}
                  disabled={!readerPage?.ai_md}
                  title={readerPage?.ai_md ? "AI-formatted text" : "Not AI formatted yet"}
                >
                  AI{readerPage?.ai_stale ? "*" : ""}
                </button>
                <button
                  className={readerView === "clean" ? "on" : ""}
                  onClick={() => setReaderView("clean")}
                  title="Rule-based cleanup of the extracted text"
                >
                  Cleaned
                </button>
                <button
                  className={readerView === "raw" ? "on" : ""}
                  onClick={() => setReaderView("raw")}
                  disabled={!readerPage?.raw_text}
                  title={readerPage?.raw_text ? "Text exactly as extracted from the PDF" : "Re-open the book to capture raw text"}
                >
                  Raw
                </button>
                <button
                  className={readerView === "changes" ? "on" : ""}
                  onClick={() => setReaderView("changes")}
                  disabled={!readerPage?.ai_md}
                  title={readerPage?.ai_md ? "See exactly what the AI changed compared with the cleaned text" : "Not AI formatted yet"}
                >
                  Changes
                </button>
              </div>

              <button
                className="btn small"
                onClick={() => aiFormatReaderPage()}
                disabled={aiBusy || readerLoading || !readerPage || !aiStatus?.available}
                title={
                  aiBusy
                    ? "One page at a time. You can keep reading while it runs."
                    : aiStatus?.available
                    ? `Format this page with ${aiStatus.model} (runs locally in Ollama; never rewrites the text)`
                    : aiStatus?.error ?? "Checking for Ollama…"
                }
              >
                {aiBusy && aiRun!.sourceId === reader.sourceId && aiRun!.page === reader.page
                  ? "Formatting…"
                  : aiBusy
                  ? `Formatting p. ${aiRun!.page}…`
                  : readerPage?.ai_md && !readerPage.ai_stale
                  ? "Regenerate AI format"
                  : "AI format page"}
              </button>
              {aiStatus && !aiStatus.available && (
                <span className="readerNote" title={aiStatus.error ?? ""}>
                  {aiStatus.state === "starting"
                    ? "AI starting…"
                    : aiStatus.state === "off"
                    ? "AI formatting is off"
                    : "AI formatting unavailable"}{" "}
                  <button className="linkBtn" onClick={() => setSettingsOpen(true)}>
                    Settings
                  </button>
                </span>
              )}

              <div className="spacer" />
              {readerView === "edited" && !editing && (
                <button className="btn small" onClick={revertEdit} title="Go back to the automatic (AI or cleaned) version">
                  Revert edit
                </button>
              )}
              <button
                className="btn small"
                onClick={() => startEdit()}
                disabled={!readerPage || Boolean(editing)}
                title="Correct this page's text yourself. The PDF and the automatic versions are kept."
              >
                Edit page
              </button>
              <button
                className="btn small"
                onClick={() => readerPage && openPdf(readerPage.path, reader.page)}
                disabled={!readerPage}
              >
                Open PDF
              </button>
            </div>

            <div className="readerBody" ref={readerBodyRef}>
              {readerError && <div className="readerError">{readerError}</div>}
              {aiNotice && <div className="aiNotice">{aiNotice}</div>}
              {aiDecision && !aiBusy && !editing && aiDecision.sourceId === reader.sourceId && aiDecision.page === reader.page && (
                <div className="decision" role="alert">
                  <b>
                    The AI couldn't format {aiDecision.pending.failed.length} of {aiDecision.pending.sections} part
                    {aiDecision.pending.sections === 1 ? "" : "s"} of this page without changing the text
                    {aiDecision.pending.failed[0]?.attempts ? `, even after ${aiDecision.pending.failed[0].attempts} tries` : ""}.
                  </b>
                  <div className="hint">
                    Last problem: {aiDecision.pending.failed[aiDecision.pending.failed.length - 1]?.reason}. Nothing has been
                    changed yet. What would you like to do?
                  </div>
                  <div className="decisionActions">
                    <button className="btn small" onClick={() => aiFormatReaderPage("retry")}>
                      Keep trying
                    </button>
                    <button className="btn small" onClick={() => aiFormatReaderPage("clean")}>
                      {aiDecision.pending.formatted ? "Use cleaned text for those parts" : "Keep the cleaned text"}
                    </button>
                    <button className="btn small" onClick={() => startEdit(aiDecision.pending.draft_md)}>
                      Edit the page myself
                    </button>
                  </div>
                </div>
              )}
              {editing ? (
                <div className="editor">
                  <div className="hint">
                    Correct this page however you like. Markdown works: # Heading, **bold**, - list item, | table |. Your
                    edit is shown instead of the automatic versions and can be reverted anytime; the PDF is never changed.
                  </div>
                  <textarea
                    className="editArea"
                    value={editing.text}
                    onChange={(e) => setEditing({ ...editing, text: e.target.value })}
                    spellCheck={false}
                    autoFocus
                  />
                  <div className="modalActions">
                    <button className="btn" onClick={() => confirmDiscardEdit() && setEditing(null)} disabled={savingEdit}>
                      Cancel
                    </button>
                    <button className="btn primary" onClick={saveEdit} disabled={savingEdit}>
                      {savingEdit ? "Saving…" : "Save edit"}
                    </button>
                  </div>
                </div>
              ) : readerLoading && !readerPage ? (
                <div className="readerNote">Loading page… (the first time a book opens it is prepared for reading, which can take a few seconds)</div>
              ) : readerPage ? (
                readerView === "raw" ? (
                  <pre className="rawText">{highlightText(readerPage.raw_text ?? "", highlight)}</pre>
                ) : readerView === "changes" && readerPage.ai_md ? (
                  <ChangesView before={readerPage.clean_md} after={readerPage.ai_md} />
                ) : (
                  <Markdown
                    text={
                      readerView === "edited" && readerPage.edited_md != null
                        ? readerPage.edited_md
                        : readerView === "ai" && readerPage.ai_md
                        ? readerPage.ai_md
                        : readerPage.clean_md
                    }
                    highlight={highlight}
                  />
                )
              ) : null}
            </div>
          </div>
        </div>
      )}

      {aiRun && (
        <AIProgressWindow
          run={aiRun}
          readerOnPage={Boolean(reader && reader.sourceId === aiRun.sourceId && reader.page === aiRun.page)}
          onCancel={cancelAiFormat}
          onClose={() => setAiRun(null)}
          onShowPage={() => {
            setReaderPage(null);
            loadReaderPage(aiRun.sourceId, aiRun.page);
          }}
        />
      )}

      {settingsOpen && (
        <SettingsPanel
          status={aiStatus}
          pull={modelPull}
          onClose={() => setSettingsOpen(false)}
          onStatus={setAiStatus}
          general={{
            dark,
            onDarkChange: setDark,
            logToConsole,
            onLogToConsoleChange: setLogToConsole,
            logToFile,
            onLogToFileChange: setLogToFile,
            onCheckUpdates: checkForUpdates,
            checkingUpdates,
            updateStatus: status,
            versionText: `UI ${FRONTEND_VERSION} • API ${versionInfo?.backend_version ?? "..."} • Updater ${versionInfo?.updater_version ?? "..."}${
              versionInfo ? ` • ${versionInfo.platform}${versionInfo.updater_present ? "" : " • updater missing"}` : ""
            }`,
          }}
        />
      )}

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
                    const file = await pickPdfFile();
                    if (file) await startIngestWithFile(file);
                  }}
                >
                  Upload PDF
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

                  {ingestStarted && (
                    <div className="hint" style={{ marginTop: 6 }}>
                      Elapsed {Math.floor(((progress?.done ? ingestLog[ingestLog.length - 1]?.at ?? ingestNow : ingestNow) - ingestStarted) / 60000)}:
                      {String(Math.floor((((progress?.done ? ingestLog[ingestLog.length - 1]?.at ?? ingestNow : ingestNow) - ingestStarted) / 1000) % 60)).padStart(2, "0")}
                    </div>
                  )}
                  {ingestLog.length > 0 && (
                    <ol className="aiWindowLog" style={{ marginTop: 8 }}>
                      {ingestLog.map((entry, i) => (
                        <li key={i}>
                          <span className="hint">
                            {ingestStarted ? `${Math.round((entry.at - ingestStarted) / 1000)}s` : ""}
                          </span>{" "}
                          {entry.message}
                        </li>
                      ))}
                    </ol>
                  )}
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
              <button
                className="btn small"
                title="Closing keeps the existing source and discards the new ingest"
                onClick={() => resolveDuplicate("discard")}
              >
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







