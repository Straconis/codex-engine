"""Runs Ollama for Codex Engine, so users don't have to.

In "managed" mode the backend starts its own `ollama serve` on a private port
(default 11435, so it never collides with an Ollama the user runs on 11434), with
OLLAMA_MODELS pointing at the folder from Settings: the system drive on one machine,
a companion drive on another. In "external" mode it just connects to a URL.

The managed process must never outlive the app: on Windows it is placed in a Job
Object that kills it when the backend exits for any reason (including a crash); on
Linux it gets a parent-death signal; and a pid file lets the next start clean up
anything left behind elsewhere. AI being unavailable is a normal state, never an
app failure.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from . import ai_format
from .settings import AppSettings, SettingsStore

STARTUP_TIMEOUT = 30.0

# Manager states
IDLE = "idle"  # managed mode, not started yet (starts on app start or first use)
STARTING = "starting"
RUNNING = "running"  # our own ollama serve is up
EXTERNAL = "external"  # using an Ollama the user runs
OFF = "off"  # AI formatting disabled in Settings
NOT_INSTALLED = "not_installed"
ERROR = "error"


def default_ollama_locations() -> list[Path]:
    home = Path.home()
    if sys.platform.startswith("win"):
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        return [local / "Programs" / "Ollama" / "ollama.exe", program_files / "Ollama" / "ollama.exe"]
    if sys.platform == "darwin":
        return [Path("/Applications/Ollama.app/Contents/Resources/ollama"), Path("/usr/local/bin/ollama"), Path("/opt/homebrew/bin/ollama")]
    return [Path("/usr/local/bin/ollama"), Path("/usr/bin/ollama"), home / ".local" / "bin" / "ollama"]


def find_ollama(configured: str = "") -> Path | None:
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_file() else None
    found = shutil.which("ollama")
    if found:
        return Path(found)
    return next((p for p in default_ollama_locations() if p.is_file()), None)


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((host, port)) == 0


def ollama_version(url: str, timeout: float = 1.0) -> str | None:
    try:
        with urllib.request.urlopen(f"{url}/api/version", timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")).get("version") or "unknown"
    except (urllib.error.URLError, OSError, ValueError):
        return None


# ---- process lifetime -------------------------------------------------------------

def _kill_with_parent_windows(proc: subprocess.Popen):
    """Put proc in a Job Object that kills it when this (backend) process exits."""
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ("Read", "Write", "Other", "ReadBytes", "WriteBytes", "OtherBytes")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = ExtendedLimits()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))  # 9 = ExtendedLimitInformation
    kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(int(proc._handle)))  # type: ignore[attr-defined]
    return job  # the handle must stay open for as long as the backend runs


def _close_job(job, terminate: bool) -> None:
    """Optionally kill every process in the job, then release the handle."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    if terminate:
        kernel32.TerminateJobObject(job, 1)
    kernel32.CloseHandle(job)  # with KILL_ON_JOB_CLOSE this also ends anything left


def _linux_parent_death_signal():  # runs in the child before exec
    import ctypes

    ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG


# ---- manager ---------------------------------------------------------------------

@dataclasses.dataclass
class PullState:
    model: str
    status: str = "starting"
    completed: int = 0
    total: int = 0
    done: bool = False
    error: str | None = None


