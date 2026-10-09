const { app, BrowserWindow, dialog, ipcMain, shell } = require("electron");
const { spawn, spawnSync } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

const isPackaged = app.isPackaged;
const ROOT = path.resolve(__dirname, "..");
const PREFERRED_BACKEND_PORT = Number(process.env.CODEX_ENGINE_PORT || 8787);
let backendPort = PREFERRED_BACKEND_PORT;
const backendOrigin = () => `http://127.0.0.1:${backendPort}`;
const MAX_LOG_BYTES = 5 * 1024 * 1024;
const DEV_FRONTEND_URL = process.env.CODEX_ENGINE_FRONTEND_URL || "http://127.0.0.1:1420";
// CODEX_ENGINE_USE_BUILD=1 runs the built UI (dist/) in development, as the installed app does.
const FRONTEND_URL = isPackaged || process.env.CODEX_ENGINE_USE_BUILD === "1" ? null : DEV_FRONTEND_URL;
const SHUTDOWN_TOKEN = crypto.randomBytes(32).toString("hex");
// How long to wait for the backend (and its Ollama) to stop by itself before force-closing it.
const SHUTDOWN_REQUEST_TIMEOUT_MS = 3000;
const SHUTDOWN_GRACE_MS = 12000;
// The Start menu's "Codex Engine Data Folder" shortcut runs the app with this flag.
const OPEN_DATA_FOLDER_FLAG = "--open-data-folder";

// The backend keeps the library, settings and logs here (platformdirs' user_data_dir).
function dataFolder() {
  if (process.platform === "win32") {
    return path.join(process.env.LOCALAPPDATA || path.join(app.getPath("home"), "AppData", "Local"), "Codex Engine");
  }
  if (process.platform === "darwin") return path.join(app.getPath("home"), "Library", "Application Support", "Codex Engine");
  return path.join(process.env.XDG_DATA_HOME || path.join(app.getPath("home"), ".local", "share"), "Codex Engine");
}

function openDataFolder() {
  fs.mkdirSync(dataFolder(), { recursive: true });
  return shell.openPath(dataFolder());
}

const openingDataFolder = process.argv.includes(OPEN_DATA_FOLDER_FLAG);
if (openingDataFolder) {
  // Open the folder and leave: no window, no backend, and a running app isn't disturbed.
  app.whenReady().then(() => openDataFolder()).finally(() => app.exit(0));
}

const gotSingleInstanceLock = openingDataFolder || app.requestSingleInstanceLock();
if (!gotSingleInstanceLock) {
  app.quit();
  process.exit(0);
}

let backendProcess = null;
let mainWindow = null;
let consoleWindow = null;
let consoleTailTimer = null;
const consoleLogOffsets = new Map();
let ownsBackend = false;
let shuttingDownBackend = false;

function isPortOpen(port, host = "127.0.0.1") {
  return new Promise((resolve) => {
    const socket = net.createConnection({ port, host });
    socket.once("connect", () => {
      socket.end();
      resolve(true);
    });
    socket.once("error", () => {
      socket.destroy();
      resolve(false);
    });
  });
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function waitForPort(port, host = "127.0.0.1", timeoutMs = 15000) {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    function tryConnect() {
      isPortOpen(port, host).then((open) => {
        if (open) {
          resolve();
          return;
        }
        if (Date.now() - started > timeoutMs) {
          reject(new Error(`Timed out waiting for backend on ${host}:${port}`));
          return;
        }
        setTimeout(tryConnect, 250);
      });
    }
    tryConnect();
  });
}

// Is the thing listening on this port actually a Codex Engine backend?
async function isCodexBackend(port) {
  try {
    const response = await fetch(`http://127.0.0.1:${port}/api/health`, { signal: AbortSignal.timeout(1500) });
    if (!response.ok) return false;
    const body = await response.json();
    return body && body.app === "Codex Engine";
  } catch {
    return false;
  }
}

function getFreePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

