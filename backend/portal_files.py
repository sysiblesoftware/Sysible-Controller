"""
Shared-pool file storage backing the Webserver Portal's file-transfer
feature - lets a remote host operator logged into the portal send a
file to the controller ("uploads"), and grab a file the admin staged
for them ("downloads"), without needing the host to be agent-enrolled
or SSH-reachable. That matters because the portal is mainly used
*before* either of those exist - it's the provisioning surface, not
the post-enrollment one (that's Remote Administration / agent tasks).

One shared pool rather than per-host folders: the portal already has
only a single shared username/password (backend/portal_auth.py), with
no existing notion of "which host is this" to partition by, so a
shared pool matches the existing model instead of inventing one.

Filesystem-only by design (no DB table) - this is just a drop box, not
inventory the admin needs to query/filter on.
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STORAGE_ROOT = PROJECT_ROOT / "portal_files"
UPLOADS_DIR = STORAGE_ROOT / "uploads"      # host -> controller
DOWNLOADS_DIR = STORAGE_ROOT / "downloads"  # controller -> host

UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)


class InvalidFilename(ValueError):
    pass


def safe_filename(name: str) -> str:
    """Strip any directory components and reject anything that isn't
    a plain filename - the only thing standing between a crafted name
    (../../etc/passwd, an absolute path) and reading/writing outside
    the two pool directories."""
    candidate = Path(name or "").name

    if not candidate or candidate in (".", ".."):
        raise InvalidFilename(f"Invalid filename: {name!r}")

    return candidate


def _list_dir(directory: Path):
    entries = []

    for path in directory.iterdir():
        if not path.is_file():
            continue

        stat = path.stat()
        entries.append({
            "filename": path.name,
            "size": stat.st_size,
            "modified": stat.st_mtime,
        })

    entries.sort(key=lambda e: e["modified"], reverse=True)

    return entries


def list_uploads():
    return _list_dir(UPLOADS_DIR)


def list_downloads():
    return _list_dir(DOWNLOADS_DIR)


def _save(directory: Path, filename: str, data: bytes) -> str:
    name = safe_filename(filename)
    dest = directory / name

    # Don't silently clobber a same-named file from a different host/
    # admin - append a numeric suffix instead, same idea as a browser's
    # "file (1).ext" on a repeat download.
    if dest.exists():
        stem, suffix = Path(name).stem, Path(name).suffix
        n = 1
        while dest.exists():
            dest = directory / f"{stem} ({n}){suffix}"
            n += 1

    dest.write_bytes(data)

    return dest.name


def save_upload(filename: str, data: bytes) -> str:
    return _save(UPLOADS_DIR, filename, data)


def save_download(filename: str, data: bytes) -> str:
    return _save(DOWNLOADS_DIR, filename, data)


def _path_in(directory: Path, filename: str) -> Path:
    path = directory / safe_filename(filename)

    if not path.is_file():
        raise FileNotFoundError(filename)

    return path


def upload_path(filename: str) -> Path:
    return _path_in(UPLOADS_DIR, filename)


def download_path(filename: str) -> Path:
    return _path_in(DOWNLOADS_DIR, filename)


def delete_upload(filename: str) -> bool:
    try:
        upload_path(filename).unlink()
        return True
    except FileNotFoundError:
        return False


def delete_download(filename: str) -> bool:
    try:
        download_path(filename).unlink()
        return True
    except FileNotFoundError:
        return False