class OllamaManager:
    def __init__(
        self,
        store: SettingsStore,
        state_dir: Path,
        log_dir: Path,
        publish: Callable[[str, dict], None] = lambda name, payload: None,
    ):
        self.store = store
        self.state_dir = state_dir
        self.log_dir = log_dir
        self.publish = publish
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._job = None
        self._state = IDLE
        self._message = ""
        self._url = ""
        self._models_dir = ""
        self._exe = ""
        self._pull: PullState | None = None
        self._port: int | None = None  # port of the Ollama we started (to wait for its release)
        self.apply(start=False)

    # -- configuration --

    @property
    def settings(self) -> AppSettings:
        return self.store.load()

    @staticmethod
    def _env_url() -> str | None:
        # Developer override: point at a specific Ollama and skip managing one.
        return os.environ.get("CODEX_ENGINE_OLLAMA_URL")

    def _target_url(self, s: AppSettings) -> str:
        if self._env_url():
            return self._env_url().rstrip("/")
        return f"http://127.0.0.1:{s.managed_port}" if s.manage_ollama else s.external_url

    def config(self) -> ai_format.AIConfig:
        """AI settings for the formatter: URL from the current mode, model from Settings."""
        base = ai_format.AIConfig.from_env()
        model = os.environ.get("CODEX_ENGINE_OLLAMA_MODEL") or self.settings.model
        return dataclasses.replace(base, url=self._url or self._target_url(self.settings), model=model)

    @property
    def _pid_file(self) -> Path:
        return self.state_dir / "ollama-managed.json"

    # -- lifecycle --

    def apply(self, start: bool = True) -> None:
        """(Re)apply settings: stop what no longer matches, then start if managed."""
        s = self.settings
        with self._lock:
            self._stop_locked()
            self._url = self._target_url(s)
            if not s.ai_enabled:
                self._set(OFF, "AI formatting is turned off in Settings.")
            elif self._env_url() or not s.manage_ollama:
                self._set(EXTERNAL, f"Using the Ollama at {self._url}.")
            else:
                self._set(IDLE, "")
        if start and self._state == IDLE:
            threading.Thread(target=self.ensure_running, daemon=True).start()

    def ensure_running(self, timeout: float = STARTUP_TIMEOUT) -> None:
        """Start managed Ollama if needed and wait until it answers (no-op in other modes)."""
        with self._lock:
            if self._state == IDLE or (self._state == RUNNING and not self._alive()):
                self._start_locked()
            starting = self._state == STARTING
        if starting:
            self._ready.wait(timeout)

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _set(self, state: str, message: str) -> None:
        self._state, self._message = state, message
        if state != STARTING:
            self._ready.set()
        else:
            self._ready.clear()

    def _start_locked(self) -> None:
        s = self.settings
        exe = find_ollama(s.ollama_path)
        if not exe:
            self._set(
                NOT_INSTALLED,
                "Ollama isn't installed (or wasn't found). Install it from ollama.com, or set its location in Settings.",
            )
            return
        env = os.environ.copy()
        env["OLLAMA_HOST"] = f"127.0.0.1:{s.managed_port}"
        models_dir = ""
        if s.models_dir:
            folder = Path(s.models_dir).expanduser()
            if not Path(folder.anchor).exists():
                self._set(ERROR, f"The model folder {folder} isn't available. Is that drive connected?")
                return
            try:
                folder.mkdir(parents=True, exist_ok=True)
                probe = folder / ".codex-engine-write-test"
                probe.write_text("ok")
                probe.unlink()
            except OSError as exc:
                self._set(ERROR, f"Can't use the model folder {folder}: {exc}")
                return
            env["OLLAMA_MODELS"] = models_dir = str(folder)

        if port_in_use(s.managed_port):
            self._reap_leftover(s.managed_port)
            if port_in_use(s.managed_port):
                self._set(ERROR, f"Port {s.managed_port} is already used by another program. Pick another port in Settings.")
                return

        self.log_dir.mkdir(parents=True, exist_ok=True)
        log = open(self.log_dir / "ollama.log", "ab")
        kwargs: dict = {"stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL, "env": env}
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        else:
            kwargs["start_new_session"] = True  # own process group, so stop() can take its runners too
            if sys.platform.startswith("linux"):
                kwargs["preexec_fn"] = _linux_parent_death_signal
        try:
            self._proc = subprocess.Popen(self._command(exe), **kwargs)
        except OSError as exc:
            log.close()
            self._set(ERROR, f"Couldn't start Ollama ({exe}): {exc}")
            return
        log.close()  # the child keeps its own handle
        if sys.platform.startswith("win"):
            try:
                self._job = _kill_with_parent_windows(self._proc)
            except Exception:
                self._job = None
        self._exe, self._models_dir = str(exe), models_dir
        self._port = s.managed_port
        self._write_pid_file(s.managed_port)
        self._set(STARTING, "Starting Ollama…")
        threading.Thread(target=self._wait_until_up, args=(self._proc, self._url), daemon=True).start()

    def _command(self, exe: Path) -> list[str]:
        return [str(exe), "serve"]

    def _wait_until_up(self, proc: subprocess.Popen, url: str) -> None:
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                with self._lock:
                    if self._proc is proc:
                        self._set(ERROR, f"Ollama exited during startup (code {proc.returncode}). See logs/ollama.log.")
                return
            if ollama_version(url):
                with self._lock:
                    if self._proc is proc:
                        self._set(RUNNING, "")
                self.publish("ai_status", self.status())
                return
            time.sleep(0.25)
        with self._lock:
            if self._proc is proc:
                self._set(ERROR, "Ollama didn't start within 30 seconds. See logs/ollama.log.")

    def _write_pid_file(self, port: int) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self._pid_file.write_text(json.dumps({"pid": self._proc.pid, "port": port}))
        except OSError:
            pass

    def _reap_leftover(self, port: int) -> None:
        """Stop an Ollama we started in an earlier run that didn't shut down (e.g. a crash)."""
        try:
            info = json.loads(self._pid_file.read_text())
        except (OSError, ValueError):
            return
        if info.get("port") != port or not ollama_version(f"http://127.0.0.1:{port}"):
            return  # whatever is on the port isn't the Ollama we recorded
        try:
            os.kill(int(info["pid"]), signal.SIGTERM)
        except (OSError, ValueError, KeyError):
            return
        for _ in range(20):
            if not port_in_use(port):
                break
            time.sleep(0.25)
        self._pid_file.unlink(missing_ok=True)

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        """Stop Ollama *and* the model runner processes it spawned."""
        proc, self._proc = self._proc, None
        job, self._job = self._job, None
        if proc is not None:
            if job is not None:
                _close_job(job, terminate=True)  # kills every process in the job, runners included
            elif proc.poll() is None:
                try:
                    if sys.platform.startswith("win"):
                        proc.terminate()
                    else:
                        os.killpg(proc.pid, signal.SIGTERM)  # own process group: takes the runners too
                except OSError:
                    pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            self._pid_file.unlink(missing_ok=True)
            # A stopped server can hold its port for a moment. Starting the replacement before
            # it's released means the new one can't bind, or "is it up?" gets answered by the
            # dying old one (seen as a reset connection right after a folder change).
            port = self._port
            for _ in range(40):
                if not port or not port_in_use(port):
                    break
                time.sleep(0.125)
        if self._state in (RUNNING, STARTING):
            self._set(IDLE, "")

    # -- status --

    def status(self) -> dict:
        s = self.settings
        with self._lock:
            state, message = self._state, self._message
        info = {
            "state": state,
            "mode": "off" if not s.ai_enabled else "external" if (self._env_url() or not s.manage_ollama) else "managed",
            "url": self._url,
            "models_dir": self._models_dir or s.models_dir,
            "ollama_path": self._exe or str(find_ollama(s.ollama_path) or ""),
            "pull": dataclasses.asdict(self._pull) if self._pull else None,
        }
        if state in (OFF, NOT_INSTALLED, ERROR, STARTING, IDLE):
            pending = {IDLE: "Ollama hasn't been started yet.", STARTING: "Ollama is starting…"}
            return {**info, "available": False, "models": [], "model": None, "error": message or pending.get(state, "")}
        result = {**info, **ai_format.status(config=self.config())}
        if result["error"] and not result["models"] and "no models" in result["error"]:
            result["error"] = f"No AI model downloaded yet. Download one (e.g. {s.model}) in Settings."
        return result

    # -- model downloads --

    def pull(self, model: str) -> PullState:
        """Download a model in the background; progress is published as `model_pull` events."""
        model = AppSettings(model=model).model  # validates the name
        with self._lock:
            if self._pull and not self._pull.done:
                raise RuntimeError(f"Already downloading {self._pull.model}.")
            self._pull = PullState(model=model)
            pull = self._pull
        threading.Thread(target=self._pull_worker, args=(pull,), daemon=True).start()
        return pull

    def _pull_worker(self, pull: PullState) -> None:
        try:
            self.ensure_running()
            url = self.config().url
            req = urllib.request.Request(
                f"{url}/api/pull",
                data=json.dumps({"model": pull.model, "stream": True}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            last = 0.0
            with urllib.request.urlopen(req, timeout=60) as resp:
                for raw in resp:
                    if not raw.strip():
                        continue
                    event = json.loads(raw)
                    if event.get("error"):
                        raise RuntimeError(event["error"])
                    pull.status = event.get("status", pull.status)
                    pull.total = int(event.get("total") or pull.total)
                    pull.completed = int(event.get("completed") or pull.completed)
                    if time.monotonic() - last > 0.25:
                        self.publish("model_pull", dataclasses.asdict(pull))
                        last = time.monotonic()
            pull.status = "done"
        except Exception as exc:  # network, disk full, unknown model...
            pull.error = str(exc)
            pull.status = "failed"
        pull.done = True
        self.publish("model_pull", dataclasses.asdict(pull))
        self.publish("ai_status", self.status())
