import DeepSeekGuide from "./DeepSeekGuide";
import { useEffect, useState } from "react";
import { api, type AIStatus, type AppSettings, type ModelPull } from "./api";

// Per-machine model storage and optional user-funded cloud formatting.

const STATE_TEXT: Record<AIStatus["state"], string> = {
  idle: "Not started yet",
  starting: "Loading local model…",
  running: "Local model running inside Codex Engine",
  external: "Cloud formatting configured",
  off: "AI formatting is off",
  not_installed: "Local runtime missing",
  error: "Problem",
  missing_model: "Download a local model to get started",
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
  const [apiKey, setApiKey] = useState("");
  const [removeKey, setRemoveKey] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState("");

  const canBrowse = Boolean(window.codexEngine?.pickFolder);
  const dirty = Boolean(apiKey || removeKey || (saved && draft && JSON.stringify(saved) !== JSON.stringify(draft)));
  const pulling = Boolean(pull && !pull.done);

  useEffect(() => {
    api
      .getSettings()
      .then(({ settings, path }) => {
        setSaved(settings);
        setDraft(settings);
        setSettingsPath(path);
      })
      .catch((e: any) => setError(`Couldn't load settings: ${String(e)}`));
    refreshStatus();
  }, []);

  function refreshStatus() {
    api.aiStatus().then(onStatus).catch(() => undefined);
  }

  // The local model may still be loading when the panel opens:
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

  async function save(): Promise<AIStatus | null> {
    if (!draft || !saved) return null;
    const changes = Object.fromEntries(
      Object.entries(draft).filter(([k, v]) => saved[k as keyof AppSettings] !== v)
    ) as Partial<AppSettings>;
    setSaving(true);
    setError("");
    try {
      const { settings } = await api.updateSettings({ ...changes, ...(apiKey || removeKey ? { deepseek_api_key: removeKey ? "" : apiKey.trim() } : {}) });
      setSaved(settings);
      setDraft(settings);
      setApiKey("");
      setRemoveKey(false);
      setTestResult("");
      // Wait for a changed local model to finish loading.
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
    if (!draft) return;
    setError("");
    if (dirty && !(await save())) return;
    try {
      const result = await api.pullModel(draft.local_model);
      onStatus({ ...(status ?? { available: false, models: [], model: null, error: null, state: "missing_model", mode: "local", url: "", models_dir: "", pull: null }), pull: result });
    } catch (e: any) { setError(String(e).replace(/^Error: /, "")); }
  }

  const installed = status?.models ?? [];
  const catalog = status?.catalog ?? [{ id: "qwen2.5-1.5b-instruct-q4_k_m", label: "Qwen 2.5 1.5B Instruct (Q4_K_M, about 1.1 GB)", size: 1117320736 }];
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
            <b>AI formatting: {status?.mode === "deepseek" && status.available ? "DeepSeek configured (connection not yet checked)" : status ? STATE_TEXT[status.state] : "Checking…"}</b>
            {status?.mode === "local" && status.state === "running" ? (
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
                Use AI to format pages (optional; everything else works without it)
              </label>

              <div className="field">
                <span>Formatting provider</span>
                <select className="input" value={draft.ai_provider} onChange={(e) => set("ai_provider", e.target.value as AppSettings["ai_provider"])}>
                  <option value="local">Local model inside Codex Engine (no API charges)</option>
                  <option value="deepseek">DeepSeek (your API account and billing)</option>
                </select>
              </div>
              <DeepSeekGuide />
              {draft.ai_provider === "deepseek" && (
                <fieldset className="settingsGroup">
                  <legend>DeepSeek API</legend>
                  <div className="readerError" role="note">
                    <b>Paid service: you pay DeepSeek directly.</b> API usage is billed for text sent and generated, including automatic retries and regeneration.
                    Free chat access does not make the API free. Codex Engine provides no credits or billing cap.
                    Formatting sends page text to DeepSeek over the internet; only enable it for text you are comfortable sending.
                  </div>
                  <label className="chk">
                    <input type="checkbox" checked={draft.deepseek_enabled} onChange={(e) => set("deepseek_enabled", e.target.checked)} />
                    Enable DeepSeek — I understand the charges and that page text is sent to DeepSeek
                  </label>
                  <div className="hint">Off by default. Adding a key does not enable it. Changes apply when you click Save. Turn this off and Save to stop further requests; charges already incurred remain payable.</div>
                  <label className="chk">
                    <input type="checkbox" checked={draft.deepseek_confirm_cost} onChange={(e) => set("deepseek_confirm_cost", e.target.checked)} />
                    Show a cost estimate and ask before each DeepSeek formatting run (recommended)
                  </label>
                  <div className="hint">On by default. Estimates use a dated pricing snapshot and approximate token counts, including a higher estimate for retries. They are not a guaranteed bill or spending cap. Turn this off and Save to run without cost prompts, including book-wide reformatting.</div>
                  <div className="field">
                    <span>API key</span>
                    <input className="input" type="password" autoComplete="off" spellCheck={false} value={apiKey}
                      onChange={(e) => { setApiKey(e.target.value); setRemoveKey(false); setTestResult(""); }}
                      placeholder={draft.deepseek_has_key ? "Key saved — enter a replacement or leave blank to keep it" : "Paste your DeepSeek API key"} />
                    <div className="hint">Encrypted for this Windows account. The saved key is never displayed. Each computer needs its own setup.</div>
                    {draft.deepseek_has_key && <label className="chk"><input type="checkbox" checked={removeKey} onChange={(e) => { setRemoveKey(e.target.checked); setApiKey(""); }} />Remove the saved key when I Save</label>}
                  </div>
                  <div className="field">
                    <span>DeepSeek model</span>
                    <input className="input" value={draft.deepseek_model} onChange={(e) => set("deepseek_model", e.target.value)} />
                    <div className="hint">Default: deepseek-flash. Check current availability and pricing in the guide.</div>
                  </div>
                  <button className="btn small" disabled={saving || testing || (!apiKey && (!draft.deepseek_has_key || removeKey))} onClick={async () => {
                    setTesting(true); setError(""); setTestResult("");
                    try {
                      if (dirty && !(await save())) return;
                      const result = await api.testDeepSeek();
                      setTestResult(`${result.available ? "Key accepted; account has API credit." : "Key accepted; insufficient API credit. Set up billing before formatting."} ${result.balance}`);
                    } catch (e: any) { setError(String(e).replace(/^Error: /, "")); }
                    finally { setTesting(false); }
                  }}>{testing ? "Checking…" : dirty ? "Save & test API key" : "Test API key"}</button>
                  <div className="hint">Checks your account balance without sending book text or generating a paid reply. It does not test formatting quality or model access.</div>
                  {testResult && <div className="hint" role="status">{testResult}</div>}
                </fieldset>
              )}
              {draft.ai_provider === "local" && <fieldset className="settingsGroup">
                <legend>Local model</legend>
                <p>Codex Engine includes the model runtime. Download a model here once, then format pages offline. You do not need to install Ollama, Python, or another model app.</p>
                <div className="field">
                  <span>Model storage folder</span>
                  <div className="row">
                    <input className="input" value={draft.models_dir} onChange={(e) => set("models_dir", e.target.value)} placeholder={status?.models_dir || "Codex Engine’s data folder"} />
                    {canBrowse && <button className="btn small" onClick={browseFolder} disabled={pulling}>Browse…</button>}
                  </div>
                  <div className="hint">Choose a drive with at least 1.2 GB free for the model. Changing folders does not move an existing model; download it into the new folder.</div>
                </div>
                <div className="field">
                  <span>Use this local model</span>
                  <select className="input" value={draft.local_model} onChange={(e) => set("local_model", e.target.value)}>
                    {catalog.map((m) => <option key={m.id} value={m.id}>{m.label}{installed.includes(m.id) ? " — downloaded" : " — download needed"}</option>)}
                  </select>
                  <div className="hint">Runs on your CPU. Formatting keeps the book’s words; replies that change them are rejected.</div>
                  <button className="btn small" onClick={download} disabled={pulling || saving || (!dirty && installed.includes(draft.local_model))}>
                    {pulling ? "Downloading…" : saving ? "Saving…" : dirty ? "Save & download model" : installed.includes(draft.local_model) ? "Model downloaded" : "Download model"}
                  </button>
                  <div className="hint">Downloads the model from its publisher on Hugging Face and verifies its checksum before use. No book text is sent. Downloads require internet; local formatting does not.</div>
                  {pull && <div className="pullProgress">
                    <div className="barOuter"><div className="barInner" style={{ width: `${pull.done && !pull.error ? 100 : pct}%` }} /></div>
                    <div className="hint" role="status">{pull.error ? pull.error : pull.done ? "Model downloaded and verified." : `${pull.status}: ${formatBytes(pull.completed)} / ${formatBytes(pull.total)}`}</div>
                  </div>}
                </div>
              </fieldset>}
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
