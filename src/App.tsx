import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import {
  api,
  errorText,
  type AIFormatLogEvent,
  type AIFormatProgressEvent,
  type AIStatus,
  type BookAICheck,
  type DuplicateDetectedPayload,
  type IngestProgress,
  type AIPending,
  type ModelPull,
  type ReaderView,
  type PageView,
  type SearchRow,
  type SourceRow,
  type UpdateCheckResult,
  type VersionInfo,
} from "./api";
import { highlightText, markedSnippet, queryPattern } from "./highlight";
import AIProgressWindow, { newAIRun, type AIRun } from "./AIProgressWindow";
import ChangesView from "./ChangesView";
import Dialog from "./Dialog";
import { formatElapsed, percent, plural } from "./format";
import Markdown from "./Markdown";
import SettingsPanel from "./SettingsPanel";
// A 128 px copy of the app logo: the header shows it at 44 px.
import appLogo from "./assets/codex-engine-logo-128.png";

const FRONTEND_VERSION = import.meta.env.PACKAGE_VERSION;

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

// Plain-language result of an update check (or of an apply that didn't start the updater).
function describeUpdate(update: UpdateCheckResult): string {
  switch (update.status) {
    case "current":
      return `Codex Engine is up to date (${update.current_version}).`;
    case "no_release":
      return "No GitHub release is published yet. Create a release and attach the CodexEngineSetup installer to enable updates.";
    case "missing_installer":
      return `Update ${update.latest_version} has no installer for this computer yet${
        update.platform ? ` (${update.platform}` + (update.expected_asset ? `, expected ${update.expected_asset})` : ")") : ""
      }.`;
    case "update_available":
      return `Update ${update.latest_version} is available.`;
    case "updater_launched":
      return "Updater launched. Codex Engine will close to finish updating.";
    default:
      return update.message || `Unexpected update status "${String((update as { status?: unknown }).status)}".`;
  }
}

const SEARCH_LIMIT = 50; // the backend returns at most this many results

// The text the reader shows (and "Edit page" starts from) for a text version. "changes" and a
// version the page doesn't have fall back to the cleaned text.
function shownText(page: PageView, view: ReaderView): string {
  if (view === "edited" && page.edited_md != null) return page.edited_md;
  if (view === "ai" && page.ai_md) return page.ai_md;
  if (view === "raw" && page.raw_text) return page.raw_text;
  return page.clean_md;
}

