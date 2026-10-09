const { contextBridge, ipcRenderer } = require("electron");

let apiBase = null;
try {
  apiBase = ipcRenderer.sendSync("codex-engine:get-api-base");
} catch {
  apiBase = null;
}

contextBridge.exposeInMainWorld("codexEngine", {
  apiBase,
  setConsoleOpen(open) {
    ipcRenderer.send("codex-engine:set-console-open", Boolean(open));
  },
  log(line) {
    ipcRenderer.send("codex-engine:renderer-log", String(line));
  },
  logToFile(line) {
    ipcRenderer.send("codex-engine:renderer-log-file", String(line));
  },
  pickFolder(defaultPath) {
    return ipcRenderer.invoke("codex-engine:pick-folder", defaultPath ? String(defaultPath) : "");
  },
  pickFile(defaultPath) {
    return ipcRenderer.invoke("codex-engine:pick-file", defaultPath ? String(defaultPath) : "");
  },
  uninstall() {
    return ipcRenderer.invoke("codex-engine:uninstall");
  },
});

ipcRenderer.on("codex-engine:console-closed", () => {
  window.dispatchEvent(new Event("codex-engine:console-closed"));
});
