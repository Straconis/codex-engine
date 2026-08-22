const { app, BrowserWindow, dialog } = require("electron");
const { spawn, spawnSync } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");

const isPackaged = app.isPackaged;
const ROOT = path.resolve(__dirname, "..");
const BACKEND_PORT = Number(process.env.CODEX_ENGINE_PORT || 8787);
const BACKEND_ORIGIN = `http://127.0.0.1:${BACKEND_PORT}`;
const DEV_FRONTEND_URL = process.env.CODEX_ENGINE_FRONTEND_URL || "http://127.0.0.1:1420";
const FRONTEND_URL = isPackaged ? null : DEV_FRONTEND_URL;
const SHUTDOWN_TOKEN = crypto.randomBytes(32).toString("hex");

const gotSingleInstanceLock = app.requestSingleInstanceLock();
if (!gotSingleInstanceLock) {
  app.quit();
  process.exit(0);
}

let backendProcess = null;
let mainWindow = null;
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

function backendArgs() {
  if (isPackaged || process.env.CODEX_ENGINE_BACKEND) {
    return ["--host", "127.0.0.1", "--port", String(BACKEND_PORT)];
  }
  return ["-m", "uvicorn", "codex_engine.app:app", "--host", "127.0.0.1", "--port", String(BACKEND_PORT)];
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
  return isPackaged ? process.execPath : path.join(ROOT, "dist", "win-unpacked", "Codex Engine.exe");
}

function backendLogFiles() {
  const dir = path.join(app.getPath("userData"), "logs");
  fs.mkdirSync(dir, { recursive: true });
  return {
    out: fs.openSync(path.join(dir, "backend.log"), "a"),
    err: fs.openSync(path.join(dir, "backend-error.log"), "a"),
  };
}

async function startBackend() {
  if (await isPortOpen(BACKEND_PORT)) {
    ownsBackend = false;
    return;
  }

  const executable = backendExecutable();
  const logs = isPackaged ? backendLogFiles() : null;
  const stdio = logs ? ["ignore", logs.out, logs.err] : "inherit";
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

  try {
    await fetch(`${BACKEND_ORIGIN}/api/shutdown`, {
      method: "POST",
      headers: { "X-Codex-Engine-Shutdown-Token": SHUTDOWN_TOKEN },
    });
  } catch {
    // The backend may already be gone; fall through to process cleanup.
  }

  await sleep(750);

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

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1180,
    height: 820,
    minWidth: 900,
    minHeight: 640,
    title: "Codex Engine",
    backgroundColor: "#0b0f14",
    icon: path.join(ROOT, "assets", process.platform === "win32" ? "icons-v2/codex-engine-v2.ico" : "icons-v2/codex-engine-v2-256.png"),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  if (FRONTEND_URL) {
    mainWindow.loadURL(FRONTEND_URL);
  } else {
    mainWindow.loadFile(path.join(ROOT, "dist", "index.html"));
  }
}

app.on("second-instance", () => {
  if (!mainWindow) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.focus();
});

app.whenReady().then(async () => {
  await startBackend();
  try {
    await waitForPort(BACKEND_PORT);
  } catch (error) {
    dialog.showErrorBox("Codex Engine backend failed", String(error));
    app.quit();
    return;
  }
  createWindow();
});

app.on("window-all-closed", () => {
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
