"""Run the shipped local runtime and download a verified model, without installing Ollama."""
from __future__ import annotations
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from . import ai_format
from .local_catalog import CATALOG, DEFAULT_MODEL, RUNTIME_TAG
from .local_client import LocalClient
from .process_lifetime import _kill_with_parent_windows, _close_job

LOCAL_SETTINGS = ("ai_enabled", "ai_provider", "models_dir", "local_model")


@dataclasses.dataclass
class Download:
    model: str
    status: str = "starting"
    completed: int = 0
    total: int = 0
    done: bool = False
    error: str | None = None


def runtime_path():
    folder = os.environ.get("CODEX_ENGINE_LOCAL_RUNTIME_DIR")
    if folder:
        root = Path(folder)
    elif getattr(sys, "frozen", False):
        root = Path(sys.executable).parent.parent / "llama"
    else:
        root = Path(__file__).resolve().parents[2] / "resources" / "llama"
    return root / ("llama-server.exe" if sys.platform == "win32" else "llama-server")


class LocalModelManager:
    def __init__(self, store, state_dir, log_dir, publish=lambda *args: None):
        self.store, self.state_dir, self.log_dir, self.publish = store, state_dir, log_dir, publish
        self._lock = threading.RLock()
        self._proc = None
        self._job = None
        self._token = ""
        self._url = ""
        self._state = "idle"
        self._message = ""
        self._signature = None
        self._pull = None
        self._download_cancel = threading.Event()
        self._ready = threading.Event()
        self._failed_at = 0.0

    @property
    def settings(self):
        return self.store.load()

    def models_folder(self, s=None):
        s = s or self.settings
        return Path(s.models_dir) if s.models_dir else self.state_dir / "models"

    def installed_models(self, s=None):
        folder = self.models_folder(s)
        result = []
        for name, asset in CATALOG.items():
            path = folder / asset.filename
            try:
                stat = path.stat()
                marker = json.loads(path.with_suffix(".verified.json").read_text(encoding="utf-8"))
                if stat.st_size == asset.size and marker == {"sha256": asset.sha256, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}:
                    result.append(name)
            except (OSError, ValueError):
                pass
        return result

    def config(self):
        return dataclasses.replace(ai_format.AIConfig.from_env(), url=self._url,
            model=f"llama.cpp/{RUNTIME_TAG}/{self.settings.local_model}", timeout=300)

    def client(self):
        return LocalClient(self.config(), self._token)

    def _set(self, state, message=""):
        self._state, self._message = state, message
        if state != "starting":
            self._ready.set()

    def status(self):
        s = self.settings
        with self._lock:
            enabled = s.ai_enabled and s.ai_provider == "local"
            models = self.installed_models(s)
            state, message = self._state, self._message
            if not enabled:
                state, message = "off", "Local AI formatting is turned off."
            elif s.local_model not in models:
                state, message = "missing_model", "Download the local model in Settings. No separate software installation is needed."
            elif state == "running" and (not self._proc or self._proc.poll() is not None):
                state, message = "error", "The local model stopped. Try formatting again to restart it."
            return {"available": enabled and state == "running", "state": state, "mode": "local" if enabled else "off",
                "error": message or None, "models": models, "model": s.local_model if enabled and state == "running" else None,
                "url": self._url, "models_dir": str(self.models_folder(s)), "ollama_path": "",
                "pull": dataclasses.asdict(self._pull) if self._pull else None,
                "catalog": [{"id": a.id, "label": a.label, "size": a.size} for a in CATALOG.values()]}

    def apply(self, start=True):
        with self._lock:
            if self._pull and not self._pull.done:
                self._download_cancel.set()
            self._stop_locked()
            self._signature = None
            self._failed_at = 0
            self._set("idle")
        if start:
            threading.Thread(target=self.ensure_running, daemon=True).start()

    def ensure_running(self, timeout=90):
        s = self.settings
        if not s.ai_enabled or s.ai_provider != "local":
            return
        signature = (s.local_model, str(self.models_folder(s)))
        with self._lock:
            if self._proc and self._proc.poll() is None and self._signature == signature:
                if self._state == "running":
                    return
            elif self._state != "starting":
                if self._state == "error" and time.monotonic() - self._failed_at < 10:
                    return
                self._start_locked(s, signature)
            starting = self._state == "starting"
        if starting:
            self._ready.wait(timeout)

    def _start_locked(self, s, signature):
        self._stop_locked()
        if s.local_model not in self.installed_models(s):
            self._set("missing_model", "Download the local model in Settings.")
            return
        exe = runtime_path()
        if not exe.is_file():
            self._set("error", "The bundled local runtime is missing. Reinstall Codex Engine; source builds must run prepare:local-runtime first.")
            return
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._token = secrets.token_urlsafe(32)
        key_file = self.state_dir / "local-runtime.key"
        key_file.write_text(self._token, encoding="utf-8")
        try:
            key_file.chmod(0o600)
        except OSError:
            pass
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self._url = f"http://127.0.0.1:{port}"
        command = self._command(exe, self.models_folder(s) / CATALOG[s.local_model].filename, port, key_file)
        kwargs = {"stdin": subprocess.DEVNULL, "cwd": str(exe.parent)}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        try:
            with (self.log_dir / "local-ai.log").open("ab") as log:
                self._proc = subprocess.Popen(command, stdout=log, stderr=log, **kwargs)
            if sys.platform == "win32":
                self._job = _kill_with_parent_windows(self._proc)
                if not self._job:
                    raise OSError("Could not attach the local runtime to the app lifetime.")
        except OSError:
            self._stop_locked()
            self._failed_at = time.monotonic()
            self._set("error", "Could not start the bundled local runtime. Check the local AI log or reinstall Codex Engine.")
            return
        self._signature = signature
        self._ready.clear()
        self._set("starting", "Loading the local model…")
        threading.Thread(target=self._wait_ready, args=(self._proc, self._url, self._token), daemon=True).start()

    def _command(self, exe, model, port, key_file):
        return [str(exe), "--model", str(model), "--host", "127.0.0.1", "--port", str(port),
            "--ctx-size", "8192", "--parallel", "1", "--n-gpu-layers", "0", "--threads", str(min(8, os.cpu_count() or 2)),
            "--api-key-file", str(key_file), "--no-webui", "--alias", self.config().model]

    def _wait_ready(self, proc, url, token):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            with self._lock:
                if proc is not self._proc:
                    return
                if proc.poll() is not None:
                    self._failed_at = time.monotonic()
                    self._set("error", "Local model exited while loading. Check logs/local-ai.log.")
                    return
            try:
                req = urllib.request.Request(url + "/health", headers={"Authorization": "Bearer " + token})
                with opener.open(req, timeout=1) as response:
                    data = json.load(response)
                if data.get("status") == "ok":
                    with self._lock:
                        if proc is self._proc:
                            self._set("running")
                    return
            except (OSError, ValueError):
                pass
            time.sleep(0.2)
        with self._lock:
            if proc is self._proc:
                self._stop_locked()
                self._failed_at = time.monotonic()
                self._set("error", "Local model took too long to load. Check available memory and logs/local-ai.log.")

    def _stop_locked(self):
        if self._job:
            _close_job(self._job, terminate=True)
            self._job = None
        if self._proc:
            if self._proc.poll() is None:
                try:
                    if sys.platform == "win32":
                        self._proc.terminate()
                    else:
                        os.killpg(self._proc.pid, signal.SIGTERM)
                    self._proc.wait(5)
                except (OSError, subprocess.TimeoutExpired):
                    self._proc.kill()
                    self._proc.wait(5)
            self._proc = None
        self._ready.set()
        self._token = ""
        try:
            (self.state_dir / "local-runtime.key").unlink(missing_ok=True)
        except OSError:
            pass

    def stop(self):
        self._download_cancel.set()
        with self._lock:
            self._stop_locked()
            self._set("off")

    def pull(self, model):
        if model not in CATALOG:
            raise ValueError("Choose a supported local model from Settings.")
        with self._lock:
            if self._pull and not self._pull.done:
                raise RuntimeError("A model download is already running.")
            if model in self.installed_models():
                self._pull = Download(model, status="done", completed=CATALOG[model].size, total=CATALOG[model].size, done=True)
                self.publish("model_pull", dataclasses.asdict(self._pull))
                return self._pull
            self._download_cancel.clear()
            self._pull = Download(model, total=CATALOG[model].size)
            pull = self._pull
            folder = self.models_folder()
        threading.Thread(target=self._download, args=(pull, folder), daemon=True).start()
        return pull

    def _download(self, pull, folder):
        asset = CATALOG[pull.model]
        partial = folder / (asset.filename + ".part")
        def event():
            self.publish("model_pull", dataclasses.asdict(pull))
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(folder).free < asset.size + 64 * 1024 * 1024:
                raise OSError("Not enough free space. Choose a model folder with at least 1.2 GB free.")
            pull.status = "downloading"
            event()
            digest = hashlib.sha256()
            req = urllib.request.Request(asset.url, headers={"User-Agent": "CodexEngine-model-download"})
            last = 0.0
            with urllib.request.urlopen(req, timeout=30) as response, partial.open("wb") as output:
                while True:
                    if self._download_cancel.is_set():
                        raise RuntimeError("Model download cancelled. You can download it again later.")
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    pull.completed += len(block)
                    if pull.completed > asset.size:
                        raise ValueError("Model download was larger than expected.")
                    digest.update(block)
                    output.write(block)
                    if time.monotonic() - last >= 0.25:
                        event()
                        last = time.monotonic()
            pull.status = "verifying"
            event()
            if pull.completed != asset.size or digest.hexdigest() != asset.sha256:
                raise ValueError("Model verification failed. No model was installed; try downloading again.")
            if self._download_cancel.is_set():
                raise RuntimeError("Model download cancelled.")
            destination = folder / asset.filename
            os.replace(partial, destination)
            stat = destination.stat()
            destination.with_suffix(".verified.json").write_text(json.dumps({"sha256": asset.sha256,
                "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}), encoding="utf-8")
            pull.status = "done"
        except (OSError, ValueError, RuntimeError) as exc:
            pull.error = str(exc)
            pull.status = "failed"
        finally:
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                if not pull.error:
                    pull.error = "Could not remove the partial model download. Check the model folder’s permissions."
                    pull.status = "failed"
            pull.done = True
            event()
        if not pull.error and folder == self.models_folder():
            self.ensure_running()
