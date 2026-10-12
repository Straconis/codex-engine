"""Package a pinned portable runtime; end users install only Codex Engine."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import os
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from codex_engine.local_catalog import RUNTIME_TAG, RUNTIME_URL, RUNTIME_SHA256


CRT_FILES = ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll")


def stage_crt(output):
    # Use licensed Visual Studio redistributable files, not arbitrary DLL downloads.
    override = os.environ.get("CODEX_ENGINE_VC_REDIST_DIR")
    candidates = [Path(override)] if override else []
    if not override:
        for base in (os.environ.get("ProgramFiles", "C:/Program Files"), os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")):
            vs = Path(base) / "Microsoft Visual Studio"
            candidates.extend(vs.glob("*/*/VC/Redist/MSVC/*/x64/Microsoft.VC*.CRT"))
    candidates = [p for p in candidates if all((p / name).is_file() for name in CRT_FILES)]
    if not candidates:
        raise RuntimeError("Build host needs Visual Studio C++ redistributable files. Set CODEX_ENGINE_VC_REDIST_DIR to the licensed x64 Microsoft.VC*.CRT folder. End users do not install it separately.")
    source = sorted(candidates, reverse=True)[0]
    files = {}
    for name in CRT_FILES:
        path = source / name
        # Authenticode validation is provided by Windows, including its certificate chain.
        command = "$ErrorActionPreference = 'Stop'; $s = Get-AuthenticodeSignature -LiteralPath '" + str(path).replace("'", "''") + "'; if ($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') { exit 1 }; (Get-Item -LiteralPath '" + str(path).replace("'", "''") + "').VersionInfo.FileMajorPart"
        shell_env = dict(os.environ)
        shell_env.pop("PSModulePath", None)  # Do not feed PowerShell 7 module paths to Windows PowerShell.
        result = subprocess.run([shutil.which("pwsh.exe") or "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True, env=shell_env, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode != 0 or result.stdout.strip() != "14":
            raise RuntimeError("Microsoft runtime signature/version verification failed for " + name)
        data = path.read_bytes()
        (output / name).write_bytes(data)
        files[name] = hashlib.sha256(data).hexdigest()
    return files


def prepare(root=ROOT):
    if sys.platform != "win32":
        raise SystemExit("This package prepares the Windows x64 CPU runtime.")
    output = root / "resources" / "llama"
    output.mkdir(parents=True, exist_ok=True)
    manifest = output / "runtime.json"
    if manifest.exists():
        saved = json.loads(manifest.read_text(encoding="utf-8"))
        approved = set(saved.get("files", {})) | set(CRT_FILES)
        unexpected = [p.name for p in output.iterdir() if p.suffix.lower() in (".dll", ".exe") and p.name not in approved]
        if unexpected:
            raise RuntimeError("Unexpected executable files in the runtime folder; refusing to package: " + ", ".join(unexpected))
        if saved.get("archive_sha256") == RUNTIME_SHA256 and saved.get("files") and all(
            (output / name).is_file() and hashlib.sha256((output / name).read_bytes()).hexdigest() == digest
            for name, digest in saved["files"].items()
        ):
            saved["files"].update(stage_crt(output))
            manifest.write_text(json.dumps(saved, indent=2), encoding="utf-8")
            print(f"Verified bundled llama.cpp {RUNTIME_TAG} and app-local Microsoft runtime already prepared.")
            return
    with tempfile.TemporaryDirectory(prefix="codex-runtime-") as scratch:
        archive = Path(scratch) / "runtime.zip"
        digest = hashlib.sha256()
        req = urllib.request.Request(RUNTIME_URL, headers={"User-Agent": "CodexEngine-build"})
        with urllib.request.urlopen(req, timeout=60) as response, archive.open("wb") as stream:
            total = 0
            while block := response.read(1024 * 1024):
                total += len(block)
                if total > 100 * 1024 * 1024:
                    raise RuntimeError("Runtime archive exceeded the expected size limit.")
                stream.write(block)
                digest.update(block)
        if digest.hexdigest() != RUNTIME_SHA256:
            raise RuntimeError("Runtime archive checksum mismatch; nothing will be packaged.")
        files = {}
        with zipfile.ZipFile(archive) as zipped:
            for item in zipped.infolist():
                name = PurePosixPath(item.filename.replace(chr(92), "/"))
                if name.is_absolute() or ".." in name.parts or ":" in str(name):
                    raise RuntimeError("Unsafe runtime archive path.")
                if item.is_dir():
                    continue
                # The runtime distribution has a flat executable/DLL layout.
                if len(name.parts) != 1 or item.file_size > 100 * 1024 * 1024:
                    raise RuntimeError("Unexpected runtime archive layout or size.")
                data = zipped.read(item)
                target = output / name.name
                target.write_bytes(data)
                files[name.name] = hashlib.sha256(data).hexdigest()
        if "llama-server.exe" not in files:
            raise RuntimeError("The verified distribution does not contain llama-server.exe.")
        # Bundle the upstream license with the binary for redistribution.
        license_url = "https://raw.githubusercontent.com/ggml-org/llama.cpp/" + RUNTIME_TAG + "/LICENSE"
        with urllib.request.urlopen(license_url, timeout=30) as response:
            (output / "LICENSE-llama.cpp.txt").write_bytes(response.read())
        files.update(stage_crt(output))
        manifest.write_text(json.dumps({"version": RUNTIME_TAG, "archive_sha256": RUNTIME_SHA256,
            "source": RUNTIME_URL, "files": files}, indent=2), encoding="utf-8")
        print(f"Prepared verified llama.cpp {RUNTIME_TAG}: {len(files)} runtime files.")


if __name__ == "__main__":
    prepare()
