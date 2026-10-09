"""Per-machine app settings, stored as JSON in the app data folder.

Settings live with the machine, not the library: one computer can keep its AI models
on the system drive, another on a companion/external drive.
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator

from .platforming import app_data_dir

MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")


class AppSettings(BaseModel):
    ai_enabled: bool = True
    # True: Codex Engine starts and stops its own Ollama (with the folder/port below).
    # False: connect to an Ollama the user runs themselves at external_url.
    manage_ollama: bool = True
    ollama_path: str = ""  # Ollama executable; "" = auto-detect
    models_dir: str = ""  # where Ollama stores models; "" = Ollama's own default
    managed_port: int = Field(default=11435, ge=1024, le=65535)  # not 11434, so it never clashes with a user's Ollama
    external_url: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5:1.5b"

    @field_validator("model")
    @classmethod
    def _model_name(cls, value: str) -> str:
        value = value.strip()
        if not MODEL_NAME_RE.match(value):
            raise ValueError("Model names look like 'qwen2.5:1.5b'.")
        return value

    @field_validator("models_dir", "ollama_path")
    @classmethod
    def _path(cls, value: str) -> str:
        value = value.strip().strip('"')
        if value and not Path(value).expanduser().is_absolute():
            raise ValueError("Use a full path, e.g. D:\\ollama\\models.")
        return value

    @field_validator("ollama_path")
    @classmethod
    def _ollama_exe(cls, value: str) -> str:
        # This path is executed, so only accept something that is actually Ollama.
        if value and (not Path(value).expanduser().is_file() or not Path(value).name.lower().startswith("ollama")):
            raise ValueError("That isn't the Ollama program (expected a file named ollama or ollama.exe).")
        return value

    @field_validator("external_url")
    @classmethod
    def _url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not re.match(r"^https?://[^\s/]+(:\d+)?$", value):
            raise ValueError("Use an address like http://127.0.0.1:11434.")
        return value


def settings_path() -> Path:
    override = os.environ.get("CODEX_ENGINE_SETTINGS")
    return Path(override).expanduser() if override else app_data_dir() / "settings.json"


class SettingsStore:
    def __init__(self, path: Path | None = None):
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path or settings_path()

    def _stored(self) -> dict:
        """The settings file as saved (known keys only), whether or not each value is valid now."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {key: value for key, value in raw.items() if key in AppSettings.model_fields}

    def load(self) -> AppSettings:
        """Saved settings; anything missing or invalid falls back to its default."""
        valid: dict = {}
        for key, value in self._stored().items():
            try:
                AppSettings(**{key: value})
            except ValidationError:
                continue  # e.g. an Ollama path from a drive that's no longer there
            valid[key] = value
        return AppSettings(**valid)

    def update(self, changes: dict) -> AppSettings:
        """Validate and persist a partial update. Raises ValidationError on bad input.

        Only the changed keys are validated and rewritten. A saved value that is invalid
        right now (an Ollama path on an unplugged drive) is kept for when it works again,
        instead of being erased by an unrelated change.
        """
        with self._lock:
            checked = AppSettings(**changes)
            stored = self._stored()
            stored.update({key: getattr(checked, key) for key in changes if key in AppSettings.model_fields})
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(stored, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
            return self.load()
