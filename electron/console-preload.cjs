const { contextBridge, ipcRenderer } = require("electron");

// The console window only receives log lines; it gets nothing else from the main process.
contextBridge.exposeInMainWorld("codexConsole", {
  onLine(callback) {
    const listener = (_event, line) => callback(String(line));
    ipcRenderer.on("codex-engine:console-line", listener);
    return () => ipcRenderer.removeListener("codex-engine:console-line", listener);
  },
});
