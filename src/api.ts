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

const API_BASE = import.meta.env.VITE_CODEX_ENGINE_API ?? "http://127.0.0.1:8787";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: init?.body instanceof FormData ? undefined : { "Content-Type": "application/json" },
    ...init,
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
  applyUpdate: (update: UpdateCheckResult) =>
    request<UpdateCheckResult>("/api/update/apply", {
      method: "POST",
      body: JSON.stringify({ update }),
    }),
  requestShutdown: () =>
    fetch(`${API_BASE}/api/shutdown`, { method: "POST", keepalive: true }).catch(() => undefined),
};