type BookRun = {
  total: number;
  done: number;
  current: number | null; // page being reformatted; null once the run is over
  ok: number[];
  needsYou: number[];
  failed: { page: number; message: string }[];
  stopped: boolean;
};

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
  // Update check/apply results get their own line in Settings > Updates.
  const [updateStatus, setUpdateStatus] = useState("");
  const updateBusy = useRef(false); // a check or apply is in flight (state alone can't stop a fast double click)
  const [versionInfo, setVersionInfo] = useState<VersionInfo | null>(null);

  // Import window state
  const [ingestOpen, setIngestOpen] = useState(false);
  const [ingestPath, setIngestPath] = useState("");
  const [ingestId, setIngestIdState] = useState<number | null>(null);
  // Read by the long-lived event stream; kept in step with the state by setIngestId.
  const ingestIdRef = useRef<number | null>(null);
  function setIngestId(id: number | null) {
    ingestIdRef.current = id;
    setIngestIdState(id);
  }
  const [progress, setProgress] = useState<IngestProgress | null>(null);
  // Plain-language history of the current import (one line per step) and when it started.
  const [ingestLog, setIngestLog] = useState<{ at: number; message: string }[]>([]);
  const [ingestStarted, setIngestStarted] = useState<number | null>(null);
  const [ingestNow, setIngestNow] = useState(() => Date.now());

  // The duplicate waiting for the user's choice (its window is open while this is set).
  const [dup, setDup] = useState<DuplicateDetectedPayload | null>(null);

  // Page reader state
  const [reader, setReader] = useState<{ sourceId: number; page: number } | null>(null);
  const [readerPage, setReaderPage] = useState<PageView | null>(null);
  const [readerView, setReaderView] = useState<ReaderView>("clean");
  const [readerError, setReaderError] = useState("");
  const [aiNotice, setAiNotice] = useState(""); // AI unavailable/rejected: informational, not an app error
  const [readerLoading, setReaderLoading] = useState(false);
  // The current (or last) AI formatting run, shown in the progress window (null: none to show).
  const [aiRun, setAiRun] = useState<AIRun | null>(null);
  const aiBusy = Boolean(aiRun && !aiRun.finished);
  // Sections the AI still couldn't format after its automatic retries: the user decides what next.
  const [aiDecision, setAiDecision] = useState<{ sourceId: number; page: number; pending: AIPending } | null>(null);
  // "Check AI pages" for one book, and the "Reformat all" run over its outdated pages.
  const [bookCheck, setBookCheck] = useState<{ sourceId: number; title: string; report: BookAICheck | null; error: string } | null>(null);
  const [bookRun, setBookRun] = useState<BookRun | null>(null);
  const bookStopRef = useRef(false); // set to stop "Reformat all" after the current page
  const bookRunning = Boolean(bookRun && bookRun.current !== null);
  // Manual edit in progress (the page text being edited), and whether it differs from where it started.
  const [editing, setEditing] = useState<{ text: string; original: string } | null>(null);
  // reformatBook runs across many renders; it reads which page the reader shows, and whether
  // an edit is open there, through these two refs (updated after every render).
  const readerRef = useRef(reader);
  const editingRef = useRef(editing);
  useEffect(() => {
    readerRef.current = reader;
    editingRef.current = editing;
  });
  const [savingEdit, setSavingEdit] = useState(false);
  // The search the current results came from: its terms are highlighted in the reader.
  const [searchedFor, setSearchedFor] = useState("");
  const highlight = useMemo(() => queryPattern(searchedFor), [searchedFor]);
  const readerBodyRef = useRef<HTMLDivElement>(null);
  const [matchCount, setMatchCount] = useState(0);
  const [aiStatus, setAiStatus] = useState<AIStatus | null>(null);
  const readerRequest = useRef(0); // ignore responses for pages we've navigated away from
  const searchRequest = useRef(0); // ignore responses for searches that were superseded (or cleared)

  const [settingsOpen, setSettingsOpen] = useState(false);
  const [modelPull, setModelPull] = useState<ModelPull | null>(null);

  // Logging controls for frontend diagnostics.
  const [logToConsole, setLogToConsole] = useState(false);
  const [logToFile, setLogToFile] = useState(false);
  // Refs so long-lived callbacks (the event stream) always see the current toggles
  // without tearing down and reconnecting every time a checkbox changes.
  const logToConsoleRef = useRef(logToConsole);
  const logToFileRef = useRef(logToFile);
  useEffect(() => {
    logToConsoleRef.current = logToConsole;
    logToFileRef.current = logToFile;
  }, [logToConsole, logToFile]);

  // Stable (reads the toggles through refs), so the event stream below connects once.
  const uiLog = useCallback((line: string) => {
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
  }, []);

  const refreshSources = useCallback(async () => {
    try {
      uiLog("refresh_sources start");
      const rows = await api.listSources();
      setSources(rows);
      uiLog(`refresh_sources ok count=${rows.length}`);
    } catch (e: unknown) {
      uiLog(`refresh_sources failed ${errorText(e)}`);
      setStatus(`Couldn't load the sources: ${errorText(e)}`);
    }
  }, [uiLog]);

  const enabledCount = useMemo(() => sources.filter((s) => s.enabled).length, [sources]);

  const refreshAiStatus = useCallback(() => {
    api.aiStatus().then(setAiStatus).catch(() => setAiStatus(null));
  }, []);

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
      .then(setVersionInfo)
      .catch((e: unknown) => setStatus(`Version check failed: ${errorText(e)}`));

    const events = new EventSource(api.eventsUrl);

    events.addEventListener("ingest_progress", (event) => {
      const p = JSON.parse((event as MessageEvent).data) as IngestProgress;
      // Once the current ingest's id is known, other ingests' events don't belong in its window.
      const current = ingestIdRef.current;
      if (current !== null && typeof p.id === "number" && p.id !== current) {
        uiLog(`ingest_progress ignored id=${p.id} (showing ${current})`);
        if (p.done) refreshSources(); // it may still have added a source
        return;
      }
      setProgress(p);
      setIngestLog((log) => {
        // One line per step: per-page updates within a step replace the step's line.
        // A cancelled import ends with stage "cancelled", done and no error; its message says so.
        const message = p.error
          ? `Failed: ${p.error}`
          : p.stage === "cancelled"
          ? p.message
          : p.done
          ? `Finished: ${p.message}`
          : p.message;
        const last = log[log.length - 1];
        const sameStep = last && !p.done && !p.error && last.message.split(/\d/)[0] === message.split(/\d/)[0];
        const entry = { at: Date.now(), message };
        return sameStep ? [...log.slice(0, -1), entry] : [...log, entry].slice(-50);
      });
      uiLog(`ingest_progress id=${p.id} stage=${p.stage} ${p.current}/${p.total} done=${p.done} msg=${p.message}`);
      if (current === null && typeof p.id === "number") setIngestId(p.id);
      if (p.done) refreshSources();
    });

    events.addEventListener("duplicate_detected", (event) => {
      const payload = JSON.parse((event as MessageEvent).data) as DuplicateDetectedPayload;
      uiLog(`duplicate_detected for ingest_id=${payload.ingest_id} existing_id=${payload.existing_id}`);
      setDup(payload);
      setIngestOpen(true);
      setStatus("This PDF is already in the library. Choose what to do.");
    });

    events.addEventListener("model_pull", (event) => {
      const p = JSON.parse((event as MessageEvent).data) as ModelPull;
      setModelPull(p);
      if (p.done) uiLog(`model_pull ${p.model} ${p.error ? `failed: ${p.error}` : "done"}`);
    });

    events.addEventListener("ai_format_progress", (event) => {
      const p = JSON.parse((event as MessageEvent).data) as AIFormatProgressEvent;
      setAiRun((run) =>
        run && !run.finished && run.sourceId === p.source_id && run.page === p.page_num
          ? { ...run, done: p.done, total: p.total, attempt: p.attempt, maxAttempts: p.max_attempts, written: p.written, expected: p.expected }
          : run
      );
    });

    events.addEventListener("ai_format_log", (event) => {
      const p = JSON.parse((event as MessageEvent).data) as AIFormatLogEvent;
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
  }, [refreshSources, uiLog]); // both stable: connects once

  async function toggleSourceEnabled(sourceId: number, enabled: boolean) {
    try {
      uiLog(`set_source_enabled id=${sourceId} enabled=${enabled}`);
      await api.setSourceEnabled(sourceId, enabled);
      // A book that's off isn't searched: its results leave the current list too.
      if (!enabled) setResults((r) => r.filter((x) => x.source_id !== sourceId));
      await refreshSources();
    } catch (e: unknown) {
      uiLog(`set_source_enabled failed id=${sourceId} ${errorText(e)}`);
      setStatus(`Couldn't update the source: ${errorText(e)}`);
    }
  }

  async function deleteSource(sourceId: number, title: string) {
    if (!confirm(`Delete "${title}" from the library, with its pages, AI versions and your edits?`)) return;
    try {
      uiLog(`delete_source id=${sourceId}`);
      await api.deleteSource(sourceId);
      await refreshSources();
      setResults((r) => r.filter((x) => x.source_id !== sourceId));
    } catch (e: unknown) {
      uiLog(`delete_source failed id=${sourceId} ${errorText(e)}`);
      setStatus(`Couldn't delete the source: ${errorText(e)}`);
    }
  }

  async function runSearch() {
    const q = query.trim();
    if (!q) return;
    const requestId = ++searchRequest.current;

    try {
      uiLog(`search start query="${q}"`);
      setStatus("Searching…");
      const rows = await api.search(q);
      if (requestId !== searchRequest.current) return;
      setResults(rows);
      setSearchedFor(q);
      setStatus(
        rows.length >= SEARCH_LIMIT
          ? `Showing the first ${SEARCH_LIMIT} results (best matches first). Add more words to narrow the search.`
          : rows.length
          ? `Found ${plural(rows.length, "result")}.`
          : "No results."
      );
      uiLog(`search ok query="${q}" results=${rows.length}`);
    } catch (e: unknown) {
      if (requestId !== searchRequest.current) return;
      uiLog(`search failed query="${q}" ${errorText(e)}`);
      setStatus(`Search failed: ${errorText(e)}`);
    }
  }

  async function checkForUpdates() {
    if (updateBusy.current) return;
    updateBusy.current = true;
    let launched = false;
    let applying = false;
    try {
      uiLog("update_check start");
      setCheckingUpdates(true);
      setUpdateStatus("Checking GitHub for updates…");
      const update = await api.checkForUpdate();
      uiLog(`update_check result status=${update.status} current=${update.current_version} latest=${update.latest_version}`);

      if (update.status !== "update_available") {
        setUpdateStatus(describeUpdate(update));
        return;
      }

      const shouldUpdate = confirm(
        `Codex Engine ${update.latest_version} is available. Download and install it now? The app will close while updating.`
      );
      if (!shouldUpdate) {
        setUpdateStatus(describeUpdate(update));
        return;
      }

      applying = true;
      setUpdateStatus(`Downloading Codex Engine ${update.latest_version}…`);
      // The backend re-checks GitHub; it reports "updater_launched" only when the updater really started.
      const result = await api.applyUpdate();
      uiLog(`update_apply result status=${result.status} latest=${result.latest_version}`);
      if (result.status !== "updater_launched") {
        setUpdateStatus(`The update didn't start. ${describeUpdate(result)}`);
        return;
      }
      launched = true;
      setUpdateStatus(describeUpdate(result));
      setTimeout(() => window.close(), 750);
    } catch (e: unknown) {
      uiLog(`${applying ? "update_apply" : "update_check"} failed ${errorText(e)}`);
      setUpdateStatus(`${applying ? "Update failed" : "Update check failed"}: ${errorText(e)}`);
    } finally {
      // Once the updater is running the app is closing: keep the button disabled.
      if (!launched) {
        updateBusy.current = false;
        setCheckingUpdates(false);
      }
    }
  }

  async function openPdf(path: string, page: number) {
    try {
      uiLog(`open_pdf path="${path}" page=${page}`);
      await api.openPdfAtLocation(path, page);
    } catch (e: unknown) {
      uiLog(`open_pdf failed path="${path}" page=${page} ${errorText(e)}`);
      setReaderError(`Couldn't open the PDF: ${errorText(e)}`);
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
    } catch (e: unknown) {
      if (requestId !== readerRequest.current) return;
      uiLog(`reader_load failed source=${sourceId} page=${page} ${errorText(e)}`);
      setReaderPage(null);
      setReaderError(errorText(e));
    } finally {
      if (requestId === readerRequest.current) setReaderLoading(false);
    }
  }

  function openReader(r: SearchRow) {
    setReaderPage(null);
    loadReaderPage(r.source_id, r.page_num);
    refreshAiStatus();
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
  // Shows a new run in the progress window; the returned function records how it ended (if
  // the window still shows that run).
  function startAiRun(sourceId: number, page: number, title: string) {
    setAiRun(newAIRun(sourceId, page, title));
    return (kind: NonNullable<AIRun["finished"]>["kind"], message: string) =>
      setAiRun((run) => (run && run.sourceId === sourceId && run.page === page ? { ...run, finished: { kind, message } } : run));
  }

  async function aiFormatReaderPage(decision?: "retry" | "clean") {
    if (!reader) return;
    const { sourceId, page } = reader;
    const requestId = readerRequest.current;
    // The button on a page that already has current AI output means "regenerate": skip the cache.
    // A follow-up decision must not: the sections that already passed are reused from the cache.
    const force = !decision && Boolean(readerPage?.ai_md && !readerPage.ai_stale);
    const finish = startAiRun(sourceId, page, readerPage?.title ?? "");
    setAiDecision(null);
    setAiNotice("");
    try {
      uiLog(`ai_format start source=${sourceId} page=${page} force=${force} decision=${decision ?? ""}`);
      const data = await api.aiFormatPage(sourceId, page, { model: aiStatus?.model, force, useCleanForFailed: decision === "clean" });
      uiLog(`ai_format done source=${sourceId} page=${page} model=${data.ai_model} pending=${data.pending?.failed.length ?? 0}`);
      if (data.pending) {
        setAiDecision({ sourceId, page, pending: data.pending });
        finish(
          "decision",
          `Couldn't format ${data.pending.failed.length} of ${plural(data.pending.sections, "part")} without changing the text. Your choice is waiting on the page.`
        );
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
    } catch (e: unknown) {
      // 409 = cancelled by the user; 503 = Ollama not running / no model. The reader keeps working.
      const message = errorText(e);
      uiLog(`ai_format failed source=${sourceId} page=${page} ${message}`);
      finish(/^Cancelled/.test(message) ? "cancelled" : "error", message);
      if (!/^Cancelled/.test(message)) setAiNotice(message);
      refreshAiStatus();
    }
  }

  async function checkBookAI(sourceId: number, title: string) {
    setBookCheck({ sourceId, title, report: null, error: "" });
    setBookRun(null);
    refreshAiStatus();
    try {
      uiLog(`book_ai_check source=${sourceId}`);
      const report = await api.checkBookAI(sourceId);
      setBookCheck((c) => (c && c.sourceId === sourceId ? { ...c, report } : c));
    } catch (e: unknown) {
      setBookCheck((c) => (c && c.sourceId === sourceId ? { ...c, error: errorText(e) } : c));
    }
  }

  // Reformats the book's outdated pages one at a time, through the same endpoint as the reader's
  // button, so each page shows in the progress window. A page whose parts still fail after the
  // automatic retries is left for the user to decide on in the reader, never decided for them.
  async function reformatBook() {
    if (!bookCheck?.report || aiBusy) return;
    const { sourceId, title } = bookCheck;
    const pages = bookCheck.report.outdated.map((p) => p.page_num);
    bookStopRef.current = false;
    setBookRun({ total: pages.length, done: 0, current: null, ok: [], needsYou: [], failed: [], stopped: false });
    for (const page of pages) {
      if (bookStopRef.current) break;
      setBookRun((r) => r && { ...r, current: page });
      const finish = startAiRun(sourceId, page, title);
      let outcome: "ok" | "needsYou" | "failed" = "failed";
      let message = "";
      try {
        uiLog(`book_reformat source=${sourceId} page=${page}`);
        const data = await api.aiFormatPage(sourceId, page, { model: aiStatus?.model });
        if (data.pending) {
          outcome = "needsYou";
          finish("decision", `Couldn't format part of page ${page} without changing the text. Open the page to decide.`);
        } else if (data.ai_error) {
          message = data.ai_error;
          finish("error", message);
        } else {
          outcome = "ok";
          finish("ok", `Page ${page} reformatted and checked.`);
        }
      } catch (e: unknown) {
        message = errorText(e);
        finish(/^Cancelled/.test(message) ? "cancelled" : "error", message);
        if (/^Cancelled/.test(message) || /unavailable/i.test(message)) bookStopRef.current = true; // stop, or no AI to continue with
      }
      setBookRun(
        (r) =>
          r && {
            ...r,
            done: r.done + 1,
            ok: outcome === "ok" ? [...r.ok, page] : r.ok,
            needsYou: outcome === "needsYou" ? [...r.needsYou, page] : r.needsYou,
            failed: outcome === "failed" ? [...r.failed, { page, message }] : r.failed,
          }
      );
      const open = readerRef.current;
      if (open && open.sourceId === sourceId && open.page === page && !editingRef.current) loadReaderPage(sourceId, page);
    }
    setBookRun((r) => r && { ...r, current: null, stopped: bookStopRef.current });
    try {
      const report = await api.checkBookAI(sourceId);
      setBookCheck((c) => (c && c.sourceId === sourceId ? { ...c, report } : c));
    } catch {
      // The run's own summary is still shown.
    }
  }

  function stopBookReformat() {
    bookStopRef.current = true;
    cancelAiFormat();
  }

  // Also the progress window's "Show page": same guards (no jumping mid "Reformat all", no lost edits).
  function openBookPage(sourceId: number, page: number) {
    if (bookRunning || !confirmDiscardEdit()) return;
    setBookCheck(null); // the reader opens underneath this window
    setReaderPage(null);
    loadReaderPage(sourceId, page);
    refreshAiStatus();
  }

  async function cancelAiFormat() {
    if (!aiRun || aiRun.finished) return;
    try {
      await api.cancelAiFormat(aiRun.sourceId, aiRun.page);
    } catch (e: unknown) {
      uiLog(`ai_format cancel failed ${errorText(e)}`);
    }
  }

  // A successful run's window closes itself after a few seconds; anything else stays until closed.
  const aiRunEnd = aiRun?.finished?.kind;
  useEffect(() => {
    if (aiRunEnd !== "ok") return;
    const timer = window.setTimeout(() => setAiRun((run) => (run?.finished?.kind === "ok" ? null : run)), 6000);
    return () => window.clearTimeout(timer);
  }, [aiRunEnd]);

  // The page shown is the page the reader points at (not a previous page still on screen while loading).
  function readerShowsItsPage(): boolean {
    return Boolean(
      reader && readerPage && !readerLoading && readerPage.source_id === reader.sourceId && readerPage.page_num === reader.page
    );
  }

  async function saveEdit() {
    if (!reader || !editing || savingEdit || !readerShowsItsPage()) return;
    // Captured now: if the reader moves on while saving, the response belongs to this page, not that one.
    const { sourceId, page } = reader;
    const requestId = readerRequest.current;
    setSavingEdit(true);
    try {
      const data = await api.saveEdit(sourceId, page, editing.text);
      uiLog(`page_edit saved source=${sourceId} page=${page}`);
      if (requestId !== readerRequest.current) return; // saved on its own page; the reader shows another
      setReaderPage(data);
      setReaderView("edited");
      setEditing(null);
      setAiDecision(null);
    } catch (e: unknown) {
      if (requestId !== readerRequest.current) return;
      setReaderError(`Couldn't save your edit: ${errorText(e)}`);
    } finally {
      setSavingEdit(false);
    }
  }

  async function revertEdit() {
    if (!reader || !readerShowsItsPage()) return;
    const { sourceId, page } = reader;
    const requestId = readerRequest.current;
    if (!confirm("Remove your edit and go back to the automatic version of this page?")) return;
    if (requestId !== readerRequest.current) return;
    try {
      const data = await api.revertEdit(sourceId, page);
      uiLog(`page_edit reverted source=${sourceId} page=${page}`);
      if (requestId !== readerRequest.current) return;
      setReaderPage(data);
      setReaderView(data.best);
    } catch (e: unknown) {
      if (requestId !== readerRequest.current) return;
      setReaderError(`Couldn't revert: ${errorText(e)}`);
    }
  }

  function startEdit(text?: string) {
    if (!readerPage || !readerShowsItsPage()) return;
    const current = text ?? shownText(readerPage, readerView);
    setEditing({ text: current, original: current });
  }

  // Jump to the first search match whenever a page (or text version, or its text) is shown.
  // Keyed on whether an edit is open, not on its text: typing must not scroll the reader.
  const isEditing = editing !== null;
  const shownPage = readerPage ? `${readerPage.source_id}:${readerPage.page_num}` : null;
  const readerText = readerPage ? shownText(readerPage, readerView) : null;
  useEffect(() => {
    const body = readerBodyRef.current;
    if (!body || readerText === null || isEditing) return;
    const marks = body.querySelectorAll("mark");
    setMatchCount(marks.length);
    if (marks.length) marks[0].scrollIntoView({ block: "center" });
    else body.scrollTop = 0;
  }, [shownPage, readerText, readerView, isEditing, highlight]);

  // Page-turning keys, only while the reader is the top-most window: Settings, the import, book
  // check and duplicate windows all open above it. (Escape is handled by Dialog.)
  const readerOnTop = Boolean(reader) && !settingsOpen && !ingestOpen && !bookCheck && !dup;
  useEffect(() => {
    if (!readerOnTop) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.ctrlKey || e.altKey || e.metaKey || e.shiftKey || readerLoading) return;
      const target = e.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === "TEXTAREA" || target.tagName === "INPUT" || target.tagName === "SELECT" || target.isContentEditable)
      )
        return;
      if (e.key === "ArrowLeft") turnReaderPage(-1);
      else if (e.key === "ArrowRight") turnReaderPage(1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const ingestRunning = Boolean(progress && !progress.done);
  const ingestPercent = progress ? percent(progress.current, progress.total) : 0;
  useEffect(() => {
    if (!ingestOpen || !ingestRunning) return;
    const timer = window.setInterval(() => setIngestNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [ingestOpen, ingestRunning]);

  // Starts an import (by path, or by uploading a file) and shows it in the import window.
  // `starting` is shown until the backend reports; `failed` names what went wrong.
  async function startIngest(
    begin: () => Promise<{ id: number; path?: string }>,
    starting: string,
    failed: string,
    logName: string
  ) {
    setIngestLog([]);
    setIngestStarted(Date.now());
    setIngestId(null);
    setIngestOpen(true);
    setStatus(starting);
    setProgress({ id: -1, stage: "start", message: starting, current: 0, total: 1, done: false, error: null });
    try {
      const { id, path } = await begin();
      setIngestId(id);
      if (path) setIngestPath(path);
      setStatus(`Import queued (#${id}).`);
      uiLog(`${logName} -> ${id}`);
    } catch (e: unknown) {
      const message = errorText(e);
      setStatus(`${failed}: ${message}`);
      setProgress({ id: -1, stage: "error", message: failed, current: 0, total: 0, done: true, error: message });
    }
  }

  async function onPickAndIngest() {
    try {
      const file = await pickPdfFile();
      if (!file) return;
      setIngestPath(file.name);
      await startIngest(() => api.uploadAndIngest(file), "Uploading the PDF…", "Upload failed", "upload_and_ingest");
    } catch (e: unknown) {
      setStatus(`Couldn't open the file picker: ${errorText(e)}`);
    }
  }

  async function cancelIngest() {
    if (ingestId == null) {
      setStatus("Can't cancel yet: the import hasn't started.");
      return;
    }
    try {
      await api.cancelIngest(ingestId);
      setStatus("Cancel requested.");
    } catch (e: unknown) {
      setStatus(`Cancel failed: ${errorText(e)}`);
    }
  }

  async function resolveDuplicate(action: "discard" | "replace" | "new_copy") {
    if (!dup) return;
    try {
      await api.resolveDuplicate(dup.ingest_id, action);
      setDup(null);
      setStatus(
        action === "discard"
          ? "Duplicate: kept the existing copy."
          : action === "replace"
          ? "Duplicate: replacing the existing copy (rebuilding)…"
          : "Duplicate: adding it as a new copy…"
      );
    } catch (e: unknown) {
      setStatus(`Couldn't resolve the duplicate: ${errorText(e)}`);
    }
  }

  const sourcesListId = useId();

  // Layout: a top bar, then two panels (sources, search results) that fill the window.
  return (
    <div className={dark ? "app dark" : "app light"}>
      <div className="topbar">
        <div className="brand">
          <img className="brandLogo" src={appLogo} alt="" />
          <div className="title">
            <h1>Codex Engine</h1>
            <div className="sub">
              Rulebook library (local) · Active sources: {enabledCount}/{sources.length}
            </div>
          </div>
        </div>

        {/* Updates, theme and diagnostics live in Settings to keep this bar to the essentials. */}
        <div className="actions">
          <button className="btn primary" onClick={onPickAndIngest}>
            Add PDF
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
              <button
                className="btn small"
                onClick={() => setSourcesOpen((v) => !v)}
                aria-expanded={sourcesOpen}
                aria-controls={sourcesListId}
              >
                {sourcesOpen ? "Collapse" : "Expand"}
              </button>
            </div>
          </div>

          <div className="panelBody" id={sourcesListId} style={{ display: sourcesOpen ? "block" : "none" }}>
            {sources.length === 0 ? (
              <div style={{ color: "var(--muted)", fontSize: 13 }}>
                No sources yet. Add a PDF to begin.
              </div>
            ) : (
              sources.map((s) => (
                <div className="sourceCard" key={s.id} title={`${s.path}\nSHA-256: ${s.sha256}`}>
                  <div className="sourceTitle">{s.title}</div>
                  <div className="sourceMeta">
                    <span>{plural(s.pages, "page")}</span>
                  </div>

                  <div className="row" style={{ marginTop: 10 }}>
                    <label className="chk">
                      <input
                        type="checkbox"
                        checked={s.enabled}
                        onChange={(e) => toggleSourceEnabled(s.id, e.target.checked)}
                        aria-label={`Enabled: ${s.title}`}
                      />
                      Enabled
                    </label>

                    <div className="row">
                      <button
                        className="btn small"
                        onClick={() => checkBookAI(s.id, s.title)}
                        title="Find pages whose AI version is outdated or fails the current text check"
                        aria-label={`Check AI pages: ${s.title}`}
                      >
                        Check AI pages
                      </button>
                      <button
                        className="btn small danger"
                        onClick={() => deleteSource(s.id, s.title)}
                        aria-label={`Delete ${s.title}`}
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
              placeholder="Search… (e.g. kenku, mothman)"
              aria-label="Search the library"
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
                searchRequest.current++; // a search still running must not refill the list
                setQuery("");
                setResults([]);
                setSearchedFor("");
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
                <button
                  type="button"
                  className="resultCard"
                  key={`${r.source_id}-${r.page_num}-${idx}`}
                  onClick={() => openReader(r)}
                  title={`Read this page\n${r.source_path}`}
                >
                  {/* Inside a button only phrasing content is allowed: spans, styled as blocks. */}
                  <span className="resultTop">
                    <span className="resultHeading">{r.heading?.trim() || "(No heading)"}</span>
                    <span className="resultSrc">
                      {r.source_title} · p. {r.page_num}
                    </span>
                  </span>
                  <span className="resultSnippet">{markedSnippet(r.snippet)}</span>
                </button>
              ))
            )}
          </div>
        </div>
      </div>

      {/* Page reader */}
      {reader && (
        <Dialog
          className="reader"
          layer="reader"
          onClose={closeReader}
          closeOnBackdrop
          header={(titleId) => (
            <div className="modalHeader">
              <div className="readerTitle">
                <h3 id={titleId} title={readerPage?.path}>
                  {readerPage?.title ?? "Loading…"}
                </h3>
                <div className="readerSub">
                  Page {reader.page}
                  {readerPage ? ` of ${readerPage.page_count}` : ""}
                  {readerPage ? ` · ${VIEW_LABELS[readerView]}` : ""}
                  {readerView === "ai" && readerPage?.ai_model ? ` (${readerPage.ai_model})` : ""}
                  {readerView === "ai" && readerPage?.ai_stale
                    ? readerPage.ai_check_failed
                      ? " · fails the current text check"
                      : " · outdated, reformat to refresh"
                    : ""}
                  {readerPage && searchedFor && readerView !== "changes" && !editing
                    ? ` · ${matchCount ? plural(matchCount, "match", "matches") : "no matches"} for “${searchedFor.replace(/"/g, "")}”`
                    : ""}
                </div>
              </div>
              <button className="btn small" onClick={closeReader}>
                Close
              </button>
            </div>
          )}
        >
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
                    aria-pressed={readerView === "edited"}
                    onClick={() => setReaderView("edited")}
                    title={`Your own edit of this page${readerPage.edited_at ? ` (saved ${readerPage.edited_at} UTC)` : ""}`}
                  >
                    Edited
                  </button>
                )}
                <button
                  className={readerView === "ai" ? "on" : ""}
                  aria-pressed={readerView === "ai"}
                  onClick={() => setReaderView("ai")}
                  disabled={!readerPage?.ai_md}
                  title={readerPage?.ai_md ? "AI-formatted text" : "Not AI formatted yet"}
                >
                  AI{readerPage?.ai_stale ? "*" : ""}
                </button>
                <button
                  className={readerView === "clean" ? "on" : ""}
                  aria-pressed={readerView === "clean"}
                  onClick={() => setReaderView("clean")}
                  title="Rule-based cleanup of the extracted text"
                >
                  Cleaned
                </button>
                <button
                  className={readerView === "raw" ? "on" : ""}
                  aria-pressed={readerView === "raw"}
                  onClick={() => setReaderView("raw")}
                  disabled={!readerPage?.raw_text}
                  title={readerPage?.raw_text ? "Text exactly as extracted from the PDF" : "Re-open the book to capture raw text"}
                >
                  Raw
                </button>
                <button
                  className={readerView === "changes" ? "on" : ""}
                  aria-pressed={readerView === "changes"}
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
                <button
                  className="btn small"
                  onClick={revertEdit}
                  disabled={readerLoading}
                  title="Go back to the automatic (AI or cleaned) version"
                >
                  Revert edit
                </button>
              )}
              <button
                className="btn small"
                onClick={() => startEdit()}
                disabled={!readerPage || readerLoading || Boolean(editing)}
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
              {readerPage?.ai_check_failed && readerView !== "edited" && !aiBusy && !editing && !aiDecision && (
                <div className="decision" role="status">
                  <b>The saved AI version of this page didn't pass the stricter text check.</b>
                  <div className="hint">
                    It was made by an older version of Codex Engine, whose check could miss an added word, a dropped number
                    or a removed hyphen. {readerView === "ai" ? "You're viewing it anyway." : "The cleaned text is shown instead."}{" "}
                    Reformat the page to get an AI version that passes.
                  </div>
                  <div className="decisionActions">
                    <button className="btn small" onClick={() => aiFormatReaderPage()} disabled={!aiStatus?.available}>
                      Reformat page
                    </button>
                    {readerView === "ai" ? (
                      <button className="btn small" onClick={() => setReaderView("changes")}>
                        See what it changed
                      </button>
                    ) : (
                      <button className="btn small" onClick={() => setReaderView("ai")}>
                        Show the old AI version anyway
                      </button>
                    )}
                  </div>
                </div>
              )}
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
              ) : readerPage && readerText !== null ? (
                readerView === "raw" && readerPage.raw_text ? (
                  <pre className="rawText">{highlightText(readerText, highlight)}</pre>
                ) : readerView === "changes" && readerPage.ai_md ? (
                  <ChangesView
                    before={readerPage.clean_md}
                    after={readerPage.ai_md}
                    aiStale={readerPage.ai_stale}
                    aiCheckFailed={readerPage.ai_check_failed}
                  />
                ) : (
                  <Markdown text={readerText} highlight={highlight} />
                )
              ) : null}
            </div>
        </Dialog>
      )}

      {aiRun && (
        <AIProgressWindow
          run={aiRun}
          readerOnPage={Boolean(reader && reader.sourceId === aiRun.sourceId && reader.page === aiRun.page)}
          onCancel={cancelAiFormat}
          onClose={() => setAiRun(null)}
          onShowPage={() => openBookPage(aiRun.sourceId, aiRun.page)}
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
            updateStatus,
            versionText: `UI ${FRONTEND_VERSION} · API ${versionInfo?.backend_version ?? "…"} · Updater ${versionInfo?.updater_version ?? "…"}${
              versionInfo ? ` · ${versionInfo.platform}${versionInfo.updater_present ? "" : " · updater missing"}` : ""
            }`,
          }}
        />
      )}

      {/* Import window: progress of the current import, which keeps running if it's closed */}
      {ingestOpen && (
        <Dialog
          onClose={() => setIngestOpen(false)}
          header={(titleId) => (
            <div className="modalHeader">
              <h3 id={titleId}>Add a PDF</h3>
              <div className="row">
                <button className="btn small" onClick={() => setIngestOpen(false)}>
                  Close
                </button>
                <button className="btn small danger" onClick={cancelIngest} disabled={!ingestRunning}>
                  Cancel import
                </button>
              </div>
            </div>
          )}
        >
            <div className="modalBody">
              <div className="row" style={{ alignItems: "stretch" }}>
                <input
                  className="input"
                  value={ingestPath}
                  onChange={(e) => setIngestPath(e.target.value)}
                  placeholder="/path/to/book.pdf"
                  aria-label="PDF file path"
                />
                <button className="btn" onClick={onPickAndIngest}>
                  Upload PDF
                </button>
                <button
                  className="btn primary"
                  onClick={() =>
                    startIngest(() => api.startIngest(ingestPath), "Starting the import…", "Couldn't start the import", "start_ingest_pdf")
                  }
                >
                  Import
                </button>
              </div>

              <div className="progressWrap">
                {/* The only live region here: the clock and the log below would be read out on every tick. */}
                <div role="status" style={{ color: "var(--muted)", fontSize: 12 }}>
                  {progress ? progress.message : "Waiting…"}
                </div>

                <div style={{ marginTop: 10 }}>
                  <div
                    className="barOuter"
                    role="progressbar"
                    aria-label="Import progress"
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={ingestPercent}
                  >
                    <div className="barInner" style={{ width: `${ingestPercent}%` }} />
                  </div>
                  <div style={{ marginTop: 6, color: "var(--muted)", fontSize: 12 }}>
                    {progress && progress.total > 0
                      ? `${progress.current} / ${progress.total} (${ingestPercent}%)`
                      : "0 / 0 (0%)"}
                  </div>

                  {progress?.error ? (
                    <div style={{ marginTop: 8, color: "var(--danger)" }}>{progress.error}</div>
                  ) : null}

                  {ingestStarted && (
                    <div className="hint" style={{ marginTop: 6 }}>
                      Elapsed{" "}
                      {formatElapsed((progress?.done ? ingestLog[ingestLog.length - 1]?.at ?? ingestNow : ingestNow) - ingestStarted)}
                    </div>
                  )}
                  {ingestLog.length > 0 && (
                    <ol className="ingestLog">
                      {ingestLog.map((entry, i) => (
                        <li key={i}>
                          <span className="hint">{ingestStarted ? formatElapsed(entry.at - ingestStarted) : ""}</span>{" "}
                          {entry.message}
                        </li>
                      ))}
                    </ol>
                  )}
                </div>
              </div>
            </div>
        </Dialog>
      )}

      {/* Book check: which AI pages of one book are outdated, and "Reformat all" */}
      {bookCheck && (
        <Dialog
          onClose={bookRunning ? undefined : () => setBookCheck(null)}
          closeOnBackdrop
          header={(titleId) => (
            <div className="modalHeader">
              <h3 id={titleId}>AI pages in {bookCheck.title}</h3>
              <button className="btn small" onClick={() => setBookCheck(null)} disabled={bookRunning}>
                Close
              </button>
            </div>
          )}
        >
            <div className="modalBody" style={{ fontSize: 13, lineHeight: 1.5 }}>
              {bookCheck.error ? (
                <div className="errorText">{bookCheck.error}</div>
              ) : !bookCheck.report ? (
                <div style={{ color: "var(--muted)" }}>Checking every AI-formatted page…</div>
              ) : bookCheck.report.ai_pages === 0 ? (
                <div>No pages of this book have been AI formatted yet.</div>
              ) : bookCheck.report.outdated.length === 0 ? (
                <div>
                  {bookCheck.report.ai_pages === 1
                    ? "The AI-formatted page passes the current text check."
                    : `All ${bookCheck.report.ai_pages} AI-formatted pages pass the current text check.`}
                </div>
              ) : (
                <>
                  <div>
                    {bookCheck.report.outdated.length} of {bookCheck.report.ai_pages} AI-formatted page
                    {bookCheck.report.ai_pages === 1 ? "" : "s"} need reformatting. Until then the reader shows their cleaned text.
                  </div>
                  {(["failed_check", "source_changed"] as const).map((reason) => {
                    const pages = bookCheck.report!.outdated.filter((p) => p.reason === reason);
                    if (!pages.length) return null;
                    return (
                      <div key={reason} style={{ marginTop: 8 }}>
                        <b>{reason === "failed_check" ? "Fail the stricter text check" : "Made from older cleaned text"}:</b>{" "}
                        {pages.map((p, i) => (
                          <span key={p.page_num}>
                            {i ? ", " : ""}
                            <button className="linkBtn" onClick={() => openBookPage(bookCheck.sourceId, p.page_num)} disabled={bookRunning}>
                              p. {p.page_num}
                            </button>
                          </span>
                        ))}
                      </div>
                    );
                  })}
                </>
              )}

              {bookRun && (
                <div style={{ marginTop: 12 }}>
                  {bookRun.current !== null ? (
                    <div>
                      Reformatting page {bookRun.current} ({bookRun.done + 1} of {bookRun.total})…
                    </div>
                  ) : (
                    <div>
                      {bookRun.stopped ? "Stopped. " : "Finished. "}
                      {plural(bookRun.ok.length, "page")} reformatted and checked.
                    </div>
                  )}
                  {bookRun.needsYou.length > 0 && (
                    <div style={{ marginTop: 6 }}>
                      Need your decision (the AI couldn't format part of the page without changing it):{" "}
                      {bookRun.needsYou.map((page, i) => (
                        <span key={page}>
                          {i ? ", " : ""}
                          <button className="linkBtn" onClick={() => openBookPage(bookCheck.sourceId, page)} disabled={bookRunning}>
                            p. {page}
                          </button>
                        </span>
                      ))}
                      . Open a page and press "AI format page" to choose.
                    </div>
                  )}
                  {bookRun.failed.length > 0 && (
                    <div style={{ marginTop: 6 }}>
                      Not reformatted:{" "}
                      {bookRun.failed.map((f) => `p. ${f.page}${f.message ? ` (${f.message})` : ""}`).join("; ")}
                    </div>
                  )}
                </div>
              )}

              <div className="decisionActions" style={{ marginTop: 12 }}>
                {bookRunning ? (
                  <button className="btn small" onClick={stopBookReformat}>
                    Stop
                  </button>
                ) : (
                  bookCheck.report &&
                  bookCheck.report.outdated.length > 0 && (
                    <button
                      className="btn small primary"
                      onClick={reformatBook}
                      disabled={aiBusy || !aiStatus?.available}
                      title={
                        !aiStatus?.available
                          ? aiStatus?.error ?? "AI formatting is unavailable"
                          : aiBusy
                          ? "Wait for the page being formatted to finish"
                          : "Reformat these pages one at a time with the local AI"
                      }
                    >
                      Reformat all ({bookCheck.report.outdated.length})
                    </button>
                  )
                )}
              </div>
            </div>
        </Dialog>
      )}

      {/* Duplicate: the PDF being added is already in the library. No Escape: every way out is a choice. */}
      {dup && (
        <Dialog
          header={(titleId) => (
            <div className="modalHeader">
              <h3 id={titleId}>Already in the library</h3>
              <button className="btn small" title="Keep the existing source and don't add this file" onClick={() => resolveDuplicate("discard")}>
                Discard new
              </button>
            </div>
          )}
        >
            <div className="modalBody">
              <div style={{ color: "var(--muted)", fontSize: 13, lineHeight: 1.4 }}>
                The file you selected is identical to a source you already have (same SHA-256). Choose what to do:
              </div>

              <div style={{ marginTop: 10, fontSize: 13 }}>
                <div>
                  <b>Existing:</b> {dup.existing_title}
                </div>
                <div style={{ color: "var(--muted)" }} title={dup.existing_path}>
                  {dup.existing_path}
                </div>
                <div style={{ marginTop: 10 }}>
                  <b>New file:</b> {dup.new_title}
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
                  Add as a new copy
                </button>
              </div>
            </div>
        </Dialog>
      )}
    </div>
  );
}
