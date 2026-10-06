"""Per-machine settings and the managed Ollama lifecycle (with a fake `ollama serve`)."""
from __future__ import annotations

import json
import socket
import string
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from pydantic import ValidationError

from codex_engine import ollama_manager as om
from codex_engine.settings import AppSettings, SettingsStore

FAKE_SERVER = Path(__file__).with_name("fake_ollama_server.py")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def missing_drive() -> str | None:
    if not sys.platform.startswith("win"):
        return None
    return next((f"{d}:\\" for d in reversed(string.ascii_uppercase) if not Path(f"{d}:\\").exists()), None)


# ---- settings ---------------------------------------------------------------------------

def test_defaults_and_partial_update_persist(tmp_path):
    store = SettingsStore(tmp_path / "s.json")
    assert store.load() == AppSettings()
    store.update({"models_dir": str(tmp_path / "models"), "model": "llama3.2:1b"})
    again = SettingsStore(tmp_path / "s.json").load()
    assert again.models_dir == str(tmp_path / "models") and again.model == "llama3.2:1b"
    assert again.manage_ollama is True  # untouched keys keep their values


@pytest.mark.parametrize(
    "changes",
    [
        {"model": "rm -rf /"},
        {"models_dir": "relative/models"},
        {"ollama_path": __file__},  # exists, but isn't Ollama
        {"external_url": "not a url"},
        {"managed_port": 80},
    ],
)
def test_invalid_settings_are_rejected_and_not_saved(tmp_path, changes):
    store = SettingsStore(tmp_path / "s.json")
    with pytest.raises(ValidationError):
        store.update(changes)
    assert not (tmp_path / "s.json").exists()


