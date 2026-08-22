from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

WAIT_SECONDS = 1
MAX_WAIT_SECONDS = 45
TEMP_MODE_ARGUMENT = "--temp-updater"


def show_message(message: str, title: str = "Codex Engine Updater", error: bool = False) -> None:
    icon = 0x10 if error else 0x40
    ctypes.windll.user32.MessageBoxW(None, message, title, icon)


def is_process_running(exe_path: str) -> bool:
    exe_name = Path(exe_path).name.lower()
    result = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return exe_name in result.stdout.lower()


def wait_for_app_to_close(app_exe: str) -> bool:
    waited = 0
    while is_process_running(app_exe):
        if waited >= MAX_WAIT_SECONDS:
            return False
        time.sleep(WAIT_SECONDS)
        waited += WAIT_SECONDS
    return True


def run_installer(installer_path: str) -> int:
    result = subprocess.run(
        [installer_path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
        check=False,
    )
    return result.returncode


def relaunch_app(app_exe: str) -> None:
    subprocess.Popen([app_exe], cwd=str(Path(app_exe).parent))


def launch_temporary_updater(installer_path: str, app_exe: str) -> None:
    current_exe = Path(sys.executable).resolve()
    temp_dir = Path(tempfile.gettempdir()) / "CodexEngineUpdater"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_updater = temp_dir / f"CodexEngineUpdater-{uuid.uuid4().hex}.exe"
    shutil.copy2(current_exe, temp_updater)
    subprocess.Popen([str(temp_updater), TEMP_MODE_ARGUMENT, installer_path, app_exe], cwd=str(temp_dir))


def perform_update(installer_path: str, app_exe: str) -> int:
    if not Path(installer_path).is_file():
        show_message(f"Update installer not found:\n\n{installer_path}", error=True)
        return 2
    if not wait_for_app_to_close(app_exe):
        show_message("Codex Engine did not close within 45 seconds.\n\nThe update has been cancelled.", error=True)
        return 1

    installer_result = run_installer(installer_path)
    if installer_result != 0:
        show_message(f"The Codex Engine update installer failed.\n\nInstaller exit code: {installer_result}", error=True)
        return installer_result

    if not Path(app_exe).is_file():
        show_message("The update completed, but Codex Engine could not be found afterward.", error=True)
        return 2

    try:
        relaunch_app(app_exe)
    except Exception as error:
        show_message(f"The update completed, but Codex Engine could not be restarted.\n\n{error}", error=True)
        return 1

    return 0


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == TEMP_MODE_ARGUMENT:
        return perform_update(str(Path(sys.argv[2]).resolve()), str(Path(sys.argv[3]).resolve()))

    if len(sys.argv) != 3:
        show_message("The updater was started without the required update information.", error=True)
        return 1

    installer_path = str(Path(sys.argv[1]).resolve())
    app_exe = str(Path(sys.argv[2]).resolve())

    if not Path(installer_path).is_file():
        show_message(f"Update installer not found:\n\n{installer_path}", error=True)
        return 2

    try:
        launch_temporary_updater(installer_path, app_exe)
    except Exception as error:
        show_message(f"The Codex Engine updater could not prepare the update.\n\n{error}", error=True)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