function backendExecutable() {
  if (process.env.CODEX_ENGINE_BACKEND) return process.env.CODEX_ENGINE_BACKEND;

  if (isPackaged) {
    const name = process.platform === "win32" ? "codex-engine-backend.exe" : "codex-engine-backend";
    return path.join(process.resourcesPath, "backend", name);
  }

  if (process.env.CODEX_ENGINE_PYTHON) return process.env.CODEX_ENGINE_PYTHON;
  if (process.platform === "win32") {
    return path.join(ROOT, "backend", ".venv", "Scripts", "python.exe");
  }
  return path.join(ROOT, "backend", ".venv", "bin", "python");
}

// --exit-with-stdin: the backend stops when this app's end of its stdin closes, so it
// can't outlive a crashed or killed app.
function backendArgs() {
  const args = ["--host", "127.0.0.1", "--port", String(backendPort), "--exit-with-stdin"];
  if (isPackaged || process.env.CODEX_ENGINE_BACKEND) return args;
  return ["server_entry.py", ...args];
}

function backendCwd() {
  if (isPackaged) return path.join(process.resourcesPath, "backend");
  return path.join(ROOT, "backend");
}

function updaterExecutable() {
  if (process.env.CODEX_ENGINE_UPDATER_EXE) return process.env.CODEX_ENGINE_UPDATER_EXE;
  const name = process.platform === "win32" ? "codex-engine-updater.exe" : "codex-engine-updater";
  if (isPackaged) return path.join(process.resourcesPath, "updater", name);
  return path.join(ROOT, "resources", "updater", name);
}

function appExecutable() {
  if (process.env.CODEX_ENGINE_APP_EXE) return process.env.CODEX_ENGINE_APP_EXE;
  return isPackaged ? process.execPath : path.join(ROOT, "release", "electron", "win-unpacked", "Codex Engine.exe");
}