def test_corrupt_or_partly_invalid_file_falls_back_per_key(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json", encoding="utf-8")
    assert SettingsStore(path).load() == AppSettings()
    # e.g. settings copied from another machine whose Ollama path doesn't exist here
    path.write_text(json.dumps({"model": "gemma3:1b", "ollama_path": "Z:/nope/ollama.exe", "junk": 1}), encoding="utf-8")
    loaded = SettingsStore(path).load()
    assert loaded.model == "gemma3:1b" and loaded.ollama_path == ""


# ---- manager: non-running states ------------------------------------------------------------

def make_manager(tmp_path, cls=om.OllamaManager, events=None, **settings) -> om.OllamaManager:
    store = SettingsStore(tmp_path / "s.json")
    store.update(settings)
    publish = (lambda name, payload: events.append((name, payload))) if events is not None else (lambda *a: None)
    return cls(store, state_dir=tmp_path / "state", log_dir=tmp_path / "logs", publish=publish)


def test_disabled_ai_is_off_not_an_error(tmp_path):
    m = make_manager(tmp_path, ai_enabled=False)
    m.ensure_running()
    status = m.status()
    assert status["state"] == "off" and status["available"] is False and "turned off" in status["error"]


def test_external_mode_uses_configured_url(tmp_path):
    m = make_manager(tmp_path, manage_ollama=False, external_url="http://192.168.1.20:11434")
    m.ensure_running()  # never starts anything
    assert m.status()["mode"] == "external" and m.config().url == "http://192.168.1.20:11434"


def test_env_url_overrides_for_development(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_ENGINE_OLLAMA_URL", "http://127.0.0.1:11500")
    m = make_manager(tmp_path, manage_ollama=True)
    assert m.status()["mode"] == "external" and m.config().url == "http://127.0.0.1:11500"


def test_model_comes_from_settings(tmp_path):
    assert make_manager(tmp_path, model="gemma3:1b").config().model == "gemma3:1b"


def test_ollama_not_installed_is_explained(tmp_path, monkeypatch):
    monkeypatch.setattr(om, "find_ollama", lambda configured="": None)
    m = make_manager(tmp_path, manage_ollama=True)
    m.ensure_running()
    status = m.status()
    assert status["state"] == "not_installed" and "ollama.com" in status["error"]


@pytest.mark.skipif(missing_drive() is None, reason="needs an unused Windows drive letter")
def test_unplugged_companion_drive_is_explained(tmp_path, monkeypatch):
    monkeypatch.setattr(om, "find_ollama", lambda configured="": Path(sys.executable))
    store = SettingsStore(tmp_path / "s.json")
    store.path.write_text(json.dumps({"manage_ollama": True, "models_dir": missing_drive() + "ollama\\models"}))
    m = om.OllamaManager(store, state_dir=tmp_path / "state", log_dir=tmp_path / "logs")
    m.ensure_running()
    assert m.status()["state"] == "error" and "drive connected" in m.status()["error"]


# ---- manager: managed lifecycle with a fake `ollama serve` -------------------------------------

class FakeServeManager(om.OllamaManager):
    extra_args: list[str] = []

    def _command(self, exe):
        return [sys.executable, str(FAKE_SERVER), *self.extra_args]


@pytest.fixture()
def managed(tmp_path, monkeypatch):
    monkeypatch.setattr(om, "find_ollama", lambda configured="": Path(sys.executable))
    created: list[om.OllamaManager] = []

    def factory(events=None, cls=FakeServeManager, **settings):
        settings = {"manage_ollama": True, "managed_port": free_port(), "models_dir": str(tmp_path / "models"), **settings}
        m = make_manager(tmp_path, cls=cls, events=events, **settings)
        created.append(m)
        return m

    yield factory
    for m in created:
        m.stop()


def get_json(url):
    with urllib.request.urlopen(url, timeout=2) as resp:
        return json.loads(resp.read())


def test_starts_with_configured_folder_and_port_then_stops(managed, tmp_path):
    m = managed()
    m.ensure_running()
    status = m.status()
    assert status["state"] == "running" and status["available"] is False  # running, no models yet
    assert "Download one" in status["error"]
    info = get_json(m.config().url + "/api/version")
    assert info["models_dir"] == str(tmp_path / "models")  # OLLAMA_MODELS passed through
    assert (tmp_path / "state" / "ollama-managed.json").exists()

    m.stop()
    assert not om.port_in_use(int(m.config().url.rsplit(":", 1)[1]))
    assert not (tmp_path / "state" / "ollama-managed.json").exists()


def test_changing_the_folder_restarts_ollama_there(managed, tmp_path):
    m = managed()
    m.ensure_running()
    m.store.update({"models_dir": str(tmp_path / "companion" / "models")})
    m.apply(start=False)
    m.ensure_running()
    assert get_json(m.config().url + "/api/version")["models_dir"] == str(tmp_path / "companion" / "models")


def test_port_used_by_another_program_is_explained(managed):
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        m = managed(managed_port=blocker.getsockname()[1])
        m.ensure_running()
        status = m.status()
    assert status["state"] == "error" and "already used by another program" in status["error"]


def test_leftover_from_a_crashed_run_is_cleaned_up(managed):
    first = managed()
    first.ensure_running()
    old_pid = get_json(first.config().url + "/api/version")["pid"]
    first._proc = None  # simulate the backend dying without stopping its Ollama
    first._job = None

    second = managed(managed_port=int(first.config().url.rsplit(":", 1)[1]))
    second.ensure_running()
    assert second.status()["state"] == "running"
    assert get_json(second.config().url + "/api/version")["pid"] != old_pid


def test_ollama_crashing_at_startup_is_an_error_not_a_hang(managed):
    class Crashing(FakeServeManager):
        extra_args = ["--exit-immediately"]

    m = managed(cls=Crashing)
    started = time.monotonic()
    m.ensure_running()
    assert m.status()["state"] == "error" and "exited during startup" in m.status()["error"]
    assert time.monotonic() - started < 10


def test_pull_downloads_into_the_folder_with_progress(managed, tmp_path):
    events: list = []
    m = managed(events=events)
    m.ensure_running()
    m.pull("qwen2.5:1.5b")
    for _ in range(40):
        if m.status()["pull"]["done"]:
            break
        time.sleep(0.1)
    status = m.status()
    assert status["pull"]["status"] == "done" and status["pull"]["error"] is None
    assert status["available"] is True and status["model"] == "qwen2.5:1.5b"
    assert (tmp_path / "models" / "pulled.txt").read_text().split() == ["qwen2.5:1.5b"]
    assert any(name == "model_pull" and p["done"] for name, p in events)


def test_failed_pull_reports_the_error(managed):
    m = managed()
    m.ensure_running()
    m.pull("missing:1b")
    for _ in range(40):
        if m.status()["pull"]["done"]:
            break
        time.sleep(0.1)
    pull = m.status()["pull"]
    assert pull["status"] == "failed" and "does not exist" in pull["error"]


def test_only_one_pull_at_a_time_and_names_validated(managed):
    m = managed()
    with pytest.raises(ValidationError):
        m.pull("bad name; rm")
    m._pull = om.PullState(model="x:1b")  # one in flight
    with pytest.raises(RuntimeError, match="Already downloading"):
        m.pull("qwen2.5:1.5b")


# ---- API -----------------------------------------------------------------------------------

@pytest.fixture()
def api(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    monkeypatch.setenv("CODEX_ENGINE_DB", str(tmp_path / "api.sqlite3"))
    from fastapi.testclient import TestClient

    from codex_engine import app as app_module

    app_module._schema_ready = False
    app_module.ollama.apply(start=False)
    return TestClient(app_module.app)


def test_settings_api_roundtrip_and_validation(api, tmp_path):
    headers = {"X-Codex-Engine-Client": "1"}
    body = api.get("/api/settings").json()
    assert body["settings"]["manage_ollama"] is False and body["path"].endswith("settings.json")

    r = api.put("/api/settings", json={"models_dir": str(tmp_path / "m"), "model": "gemma3:1b"}, headers=headers)
    assert r.status_code == 200 and r.json()["settings"]["model"] == "gemma3:1b"

    r = api.put("/api/settings", json={"models_dir": "relative"}, headers=headers)
    assert r.status_code == 422 and "full path" in r.json()["detail"]
    assert api.put("/api/settings", json={"model": "x"}).status_code == 403  # CSRF header still required


def test_status_api_reports_mode(api):
    status = api.get("/api/ai/status").json()
    assert status["mode"] == "external" and "available" in status


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Job Objects are Windows-only")
def test_managed_ollama_dies_when_the_backend_is_killed(tmp_path):
    """A crashed/killed backend must not leave Ollama running (Windows Job Object)."""
    import subprocess

    port = free_port()
    script = tmp_path / "backend_stub.py"
    script.write_text(
        f"""
import sys, time
from pathlib import Path
sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
from codex_engine import ollama_manager as om
from codex_engine.settings import SettingsStore
om.find_ollama = lambda configured="": Path(sys.executable)
class M(om.OllamaManager):
    def _command(self, exe):
        return [sys.executable, {str(FAKE_SERVER)!r}]
store = SettingsStore(Path({str(tmp_path / 's.json')!r}))
store.update({{"manage_ollama": True, "managed_port": {port}}})
m = M(store, state_dir=Path({str(tmp_path / 'state')!r}), log_dir=Path({str(tmp_path / 'logs')!r}))
m.ensure_running()
print(m.status()["state"], flush=True)
time.sleep(60)
""",
        encoding="utf-8",
    )
    backend = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True)
    try:
        assert backend.stdout.readline().strip() == "running"
        assert om.port_in_use(port)
        backend.kill()  # TerminateProcess: no cleanup code runs, like a crash
        backend.wait(5)
        for _ in range(40):
            if not om.port_in_use(port):
                break
            time.sleep(0.25)
        assert not om.port_in_use(port), "managed Ollama outlived the backend"
    finally:
        if backend.poll() is None:
            backend.kill()


def process_alive(pid: int) -> bool:
    if sys.platform.startswith("win"):
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32")
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    import os

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def windows_children(pid: int) -> dict[int, str]:
    import subprocess

    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"Get-CimInstance Win32_Process -Filter 'ParentProcessId={pid}' | ForEach-Object {{ \"$($_.ProcessId) $($_.Name)\" }}"],
        capture_output=True, text=True, timeout=30,
    ).stdout
    return {int(line.split()[0]): line.split()[1].lower() for line in out.splitlines() if line.strip()}


