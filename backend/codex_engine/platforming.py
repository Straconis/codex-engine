from __future__ import annotations

import ntpath
import os
import platform
import subprocess
from pathlib import Path

from platformdirs import user_data_dir

APP_NAME = "Codex Engine"
# No author folder: data lives in %LOCALAPPDATA%\Codex Engine, not %LOCALAPPDATA%\<author>\Codex Engine.
NO_APP_AUTHOR = False


def app_data_dir() -> Path:
    path = Path(user_data_dir(APP_NAME, NO_APP_AUTHOR))
    path.mkdir(parents=True, exist_ok=True)
    return path


def database_path() -> Path:
    override = os.environ.get("CODEX_ENGINE_DB")
    if override:
        return Path(override).expanduser().resolve()
    return app_data_dir() / "codex-engine.sqlite3"


def normalize_pdf_path(path: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_file():
        raise FileNotFoundError("File does not exist.")
    if candidate.suffix.lower() != ".pdf":
        raise ValueError("Not a PDF file.")
    return candidate.resolve()


def default_pdf_viewer_windows() -> str | None:
    """Full path of the program Windows opens .pdf files with (None if unknown)."""
    import ctypes
    from ctypes import wintypes

    shlwapi = ctypes.WinDLL("shlwapi")
    fn = shlwapi.AssocQueryStringW
    fn.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    size = wintypes.DWORD(1024)
    buf = ctypes.create_unicode_buffer(size.value)
    # ASSOCF_NOTRUNCATE = 0x20, ASSOCSTR_EXECUTABLE = 2
    if fn(0x20, 2, ".pdf", "open", buf, ctypes.byref(size)) != 0 or not buf.value:
        return None
    return buf.value


def viewer_command(viewer: str, target: Path, page: int) -> list[str] | None:
    """Command that opens `target` at `page` in `viewer`, or None if we don't know how.

    Windows drops "#page=N" when it hands a file to the default app, so each viewer gets
    the page in its own command-line syntax.
    """
    name = ntpath.basename(viewer).lower()  # Windows path, read the same way on any OS
    path = str(target)
    if name in ("acrobat.exe", "acrord32.exe", "acrord64.exe", "pdfxedit.exe", "pdfxcview.exe"):
        return [viewer, "/A", f"page={page}", path]
    if name.startswith("foxit"):
        return [viewer, path, "/A", f"page={page}"]
    if name.startswith("sumatrapdf"):
        return [viewer, "-page", str(page), path]
    if name in ("msedge.exe", "chrome.exe", "firefox.exe", "brave.exe", "opera.exe"):
        return [viewer, target.as_uri() + f"#page={page}"]
    return None


def open_file_at_page(path: str, page: int) -> None:
    target = normalize_pdf_path(path)
    page = max(1, int(page or 1))
    uri = target.as_uri() + f"#page={page}"
    system = platform.system().lower()

    if system == "windows":
        viewer = default_pdf_viewer_windows()
        command = viewer_command(viewer, target, page) if viewer else None
        if command:
            subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            os.startfile(str(target))  # type: ignore[attr-defined]  # unknown viewer: at least open the file
        return
    if system == "darwin":
        subprocess.Popen(["open", uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return

    opener = os.environ.get("BROWSER") or "xdg-open"
    subprocess.Popen([opener, uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


