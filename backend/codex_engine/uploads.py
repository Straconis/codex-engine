from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def store_upload(upload_dir: Path, filename: str, source) -> Path:
    """Save an uploaded PDF without clobbering a different file of the same name.

    Previously two different books both named e.g. "Core Rules.pdf" would overwrite
    each other, leaving the first source pointing at the second book's file.
    """
    safe_name = Path(filename.replace("\\", "/")).name or "upload.pdf"
    temp = upload_dir / f".incoming-{uuid.uuid4().hex}.part"
    digest = hashlib.sha256()
    try:
        with temp.open("wb") as handle:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
                handle.write(block)
        new_hash = digest.hexdigest()

        stem, suffix = Path(safe_name).stem, Path(safe_name).suffix or ".pdf"
        candidate = upload_dir / safe_name
        counter = 1
        while candidate.exists():
            if candidate.is_file() and _sha256_file(candidate) == new_hash:
                temp.unlink(missing_ok=True)  # identical bytes already stored
                return candidate
            counter += 1
            candidate = upload_dir / f"{stem} ({counter}){suffix}"
        os.replace(temp, candidate)
        return candidate
    finally:
        temp.unlink(missing_ok=True)
