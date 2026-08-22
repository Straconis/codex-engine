from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from codex_engine.config import APP_VERSION, GITHUB_OWNER, GITHUB_REPO

LATEST_RELEASE_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"

PLATFORM_ASSETS = {
    "win32": ("windows", "CodexEngineSetup-", ".exe"),
    "linux": ("linux", "CodexEngine-", ".AppImage"),
    "darwin": ("macos", "CodexEngine-", ".dmg"),
}


def current_platform() -> tuple[str, str, str]:
    return PLATFORM_ASSETS.get(sys.platform, (sys.platform, "", ""))


def normalize_version(version: str) -> tuple[int, int, int]:
    version = version.strip()
    if version.lower().startswith("v"):
        version = version[1:]
    parts = version.split(".")
    numbers: list[int] = []
    for part in parts:
        try:
            numbers.append(int("".join(ch for ch in part if ch.isdigit()) or "0"))
        except ValueError:
            numbers.append(0)
    while len(numbers) < 3:
        numbers.append(0)
    return tuple(numbers[:3])


def get_latest_release() -> dict:
    request = urllib.request.Request(
        LATEST_RELEASE_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Codex-Engine-Updater",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {"status": "no_release", "message": "No public latest GitHub release was found."}
        raise RuntimeError(f"GitHub returned HTTP {error.code}.") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Could not connect to GitHub: {error.reason}") from error
    except TimeoutError as error:
        raise RuntimeError("The GitHub update check timed out.") from error
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise RuntimeError("GitHub returned invalid release information.") from error


def find_installer_asset(release: dict, prefix: str, suffix: str) -> dict | None:
    if not prefix or not suffix:
        return None
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if name.startswith(prefix) and name.endswith(suffix):
            return asset
    return None


def check_for_update(current_version: str = APP_VERSION) -> dict:
    platform_name, installer_prefix, installer_suffix = current_platform()
    release = get_latest_release()
    if release.get("status") == "no_release":
        return {
            "status": "no_release",
            "current_version": current_version,
            "latest_version": current_version,
            "release_url": f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases",
            "platform": platform_name,
            "message": release.get("message"),
        }

    latest_version = release.get("tag_name", "")
    if not latest_version:
        raise RuntimeError("The latest GitHub release does not contain a version tag.")

    if normalize_version(latest_version) <= normalize_version(current_version):
        return {
            "status": "current",
            "current_version": current_version,
            "latest_version": latest_version,
            "release_url": release.get("html_url"),
            "platform": platform_name,
        }

    asset = find_installer_asset(release, installer_prefix, installer_suffix)
    if not asset or not asset.get("browser_download_url"):
        return {
            "status": "missing_installer",
            "current_version": current_version,
            "latest_version": latest_version,
            "release_url": release.get("html_url"),
            "platform": platform_name,
            "expected_asset": f"{installer_prefix}*{installer_suffix}" if installer_prefix and installer_suffix else None,
        }

    return {
        "status": "update_available",
        "current_version": current_version,
        "latest_version": latest_version,
        "release_url": release.get("html_url"),
        "platform": platform_name,
        "installer_name": asset.get("name"),
        "installer_url": asset.get("browser_download_url"),
    }


def download_installer(update: dict) -> str:
    installer_name = update.get("installer_name")
    installer_url = update.get("installer_url")
    if not installer_name or not installer_url:
        raise RuntimeError("No update installer is available for this release.")

    download_dir = Path(tempfile.gettempdir()) / "CodexEngineUpdate"
    download_dir.mkdir(parents=True, exist_ok=True)
    installer_path = download_dir / installer_name

    request = urllib.request.Request(installer_url, headers={"User-Agent": "Codex-Engine-Updater"})
    with urllib.request.urlopen(request, timeout=120) as response:
        with installer_path.open("wb") as output_file:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output_file.write(chunk)

    return str(installer_path)


def launch_updater(installer_path: str) -> None:
    if sys.platform != "win32":
        raise RuntimeError("Automatic installer updates are currently implemented for Windows packages only.")

    updater_path = os.environ.get("CODEX_ENGINE_UPDATER_EXE")
    app_exe = os.environ.get("CODEX_ENGINE_APP_EXE")

    if not updater_path:
        current_dir = Path(sys.executable).resolve().parent
        updater_path = str(current_dir.parent / "updater" / "codex-engine-updater.exe")
    if not app_exe:
        app_exe = str(Path(sys.executable).resolve().parent.parent / "Codex Engine.exe")

    if not Path(updater_path).is_file():
        raise RuntimeError(f"Codex Engine updater could not be found: {updater_path}")
    if not Path(app_exe).is_file():
        raise RuntimeError(f"Codex Engine executable could not be found: {app_exe}")

    subprocess.Popen([updater_path, installer_path, app_exe], cwd=str(Path(updater_path).parent))
