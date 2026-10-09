import { useEffect, useState } from "react";
import { api, type AIStatus, type AppSettings, type ModelPull } from "./api";

// Per-machine AI settings. Codex Engine can run Ollama itself, storing models wherever
// this computer has room (system drive, second drive, companion/external drive).

const STATE_TEXT: Record<AIStatus["state"], string> = {
  idle: "Not started yet",
  starting: "Starting Ollama…",
  running: "Running (managed by Codex Engine)",
  external: "Using your own Ollama",
  off: "AI formatting is off",
  not_installed: "Ollama not found",
  error: "Problem",
};

function formatBytes(n: number) {
  if (!n) return "0 MB";
  return n >= 1e9 ? `${(n / 1e9).toFixed(2)} GB` : `${Math.round(n / 1e6)} MB`;
}

// App-wide preferences and tools (moved here from the top bar to keep it uncluttered).
export type GeneralSettings = {
  dark: boolean;
  onDarkChange: (dark: boolean) => void;
  logToConsole: boolean;
  onLogToConsoleChange: (on: boolean) => void;
  logToFile: boolean;
  onLogToFileChange: (on: boolean) => void;
  onCheckUpdates: () => void;
  checkingUpdates: boolean;
  updateStatus: string; // result of the last update check/apply only (not the app-wide status line)
  versionText: string;
};

type Props = {
  status: AIStatus | null;
  pull: ModelPull | null;
  onClose: () => void;
  onStatus: (status: AIStatus) => void;
  general: GeneralSettings;
};

