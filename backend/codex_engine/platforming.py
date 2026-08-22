from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

from platformdirs import user_data_dir

APP_NAME = "Codex Engine"
APP_AUTHOR = False


def app_data_dir() -> Path:
    path = Path(user_data_dir(APP_NAME, APP_AUTHOR))
    path.mkdir(parents=True, exist_ok=True)
    return path


def database_path() -> Path:
    override = os.environ.get("CODEX_ENGINE_DB")
    if override:
        return Path(override).expanduser().resolve()
    return app_data_dir() / "codex-engine.sqlite3"


def normalize_pdf_path(path: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.exists():
        raise FileNotFoundError("File does not exist.")
    if candidate.suffix.lower() != ".pdf":
        raise ValueError("Not a PDF file.")
    return candidate.resolve()


def open_file_at_page(path: str, page: int) -> None:
    target = normalize_pdf_path(path)
    page = max(1, int(page or 1))
    uri = target.as_uri() + f"#page={page}"
    system = platform.system().lower()

    if system == "windows":
        os.startfile(uri)  # type: ignore[attr-defined]
        return
    if system == "darwin":
        subprocess.Popen(["open", uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return

    opener = os.environ.get("BROWSER") or "xdg-open"
    subprocess.Popen([opener, uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def is_windows() -> bool:
    return sys.platform.startswith("win")

