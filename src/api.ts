export type SourceRow = {
  id: number;
  title: string;
  path: string;
  sha256: string;
  pages: number;
  enabled: number;
  source_key?: string;
};

export type SearchRow = {
  source_id: number;
  source_title: string;
  source_path: string;
  page_num: number;
  heading: string | null;
  snippet: string;
  loc: string | null;
};

export type PageVersion = "edited" | "ai" | "clean" | "raw";
// What the reader shows: a stored version, or "changes" (AI version compared with the cleaned one).
export type ReaderView = PageVersion | "changes";

// What an accepted AI run changed (returned by ai-format).
export type AIChanges = {
  meaningful: boolean; // false: only invisible characters/spacing differed (already well formatted)
  lines_changed: number;
  markup: number;
  line_breaks: number;
  invisible_removed: number;
  summary: string;
};

// Sections the AI still couldn't format faithfully after its automatic retries.
export type AIPending = {
  sections: number;
  formatted: number;
  failed: { index: number; reason: string; attempts: number }[];
  draft_md: string; // accepted sections + cleaned text for the failed ones
};

// Pages of a book whose saved AI version is outdated: the cleaned text changed since
// ("source_changed") or it fails the current, stricter check ("failed_check").
export type BookAICheck = {
  source_id: number;
  title: string;
  ai_pages: number;
  outdated: { page_num: number; reason: "source_changed" | "failed_check" }[];
};

export type PageView = {
  source_id: number;
  page_num: number;
  page_count: number;
  title: string;
  path: string;
  raw_text: string | null;
  clean_md: string;
  clean_version: number;
  ai_md: string | null;
  ai_model: string | null;
  ai_error: string | null; // last AI attempt's rejection/partial note; cleaned text is used instead
  ai_updated_at: string | null;
  ai_stale: boolean; // ai_md was made from an older cleanup of this page, or fails the current check
  ai_check_failed?: boolean; // ai_md fails the current (stricter) faithfulness check
  edited_md: string | null; // the user's own correction; shown before anything else
  ai_changes?: AIChanges | null; // only on an ai-format response that saved a new AI version
  edited_at: string | null;
  best: PageVersion; // what the reader should show by default
  pending?: AIPending | null; // set when the user must decide what to do with failed sections
};

export type ModelPull = {
  model: string;
  status: string; // "starting" | "downloading" | … | "done" | "failed"
  completed: number;
  total: number;
  done: boolean;
  error: string | null;
};

export type AIStatus = {
  available: boolean;
  models: string[];
  model: string | null;
  error: string | null;
  state: "idle" | "starting" | "running" | "external" | "off" | "not_installed" | "error";
  mode: "managed" | "external" | "off";
  url: string;
  models_dir: string;
  ollama_path: string;
  pull: ModelPull | null;
};

// Per-machine settings (stored in the app data folder on each computer).
export type AppSettings = {
  ai_enabled: boolean;
  manage_ollama: boolean; // true: Codex Engine runs its own Ollama
  ollama_path: string; // "" = auto-detect
  models_dir: string; // "" = Ollama's default folder
  managed_port: number;
  external_url: string;
  model: string;
};

export type IngestProgress = {
  id: number;
  stage: string;
  message: string;
  current: number;
  total: number;
  done: boolean;
  error?: string | null;
};

export type DuplicateDetectedPayload = {
  ingest_id: number;
  new_path: string;
  new_title?: string;
  sha256?: string;
  existing_id: number;
  existing_title: string;
  existing_path: string;
};

export type VersionInfo = {
  app: string;
  frontend_version: string;
  backend_version: string;
  updater_version: string;
  platform: string;
  updater_present: boolean;
  updater_path?: string | null;
};

export type UpdateCheckResult = {
  status: "current" | "update_available" | "missing_installer" | "updater_launched" | "no_release";
  current_version: string;
  latest_version: string;
  release_url?: string | null;
  installer_name?: string | null;
  installer_url?: string | null;
  installer_path?: string | null;
  platform?: string | null;
  expected_asset?: string | null;
  message?: string | null;
};

declare global {
  interface Window {
    codexEngine?: {
      apiBase?: string | null;
      setConsoleOpen(open: boolean): void;
      log(line: string): void;
      logToFile?(line: string): void;
      pickFolder?(defaultPath?: string): Promise<string | null>;
      pickFile?(defaultPath?: string): Promise<string | null>;
      uninstall?(): Promise<string | null>; // error message, or null once the uninstaller started
    };
  }
}

// Electron may move the backend off 8787 if that port is taken, so prefer what the
// desktop shell tells us, then the build-time override, then the default.
const API_BASE =
  (typeof window !== "undefined" && window.codexEngine?.apiBase) ||
  import.meta.env.VITE_CODEX_ENGINE_API ||
  "http://127.0.0.1:8787";