export default function SettingsPanel({ status, pull, onClose, onStatus, general }: Props) {
  const [saved, setSaved] = useState<AppSettings | null>(null);
  const [draft, setDraft] = useState<AppSettings | null>(null);
  const [settingsPath, setSettingsPath] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [downloadName, setDownloadName] = useState("");

  const canBrowse = Boolean(window.codexEngine?.pickFolder);
  const dirty = Boolean(saved && draft && JSON.stringify(saved) !== JSON.stringify(draft));
  const pulling = Boolean(pull && !pull.done);

  useEffect(() => {
    api
      .getSettings()
      .then(({ settings, path }) => {
        setSaved(settings);
        setDraft(settings);
        setSettingsPath(path);
        setDownloadName(settings.model);
      })
      .catch((e: any) => setError(`Couldn't load settings: ${String(e)}`));
    refreshStatus();
  }, []);

  function refreshStatus() {
    api.aiStatus().then(onStatus).catch(() => undefined);
  }

  // Ollama may still be starting when the panel opens (e.g. right after app launch):
  // keep checking until it settles, so the panel never shows a stale "starting".
  const settling = !status || status.state === "idle" || status.state === "starting";
  useEffect(() => {
    if (!settling) return;
    const timer = window.setInterval(refreshStatus, 1500);
    return () => window.clearInterval(timer);
  }, [settling]);

  function set<K extends keyof AppSettings>(key: K, value: AppSettings[K]) {
    setDraft((d) => (d ? { ...d, [key]: value } : d));
  }

  async function browseFolder() {
    const picked = await window.codexEngine?.pickFolder?.(draft?.models_dir || status?.models_dir || "");
    if (picked) set("models_dir", picked);
  }

  async function browseOllama() {
    const picked = await window.codexEngine?.pickFile?.(draft?.ollama_path || status?.ollama_path || "");
    if (picked) set("ollama_path", picked);
  }

  // Returns the AI status once Ollama has settled with the saved settings, or null on error.
  async function save(): Promise<AIStatus | null> {
    if (!draft || !saved) return null;
    const changes = Object.fromEntries(
      Object.entries(draft).filter(([k, v]) => saved[k as keyof AppSettings] !== v)
    ) as Partial<AppSettings>;
    setSaving(true);
    setError("");
    try {
      const { settings } = await api.updateSettings(changes);
      setSaved(settings);
      setDraft(settings);
      // The backend has stopped the old Ollama; wait for the new one (new folder/port) to settle.
      let s = await api.aiStatus();
      for (let i = 0; i < 30 && (s.state === "idle" || s.state === "starting"); i++) {
        onStatus(s);
        await new Promise((r) => setTimeout(r, 1000));
        s = await api.aiStatus();
      }
      onStatus(s);
      return s;
    } catch (e: any) {
      setError(String(e).replace(/^Error: /, ""));
      return null;
    } finally {
      setSaving(false);
    }
  }

  // With unsaved changes this is "Save & download": settings are saved first so the
  // model lands in the folder shown above, not the previously saved one.
  async function download() {
    const name = downloadName.trim();
    if (!name) return;
    setError("");
    if (dirty) {
      const s = await save();
      if (!s) return;
      if (s.state !== "running" && s.state !== "external") {
        setError(`Settings saved, but Ollama isn't running, so the download didn't start. ${s.error ?? ""}`.trim());
        return;
      }
    }
    try {
      await api.pullModel(name);
    } catch (e: any) {
      setError(String(e).replace(/^Error: /, ""));
    }
  }

  const ollamaReady = status?.state === "running" || status?.state === "external";
  const downloadBlocked = pulling || dirty
    ? ""
    : !ollamaReady
    ? status?.state === "starting" || status?.state === "idle"
      ? "Waiting for Ollama to start…"
      : "Ollama isn't running, see the status above."
    : "";

  const installed = status?.models ?? [];
  const modelOptions = draft && !installed.includes(draft.model) ? [draft.model, ...installed] : installed;
  const pct = pull && pull.total ? Math.min(100, Math.round((pull.completed / pull.total) * 100)) : 0;

  return (
    <div className="overlay" onMouseDown={onClose}>
      <div className="modal settings" onMouseDown={(e) => e.stopPropagation()}>
        <div className="modalHeader">
          <h3>Settings</h3>
          <button className="btn small" onClick={onClose}>
            Close
          </button>
        </div>

        <div className="modalBody settingsBody">
          <div className={`aiState state-${status?.state ?? "idle"}`}>
            <b>AI formatting: {status ? STATE_TEXT[status.state] : "Checking…"}</b>
            {status?.state === "running" || status?.state === "external" ? (
              <span>
                {" "}
                · {installed.length} model{installed.length === 1 ? "" : "s"} installed
                {status.models_dir ? ` · models in ${status.models_dir}` : ""}
              </span>
            ) : null}
            {status?.error && <div className="hint">{status.error}</div>}
          </div>

          {!draft ? (
            <div className="hint">Loading…</div>
          ) : (
            <>
              <label className="chk">
                <input type="checkbox" checked={draft.ai_enabled} onChange={(e) => set("ai_enabled", e.target.checked)} />
                Use local AI to format pages (optional; everything else works without it)
              </label>

              <fieldset className="settingsGroup" disabled={!draft.ai_enabled}>
                <legend>Ollama</legend>
                <label className="radio">
                  <input type="radio" checked={draft.manage_ollama} onChange={() => set("manage_ollama", true)} />
                  Codex Engine runs Ollama for me (recommended)
                </label>
                <label className="radio">
                  <input type="radio" checked={!draft.manage_ollama} onChange={() => set("manage_ollama", false)} />
                  I run Ollama myself
                </label>

                {draft.manage_ollama ? (
                  <>
                    <div className="field">
                      <span>Model storage folder</span>
                      <div className="row">
                        <input
                          className="input"
                          value={draft.models_dir}
                          onChange={(e) => set("models_dir", e.target.value)}
                          placeholder="Ollama's default folder on this computer"
                        />
                        {canBrowse && (
                          <button className="btn small" onClick={browseFolder}>
                            Browse…
                          </button>
                        )}
                      </div>
                      <div className="hint">
                        Models are large (about 1–5 GB each). Pick a drive with room, such as a second or companion drive
                        (e.g. D:\ollama\models). Each computer keeps its own setting.
                      </div>
                    </div>
                    <div className="field">
                      <span>Ollama program</span>
                      <div className="row">
                        <input
                          className="input"
                          value={draft.ollama_path}
                          onChange={(e) => set("ollama_path", e.target.value)}
                          placeholder={status?.ollama_path ? `Auto-detected: ${status.ollama_path}` : "Auto-detect"}
                        />
                        {canBrowse && (
                          <button className="btn small" onClick={browseOllama}>
                            Browse…
                          </button>
                        )}
                      </div>
                      {status?.state === "not_installed" && (
                        <div className="hint">Install Ollama from ollama.com (any folder or drive), then pick it here if it isn't found automatically.</div>
                      )}
                    </div>
                    <div className="field narrow">
                      <span>Port</span>
                      <input
                        className="input"
                        type="number"
                        min={1024}
                        max={65535}
                        value={draft.managed_port}
                        onChange={(e) => set("managed_port", Number(e.target.value))}
                      />
                    </div>
                  </>
                ) : (
                  <div className="field">
                    <span>Ollama address</span>
                    <input className="input" value={draft.external_url} onChange={(e) => set("external_url", e.target.value)} />
                    <div className="hint">Usually http://127.0.0.1:11434. Can be another computer on your network.</div>
                  </div>
                )}
              </fieldset>

              <fieldset className="settingsGroup" disabled={!draft.ai_enabled}>
                <legend>Model</legend>
                <div className="field">
                  <span>Use this model for formatting</span>
                  <select className="input" value={draft.model} onChange={(e) => set("model", e.target.value)}>
                    {modelOptions.map((m) => (
                      <option key={m} value={m}>
                        {m}
                        {installed.includes(m) ? "" : " (not downloaded)"}
                      </option>
                    ))}
                  </select>
                  <div className="hint">Small models (1–3B) are fast and light. Output that changes the book's wording is always rejected.</div>
                </div>
                <div className="field">
                  <span>Download a model</span>
                  <div className="row">
                    <input className="input" value={downloadName} onChange={(e) => setDownloadName(e.target.value)} placeholder="qwen2.5:1.5b" />
                    <button
                      className="btn small"
                      onClick={download}
                      disabled={pulling || saving || Boolean(downloadBlocked)}
                      title={downloadBlocked || "Downloads into the model storage folder"}
                    >
                      {pulling ? "Downloading…" : saving ? "Saving…" : dirty ? "Save & download" : "Download"}
                    </button>
                  </div>
                  {downloadBlocked && <div className="hint">{downloadBlocked}</div>}
                  {!downloadBlocked && dirty && !pulling && (
                    <div className="hint">You have unsaved changes. They'll be saved first, so the model is stored in the folder shown above.</div>
                  )}
                  {pull && (
                    <div className="pullProgress">
                      <div className="barOuter">
                        <div className="barInner" style={{ width: `${pull.done && !pull.error ? 100 : pct}%` }} />
                      </div>
                      <div className="hint">
                        {pull.model}: {pull.error ? `failed: ${pull.error}` : pull.done ? "downloaded" : `${pull.status} ${formatBytes(pull.completed)} / ${formatBytes(pull.total)}`}
                      </div>
                    </div>
                  )}
                </div>
              </fieldset>
            </>
          )}

          {/* Applied immediately; not part of the Save button above. */}
          <fieldset className="settingsGroup">
            <legend>General</legend>
            <div className="row">
              <span className="radio">Theme</span>
              <div className="seg" role="group" aria-label="Theme">
                <button className={general.dark ? "on" : ""} onClick={() => general.onDarkChange(true)}>
                  Dark
                </button>
                <button className={!general.dark ? "on" : ""} onClick={() => general.onDarkChange(false)}>
                  Light
                </button>
              </div>
            </div>
            <div className="field">
              <div className="row">
                <span>Updates</span>
                <button className="btn small" onClick={general.onCheckUpdates} disabled={general.checkingUpdates}>
                  {general.checkingUpdates ? "Checking…" : "Check for updates"}
                </button>
              </div>
              <div className="hint">{general.versionText}</div>
              {general.updateStatus && <div className="hint">{general.updateStatus}</div>}
            </div>
            <div className="field">
              <span>Diagnostics</span>
              <label className="chk">
                <input type="checkbox" checked={general.logToConsole} onChange={(e) => general.onLogToConsoleChange(e.target.checked)} />
                Show the log console window
              </label>
              <label className="chk">
                <input type="checkbox" checked={general.logToFile} onChange={(e) => general.onLogToFileChange(e.target.checked)} />
                Write UI logs to a file (logs/ui.log)
              </label>
            </div>
            {window.codexEngine?.uninstall && (
              <div className="field">
                <span>Remove Codex Engine</span>
                <div className="row">
                  <span className="hint">
                    Also in the Start menu (Codex Engine folder) and Windows Settings &gt; Apps.
                  </span>
                  <button
                    className="btn small danger"
                    onClick={async () => {
                      // The desktop app asks for confirmation itself.
                      try {
                        const problem = await window.codexEngine!.uninstall!();
                        if (problem) setError(problem);
                      } catch (e) {
                        setError(`Couldn't start the uninstaller: ${String(e)}`);
                      }
                    }}
                  >
                    Uninstall Codex Engine…
                  </button>
                </div>
              </div>
            )}
          </fieldset>

          {error && <div className="readerError">{error}</div>}

          <div className="modalActions">
            <span className="hint settingsPath" title={settingsPath}>
              Saved on this computer: {settingsPath}
            </span>
            {dirty && (
              <button className="btn" onClick={onClose} title="Close without saving">
                Discard changes
              </button>
            )}
            <button className="btn primary" onClick={save} disabled={!dirty || saving}>
              {saving ? "Applying…" : "Save"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