function logsDir() {
  const dir = path.join(app.getPath("userData"), "logs");
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

// Keep one previous generation so logs can't grow forever.
function rotateIfLarge(file) {
  try {
    if (fs.existsSync(file) && fs.statSync(file).size > MAX_LOG_BYTES) {
      fs.rmSync(`${file}.1`, { force: true });
      fs.renameSync(file, `${file}.1`);
    }
  } catch {
    // Log rotation is best-effort.
  }
}

function backendLogFiles() {
  const dir = logsDir();
  const out = path.join(dir, "backend.log");
  const err = path.join(dir, "backend-error.log");
  rotateIfLarge(out);
  rotateIfLarge(err);
  return {
    out: fs.openSync(out, "a"),
    err: fs.openSync(err, "a"),
  };
}

async function startBackend() {
  if (await isPortOpen(backendPort)) {
    // Dev convenience: reuse a backend you started yourself (npm run backend, with --reload).
    // A packaged app never reuses one: it could be a stale/older backend left behind,
    // or a completely different program on 8787 (wrangler dev uses it, for one).
    if (!isPackaged && (await isCodexBackend(backendPort))) {
      ownsBackend = false;
      return;
    }
    backendPort = await getFreePort();
  }

  const executable = backendExecutable();
  const logs = isPackaged ? backendLogFiles() : null;
  const stdio = logs ? ["pipe", logs.out, logs.err] : ["pipe", "inherit", "inherit"];
  ownsBackend = true;
  backendProcess = spawn(executable, backendArgs(), {
    cwd: backendCwd(),
    env: {
      ...process.env,
      PYTHONUNBUFFERED: "1",
      CODEX_ENGINE_SHUTDOWN_TOKEN: SHUTDOWN_TOKEN,
      CODEX_ENGINE_UPDATER_EXE: updaterExecutable(),
      CODEX_ENGINE_APP_EXE: appExecutable(),
    },
    stdio,
    windowsHide: true,
  });

  // Nothing is ever written to it; it only has to stay open while the app runs.
  backendProcess.stdin.on("error", () => {});

  backendProcess.once("error", (error) => {
    dialog.showErrorBox("Codex Engine backend failed", String(error));
    app.quit();
  });

  backendProcess.once("exit", (code, signal) => {
    backendProcess = null;
    if (!app.isQuitting && ownsBackend && !shuttingDownBackend) {
      dialog.showErrorBox("Codex Engine backend stopped", `Backend exited with code ${code ?? ""} ${signal ?? ""}`.trim());
      app.quit();
    }
  });
}

async function requestBackendShutdown() {
  if (!ownsBackend) return;
  shuttingDownBackend = true;

  const child = backendProcess;
  const exited = child
    ? new Promise((resolve) => {
        if (child.exitCode !== null || child.signalCode !== null) resolve();
        else child.once("exit", resolve);
      })
    : Promise.resolve();

  try {
    await fetch(`${backendOrigin()}/api/shutdown`, {
      method: "POST",
      headers: { "X-Codex-Engine-Shutdown-Token": SHUTDOWN_TOKEN },
      signal: AbortSignal.timeout(SHUTDOWN_REQUEST_TIMEOUT_MS),
    });
  } catch {
    // Gone, or stuck: closing stdin asks it to stop too, then the grace period applies.
    child?.stdin?.end();
  }

  // Give it time to stop Ollama itself; that's what keeps the model runners from leaking.
  await Promise.race([exited, sleep(SHUTDOWN_GRACE_MS)]);

  if (backendProcess) {
    const pid = backendProcess.pid;
    if (process.platform === "win32") {
      spawnSync("taskkill", ["/PID", String(pid), "/T", "/F"], { windowsHide: true });
    } else {
      backendProcess.kill("SIGTERM");
    }
    backendProcess = null;
  }
}

function consoleLogPaths() {
  const dir = path.join(app.getPath("userData"), "logs");
  return [
    { label: "backend", file: path.join(dir, "backend.log") },
    { label: "backend", file: path.join(dir, "backend-error.log") },
  ];
}

function consoleHtml() {
  return `<!doctype html>
<html>
  <head>
    <meta charset="UTF-8" />
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'" />
    <title>Codex Engine Console</title>
    <style>
      html, body { margin: 0; height: 100%; background: #070b10; color: #d7e3f1; }
      body { font-family: Consolas, "Cascadia Mono", "Courier New", monospace; font-size: 12px; }
      .bar { height: 34px; display: flex; align-items: center; padding: 0 12px; background: #101722; border-bottom: 1px solid #243244; color: #93a3b5; }
      pre { box-sizing: border-box; margin: 0; padding: 12px; height: calc(100% - 35px); overflow: auto; white-space: pre-wrap; word-break: break-word; }
    </style>
  </head>
  <body>
    <div class="bar">Codex Engine Console</div>
    <pre id="log"></pre>
    <script>
      const log = document.getElementById("log");
      window.codexConsole.onLine((line) => {
        log.textContent += line + "\\n";
        log.scrollTop = log.scrollHeight;
      });
    </script>
  </body>
</html>`;
}

function appendConsoleLine(line) {
  if (!consoleWindow || consoleWindow.isDestroyed()) return;
  consoleWindow.webContents.send("codex-engine:console-line", line);
}

function formatConsoleLine(label, line) {
  if (/\b(ERROR|CRITICAL|Traceback|Exception)\b/i.test(line)) {
    return `[${label}-error] ${line}`;
  }
  if (/\b(WARNING|WARN)\b/i.test(line)) {
    return `[${label}-warn] ${line}`;
  }
  return `[${label}] ${line}`;
}

function readNewLogContent(label, file) {
  try {
    if (!fs.existsSync(file)) return;
    const stat = fs.statSync(file);
    const previousOffset = consoleLogOffsets.get(file) ?? stat.size;
    if (stat.size < previousOffset) {
      consoleLogOffsets.set(file, 0);
      return;
    }
    if (stat.size === previousOffset) return;

    const length = stat.size - previousOffset;
    const fd = fs.openSync(file, "r");
    try {
      const buffer = Buffer.alloc(length);
      fs.readSync(fd, buffer, 0, length, previousOffset);
      consoleLogOffsets.set(file, stat.size);
      const text = buffer.toString("utf8").trimEnd();
      if (!text) return;
      for (const line of text.split(/\r?\n/)) {
        appendConsoleLine(formatConsoleLine(label, line));
      }
    } finally {
      fs.closeSync(fd);
    }
  } catch (error) {
    appendConsoleLine(`[console] Failed reading ${label}: ${error}`);
  }
}

function pollConsoleLogs() {
  for (const entry of consoleLogPaths()) {
    readNewLogContent(entry.label, entry.file);
  }
}

function startConsoleTail() {
  pollConsoleLogs();
  if (!consoleTailTimer) {
    consoleTailTimer = setInterval(pollConsoleLogs, 750);
  }
}

function stopConsoleTail() {
  if (consoleTailTimer) {
    clearInterval(consoleTailTimer);
    consoleTailTimer = null;
  }
}

function createConsoleWindow() {
  consoleWindow = new BrowserWindow({
    width: 980,
    height: 520,
    minWidth: 640,
    minHeight: 360,
    title: "Codex Engine Console",
    backgroundColor: "#070b10",
    icon: path.join(ROOT, "assets", process.platform === "win32" ? "icons-v2/codex-engine-v2.ico" : "icons-v2/codex-engine-v2-256.png"),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      // Only the log feed: the console must not get the main window's bridge (uninstall etc.).
      preload: path.join(__dirname, "console-preload.cjs"),
    },
  });
  consoleWindow.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(consoleHtml())}`);
  consoleWindow.once("ready-to-show", () => {
    appendConsoleLine("[console] Codex Engine Console opened.");
    startConsoleTail();
  });
  consoleWindow.on("closed", () => {
    consoleWindow = null;
    stopConsoleTail();
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send("codex-engine:console-closed");
    }
  });
}

function setConsoleOpen(open) {
  if (open) {
    if (consoleWindow && !consoleWindow.isDestroyed()) {
      consoleWindow.focus();
      return;
    }
    createConsoleWindow();
  } else if (consoleWindow && !consoleWindow.isDestroyed()) {
    consoleWindow.close();
  }
}

// The app's own page: the dev server in development, dist/index.html when packaged.
function isAppUrl(url) {
  try {
    const target = new URL(url);
    if (FRONTEND_URL) return target.origin === new URL(FRONTEND_URL).origin;
    const page = pathToFileURL(path.join(ROOT, "dist", "index.html"));
    return target.protocol === "file:" && decodeURIComponent(target.pathname).toLowerCase() === decodeURIComponent(page.pathname).toLowerCase();
  } catch {
    return false;
  }
}

// Every request below must come from the main window showing the app's own page, never
// from the console window or from anything that window may have been navigated to.
function fromMainWindow(event) {
  return Boolean(
    mainWindow &&
      !mainWindow.isDestroyed() &&
      event.sender === mainWindow.webContents &&
      event.senderFrame &&
      event.senderFrame === mainWindow.webContents.mainFrame &&
      isAppUrl(event.senderFrame.url),
  );
}

ipcMain.on("codex-engine:set-console-open", (event, open) => {
  if (!fromMainWindow(event)) return;
  setConsoleOpen(open);
});

ipcMain.on("codex-engine:renderer-log", (event, line) => {
  if (!fromMainWindow(event)) return;
  appendConsoleLine(String(line));
});

ipcMain.on("codex-engine:get-api-base", (event) => {
  // The preload asks while the page is still loading, so check the window, not the frame.
  event.returnValue = mainWindow && event.sender === mainWindow.webContents ? backendOrigin() : null;
});

// Native pickers for Settings (model storage folder, Ollama program). Return a path or null.
ipcMain.handle("codex-engine:pick-folder", async (event, defaultPath) => {
  if (!fromMainWindow(event)) return null;
  const result = await dialog.showOpenDialog(mainWindow, {
    title: "Choose a folder for AI models",
    defaultPath: typeof defaultPath === "string" && defaultPath ? defaultPath : undefined,
    properties: ["openDirectory", "createDirectory"],
  });
  return result.canceled ? null : result.filePaths[0] ?? null;
});

ipcMain.handle("codex-engine:pick-file", async (event, defaultPath) => {
  if (!fromMainWindow(event)) return null;
  const result = await dialog.showOpenDialog(mainWindow, {
    title: "Locate the Ollama program",
    defaultPath: typeof defaultPath === "string" && defaultPath ? defaultPath : undefined,
    properties: ["openFile"],
    filters: process.platform === "win32" ? [{ name: "Ollama", extensions: ["exe"] }] : [],
  });
  return result.canceled ? null : result.filePaths[0] ?? null;
});

// Settings > "Uninstall Codex Engine...": confirm, start the installed uninstaller, then
// quit so it can remove the app's files. Returns an error message, or null (started or
// cancelled).
ipcMain.handle("codex-engine:uninstall", async (event) => {
  if (!fromMainWindow(event)) return "Not allowed.";
  const uninstaller = path.join(path.dirname(process.execPath), "unins000.exe");
  if (!isPackaged || !fs.existsSync(uninstaller)) {
    return "The uninstaller is only available in the installed app.";
  }
  // Asked here, not only in the page, so nothing a page does can uninstall without the user.
  const { response } = await dialog.showMessageBox(mainWindow, {
    type: "warning",
    title: "Uninstall Codex Engine",
    message: "Uninstall Codex Engine?",
    detail: "Codex Engine will close and its uninstaller will start.",
    buttons: ["Uninstall", "Cancel"],
    defaultId: 1,
    cancelId: 1,
    noLink: true,
  });
  if (response !== 0) return null;
  try {
    spawn(uninstaller, [], { detached: true, stdio: "ignore" }).unref();
  } catch (error) {
    return `Couldn't start the uninstaller: ${String(error)}`;
  }
  setTimeout(() => app.quit(), 300);
  return null;
});

