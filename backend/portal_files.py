"""Shared file-drop storage for the Webserver Portal - a pool of
uploads (from remote host operators, via backend/portal_app.py's
/files page) and downloads (staged by an admin, for those operators to
retrieve) that live on disk under portal_files/, independent of any
particular host or agent.

Kept deliberately simple (flat directories, no per-user namespacing)
since the portal has exactly one shared login (backend/db.py's
portal_credentials, singular) - there's no concept of "whose" upload a
file is once it lands here.
"""

import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PORTAL_FILES_DIR = BASE_DIR / "portal_files"
UPLOADS_DIR = PORTAL_FILES_DIR / "uploads"
DOWNLOADS_DIR = PORTAL_FILES_DIR / "downloads"

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._ \-]")


def ensure_dirs():
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)


def safe_filename(name: str) -> str:
    """Strips any path component and disallowed characters, guarding
    against path traversal (e.g. '../../etc/passwd') and odd
    characters that could confuse a shell or filesystem. Collapses to
    'file' if nothing usable remains."""
    name = os.path.basename(name or "")
    name = _UNSAFE_CHARS.sub("_", name).strip()

    return name or "file"


def _unique_path(directory: Path, filename: str) -> Path:
    """Auto-renames on collision by appending a numeric suffix
    (file.txt -> file (1).txt) rather than overwriting an existing
    file silently."""
    candidate = directory / filename

    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    n = 1

    while True:
        candidate = directory / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def save_upload(filename: str, data: bytes) -> str:
    ensure_dirs()

    safe_name = safe_filename(filename)
    dest = _unique_path(UPLOADS_DIR, safe_name)

    dest.write_bytes(data)

    return dest.name


def save_download(filename: str, data: bytes) -> str:
    ensure_dirs()

    safe_name = safe_filename(filename)
    dest = _unique_path(DOWNLOADS_DIR, safe_name)

    dest.write_bytes(data)

    return dest.name


def list_uploads():
    ensure_dirs()

    return sorted(
        (p.name, p.stat().st_size, p.stat().st_mtime)
        for p in UPLOADS_DIR.iterdir() if p.is_file()
    )


def list_downloads():
    ensure_dirs()

    return sorted(
        (p.name, p.stat().st_size, p.stat().st_mtime)
        for p in DOWNLOADS_DIR.iterdir() if p.is_file()
    )


def get_upload_path(filename: str) -> Path:
    return UPLOADS_DIR / safe_filename(filename)


def get_download_path(filename: str) -> Path:
    return DOWNLOADS_DIR / safe_filename(filename)


def delete_upload(filename: str) -> bool:
    path = get_upload_path(filename)

    if path.exists() and path.is_file():
        path.unlink()
        return True

    return False


def delete_download(filename: str) -> bool:
    path = get_download_path(filename)

    if path.exists() and path.is_file():
        path.unlink()
        return True

    return False
