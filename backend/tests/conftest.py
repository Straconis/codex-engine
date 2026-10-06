import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path_factory, monkeypatch):
    # Never read or write the real per-machine settings file, and never start a real
    # Ollama from tests: default every test to "connect to an external Ollama" mode.
    path = tmp_path_factory.mktemp("settings") / "settings.json"
    path.write_text('{"manage_ollama": false}', encoding="utf-8")
    monkeypatch.setenv("CODEX_ENGINE_SETTINGS", str(path))
    monkeypatch.delenv("CODEX_ENGINE_OLLAMA_URL", raising=False)
    monkeypatch.delenv("CODEX_ENGINE_OLLAMA_MODEL", raising=False)
    yield path