let uiLogPath = null;
ipcMain.on("codex-engine:renderer-log-file", (event, line) => {
  if (!fromMainWindow(event)) return;
  try {
    if (!uiLogPath) {
      uiLogPath = path.join(logsDir(), "ui.log");
      rotateIfLarge(uiLogPath);
    }
    fs.appendFileSync(uiLogPath, `${String(line)}\n`);
  } catch {
    // File logging is best-effort.
  }
});

// "Codex Engine 0.3.6": the version comes from package.json (app.getVersion()).
function windowTitle() {
  return `Codex Engine ${app.getVersion()}`;
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1180,
    height: 820,
    minWidth: 900,
    minHeight: 640,
    title: windowTitle(),
    backgroundColor: "#0b0f14",
    icon: path.join(ROOT, "assets", process.platform === "win32" ? "icons-v2/codex-engine-v2.ico" : "icons-v2/codex-engine-v2-256.png"),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "preload.cjs"),
    },
  });

  // Keep the version in the title bar: the page's <title> would otherwise replace it.
  mainWindow.on("page-title-updated", (event) => event.preventDefault());

  mainWindow.on("closed", () => {
    mainWindow = null;
    setConsoleOpen(false);
  });

  if (FRONTEND_URL) {
    mainWindow.loadURL(FRONTEND_URL);
  } else {
    mainWindow.loadFile(path.join(ROOT, "dist", "index.html"));
  }
}