// Required by the backend on every non-GET request (CSRF guard for the local API).
const CLIENT_HEADER = { "X-Codex-Engine-Client": "1" };

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { ...CLIENT_HEADER };
  if (!(init?.body instanceof FormData)) headers["Content-Type"] = "application/json";
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { ...headers, ...(init?.headers as Record<string, string> | undefined) },
  });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      message = body.detail || message;
    } catch {
      // keep default
    }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

export const api = {
  eventsUrl: `${API_BASE}/api/events`,
  getVersion: () => request<Omit<VersionInfo, "frontend_version">>("/api/version"),
  listSources: () => request<SourceRow[]>("/api/sources"),
  setSourceEnabled: (sourceId: number, enabled: boolean) =>
    request<{ ok: boolean }>(`/api/sources/${sourceId}/enabled`, {
      method: "PATCH",
      body: JSON.stringify({ enabled }),
    }),
  deleteSource: (sourceId: number) =>
    request<{ ok: boolean }>(`/api/sources/${sourceId}`, { method: "DELETE" }),
  search: (query: string) => request<SearchRow[]>(`/api/search?query=${encodeURIComponent(query)}`),
  getPage: (sourceId: number, page: number) => request<PageView>(`/api/sources/${sourceId}/pages/${page}`),
  aiStatus: () => request<AIStatus>("/api/ai/status"),
  checkBookAI: (sourceId: number) => request<BookAICheck>(`/api/sources/${sourceId}/ai-check`),
  getSettings: () => request<{ settings: AppSettings; path: string }>("/api/settings"),
  // 422 with a readable message if a value is invalid (e.g. a relative folder path).
  updateSettings: (changes: Partial<AppSettings>) =>
    request<{ settings: AppSettings; path: string }>("/api/settings", { method: "PUT", body: JSON.stringify(changes) }),
  pullModel: (model: string) =>
    request<ModelPull>("/api/ai/models/pull", { method: "POST", body: JSON.stringify({ model }) }),
  // 503 = AI unavailable (no Ollama/model); 200 with ai_error = output rejected, cleaned text kept.
  // useCleanForFailed: after `pending`, keep the cleaned text for the failed sections.
  aiFormatPage: (
    sourceId: number,
    page: number,
    opts: { model?: string | null; force?: boolean; useCleanForFailed?: boolean } = {}
  ) =>
    request<PageView>(`/api/sources/${sourceId}/pages/${page}/ai-format`, {
      method: "POST",
      body: JSON.stringify({
        model: opts.model ?? null,
        force: opts.force ?? false,
        use_clean_for_failed: opts.useCleanForFailed ?? false,
      }),
    }),
  saveEdit: (sourceId: number, page: number, markdown: string) =>
    request<PageView>(`/api/sources/${sourceId}/pages/${page}/edit`, { method: "PUT", body: JSON.stringify({ markdown }) }),
  // Stops a running AI format of the page; its request then fails with 409 "Cancelled...".
  cancelAiFormat: (sourceId: number, page: number) =>
    request<{ ok: boolean }>(`/api/sources/${sourceId}/pages/${page}/ai-format/cancel`, { method: "POST" }),
  revertEdit: (sourceId: number, page: number) =>
    request<PageView>(`/api/sources/${sourceId}/pages/${page}/edit`, { method: "DELETE" }),
  startIngest: (path: string) =>
    request<{ id: number }>("/api/ingest", { method: "POST", body: JSON.stringify({ path }) }),
  uploadAndIngest: (file: File) => {
    const data = new FormData();
    data.append("file", file);
    return request<{ id: number; path: string }>("/api/ingest/upload", { method: "POST", body: data });
  },
  cancelIngest: (ingestId: number) =>
    request<{ ok: boolean }>(`/api/ingest/${ingestId}/cancel`, { method: "POST" }),
  resolveDuplicate: (ingestId: number, action: "discard" | "replace" | "new_copy") =>
    request<{ ok: boolean }>("/api/ingest/duplicate", {
      method: "POST",
      body: JSON.stringify({ ingest_id: ingestId, action }),
    }),
  openPdfAtLocation: (path: string, page: number) =>
    request<{ ok: boolean }>("/api/open-pdf", { method: "POST", body: JSON.stringify({ path, page }) }),
  checkForUpdate: () => request<UpdateCheckResult>("/api/update/check"),
  // The backend re-checks GitHub itself; the client never chooses what gets installed.
  applyUpdate: () => request<UpdateCheckResult>("/api/update/apply", { method: "POST", body: "{}" }),
};