def test_stop_also_stops_model_runner_processes(managed):
    """Ollama keeps models loaded in runner subprocesses; stopping it must stop those too."""
    if sys.platform.startswith("win"):
        # A native parent with a native child, like ollama.exe and its runners. (The venv's
        # python.exe is only a launcher for a Store-packaged Python that Windows runs outside
        # our Job Object, which would make this test about the launcher, not the manager.)
        class WithRunner(FakeServeManager):
            def _command(self, exe):
                return f'cmd /c "start "" /b ping -n 120 127.0.0.1 >nul & "{sys.executable}" "{FAKE_SERVER}""'

        m = managed(cls=WithRunner)
        m.ensure_running()
        assert m.status()["state"] == "running"
        runners = [pid for pid, name in windows_children(m._proc.pid).items() if name == "ping.exe"]
        assert runners, "test setup: runner didn't start"
        runner = runners[0]
    else:
        class WithRunner(FakeServeManager):
            extra_args = ["--spawn-runner"]

        m = managed(cls=WithRunner)
        m.ensure_running()
        runner = get_json(m.config().url + "/api/version")["runner_pid"]

    assert process_alive(runner)
    m.stop()
    for _ in range(20):
        if not process_alive(runner):
            break
        time.sleep(0.25)
    assert not process_alive(runner), "model runner outlived Ollama"


def test_saving_settings_applies_before_the_response_returns(api):
    # A download started right after Save must never reach the old Ollama mid-restart.
    headers = {"X-Codex-Engine-Client": "1"}
    assert api.put("/api/settings", json={"ai_enabled": False}, headers=headers).status_code == 200
    assert api.get("/api/ai/status").json()["state"] == "off"
    assert api.put("/api/settings", json={"ai_enabled": True}, headers=headers).status_code == 200
    assert api.get("/api/ai/status").json()["state"] == "external"