// Nothing may take a window away from the app's own page: not a file dropped on it, not a
// link, not a script. New windows and <webview>s are refused outright.
app.on("web-contents-created", (_event, contents) => {
  contents.on("will-navigate", (navigation) => {
    if (!isAppUrl(navigation.url)) navigation.preventDefault();
  });
  contents.on("will-redirect", (navigation) => {
    if (!isAppUrl(navigation.url)) navigation.preventDefault();
  });
  contents.on("will-attach-webview", (attach) => attach.preventDefault());
  contents.setWindowOpenHandler(() => ({ action: "deny" }));
});

app.on("second-instance", () => {
  if (!mainWindow) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.focus();
});

app.whenReady().then(async () => {
  if (openingDataFolder) return;
  await startBackend();
  try {
    await waitForPort(backendPort);
  } catch (error) {
    dialog.showErrorBox("Codex Engine backend failed", String(error));
    app.quit();
    return;
  }
  createWindow();
});

app.on("window-all-closed", () => {
  if (mainWindow && !mainWindow.isDestroyed()) return;
  if (process.platform !== "darwin") app.quit();
});

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});

app.on("before-quit", (event) => {
  if (app.isQuitting) return;
  app.isQuitting = true;
  event.preventDefault();
  requestBackendShutdown().finally(() => app.exit(0));
});
