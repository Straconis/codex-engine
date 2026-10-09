from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path

BLOCK_SIZE = 1024 * 1024


def file_sha256(path: Path, stop: Callable[[], None] | None = None) -> str:
    """SHA-256 of a file, read in blocks. `stop` runs before each block and may raise to abort."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(BLOCK_SIZE), b""):
            if stop:
                stop()
            digest.update(block)
    return digest.hexdigest()


def _claim(temp: Path, candidate: Path) -> bool:
    """Atomically put `temp`'s bytes at `candidate`; False if the name is already taken.

    Checking that a name is free and then saving there would let two uploads of different
    books with the same name, arriving together, overwrite each other.
    """
    try:
        os.link(temp, candidate)
        return True
    except FileExistsError:
        return False
    except OSError:
        pass  # no hard links on this drive (FAT/exFAT): create the name exclusively instead
    try:
        target = candidate.open("xb")
    except FileExistsError:
        return False
    with target, temp.open("rb") as source:
        shutil.copyfileobj(source, target, BLOCK_SIZE)
    return True


def store_upload(upload_dir: Path, filename: str, source) -> Path:
    """Save an uploaded PDF without clobbering a different file of the same name.

    Identical bytes already stored under the name (or a numbered variant) are reused.
    """
    safe_name = Path(filename.replace("\\", "/")).name or "upload.pdf"
    temp = upload_dir / f".incoming-{uuid.uuid4().hex}.part"
    digest = hashlib.sha256()
    try:
        with temp.open("wb") as handle:
            for block in iter(lambda: source.read(BLOCK_SIZE), b""):
                digest.update(block)
                handle.write(block)
        new_hash = digest.hexdigest()

        stem, suffix = Path(safe_name).stem, Path(safe_name).suffix or ".pdf"
        counter = 1
        candidate = upload_dir / safe_name
        while True:
            if candidate.is_file() and file_sha256(candidate) == new_hash:
                return candidate  # identical bytes already stored
            if not candidate.exists() and _claim(temp, candidate):
                return candidate
            if candidate.exists() and not (candidate.is_file() and file_sha256(candidate) == new_hash):
                counter += 1
                candidate = upload_dir / f"{stem} ({counter}){suffix}"
    finally:
        temp.unlink(missing_ok=True)
