"""Controller backup/restore core — Community.

One consistent, portable snapshot of everything a controller needs to be rebuilt:

  * the SQLite database (agents, admins, tokens, activity log, approvals, …) —
    copied with SQLite's online backup API, so it is transactionally consistent
    and needs no downtime to CAPTURE;
  * the data dir  — install API key, hosts.json, per-host SSH keys (remote_keys/);
  * the cert dir  — TLS cert + private key + trust bundle (so a restored controller
    keeps the SAME identity and pinned agents keep verifying it);
  * the run dir   — the secret-vault master key + session-signing keys (WITHOUT the
    master key, the encrypted secrets in the DB are unrecoverable).

The archive is a `.tar.gz` written `0600` with a `manifest.json` (schema version,
timestamp, per-file sha256 + size, and the DB's `PRAGMA integrity_check`). Restore
validates the manifest and DB integrity before writing anything.

SECURITY: a full backup contains private keys and the vault master key — it is as
sensitive as the controller itself. Store it encrypted at rest (encrypted volume /
object store with SSE, or pipe the archive through your own gpg/age). A *sanitized*
export (the default for a migration bundle) omits that key material. See
docs/BACKUP_RESTORE.md.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path

SCHEMA_VERSION = 1

# Curated per-root file/dir list — we back up exactly what a rebuild needs, not
# whatever else happens to sit in these dirs (logs, caches, sockets).
_COMPONENTS = {
    "data": ["api_key.txt", "hosts.json", "remote_keys"],
    "cert": ["server.crt", "server.key", "server.pem", "trust.crt"],
    "run": ["controller_secret.key", "webgui.secret", "webgui_sudo.key"],
}

# Plaintext key material / bootstrap credentials. A *sanitized* export (the
# default for a migration bundle, `sysible_controller export`) omits these so the
# archive is safe to move between machines without carrying the crown-jewel keys;
# `--with-secrets` includes them for a true clone. The DB itself is always kept —
# it holds only hashes and vault-encrypted blobs, and those blobs are useless
# without the vault master key we strip here.
#   run/*                    → secret-vault master key + session-signing keys
#   cert/server.key|.pem     → TLS private key (host identity)
#   data/api_key.txt         → bootstrap/enrollment API key
#   data/remote_keys/**      → per-host SSH private keys
_SECRET_MATCH = {
    "run": lambda rel: True,
    "cert": lambda rel: rel in ("server.key", "server.pem"),
    "data": lambda rel: rel == "api_key.txt" or Path(rel).parts[:1] == ("remote_keys",),
}


def _is_secret(comp: str, rel: str) -> bool:
    m = _SECRET_MATCH.get(comp)
    return bool(m and m(rel))


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def resolve_roots(env=None, db_path=None):
    """The four state roots, resolved exactly as the running controller resolves
    them (same env vars, same defaults). Overridable for tests / DR onto a box with
    a different layout."""
    env = env if env is not None else os.environ
    root = repo_root()
    if db_path is None:
        # Import lazily so this module is usable without the app importable.
        try:
            import backend.db as _db
            db_path = Path(_db.DB_PATH)
        except Exception:
            db_path = root / "backend" / "sysible.db"
    return {
        "db": Path(db_path),
        "data": Path(env.get("SYSIBLE_DATA_DIR", "/opt/sysible")),
        "cert": Path(env.get("SYSIBLE_CERT_DIR", str(root / "certs"))),
        "run": Path(env.get("SYSIBLE_RUN_DIR", str(root / "run"))),
    }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _sqlite_online_backup(src: Path, dst: Path):
    """Transactionally-consistent copy of a live SQLite DB (no downtime). Returns
    the PRAGMA integrity_check result of the COPY."""
    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
            row = dst_conn.execute("PRAGMA integrity_check").fetchone()
            return row[0] if row else "unknown"
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _iter_component_files(root: Path, names):
    """Yield (relpath_within_root, absolute_path) for each existing curated entry,
    recursing into directories (e.g. remote_keys/)."""
    for name in names:
        p = root / name
        if p.is_file():
            yield name, p
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    yield str(f.relative_to(root)), f


def create_backup(dest_dir, roots=None, label=None, now=None, sanitize=False):
    """Write a backup archive into dest_dir and return its Path. `now` is the epoch
    timestamp to stamp (injectable for deterministic tests).

    sanitize=True omits plaintext key material (see _SECRET_MATCH) — the default
    for a portable migration bundle. The set of omitted files is recorded in the
    manifest ("sanitized"/"omitted") so a later import can tell the operator what
    to re-provision on the target."""
    roots = roots or resolve_roots()
    now = now if now is not None else time.time()
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    staging = Path(tempfile.mkdtemp(prefix="sysible-backup-"))
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created": now,
        "label": label or "",
        "components": {},
        "db_integrity": None,
        "sanitized": bool(sanitize),
        "omitted": [],
        "warnings": [],
    }
    # A sanitized bundle claims to be safe to move because the DB holds only hashes and
    # vault-encrypted blobs — true ONLY while at-rest encryption is active. If the vault
    # is in the deliberately-degraded mode (no key + SYSIBLE_SECRET_REQUIRED=0), the DB
    # may contain PLAINTEXT secrets, so warn rather than assert safety.
    if sanitize:
        try:
            from backend import secret_vault
            if not secret_vault.is_encrypting_at_rest():
                manifest["warnings"].append(
                    "at-rest encryption is DEGRADED (no key + SYSIBLE_SECRET_REQUIRED=0): "
                    "the database in this sanitized bundle may contain PLAINTEXT secrets "
                    "(e.g. sudo/become passwords). Treat this bundle as sensitive despite "
                    "--sanitize.")
        except Exception:
            pass
    try:
        # 1) DB snapshot via SQLite's online backup API (no downtime).
        if roots["db"].exists():
            db_src = roots["db"]
            (staging / "db").mkdir(parents=True, exist_ok=True)
            db_dst = staging / "db" / "sysible.db"
            manifest["db_integrity"] = _sqlite_online_backup(db_src, db_dst)
            manifest["db_kind"] = "sqlite"
            manifest["components"]["db"] = [{
                "path": "sysible.db",
                "sha256": _sha256(db_dst),
                "size": db_dst.stat().st_size,
            }]

        # 2) Curated files from the data/cert/run roots.
        for comp, names in _COMPONENTS.items():
            entries = []
            for rel, abspath in _iter_component_files(roots[comp], names):
                if sanitize and _is_secret(comp, rel):
                    manifest["omitted"].append(f"{comp}/{rel}")
                    continue
                out = staging / comp / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(abspath, out)
                entries.append({"path": rel, "sha256": _sha256(out),
                                "size": out.stat().st_size,
                                "mode": oct(abspath.stat().st_mode & 0o777)})
            if entries:
                manifest["components"][comp] = entries

        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2))

        # 3) Deterministic tar.gz, written 0600.
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
        archive = dest_dir / f"sysible-backup-{stamp}.tar.gz"
        fd = os.open(str(archive), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as raw:
            with tarfile.open(fileobj=raw, mode="w:gz") as tar:
                tar.add(staging, arcname="sysible-backup")
        return archive
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def omitted_labels(manifest) -> list:
    """Human-readable labels for the secrets a sanitized export left out, so the
    operator knows what must be re-provisioned on the target after import."""
    labels = []
    for path in manifest.get("omitted", []):
        if path.startswith("run/"):
            labels.append("vault master key + session-signing keys")
        elif path in ("cert/server.key", "cert/server.pem"):
            labels.append("TLS private key (host identity)")
        elif path == "data/api_key.txt":
            labels.append("bootstrap/enrollment API key")
        elif path.startswith("data/remote_keys/"):
            labels.append("per-host SSH private keys")
    out = []
    for l in labels:
        if l not in out:
            out.append(l)
    return out


def read_manifest(archive):
    """Return the manifest dict from a backup archive without extracting it."""
    with tarfile.open(archive, "r:gz") as tar:
        member = tar.getmember("sysible-backup/manifest.json")
        fh = tar.extractfile(member)
        return json.loads(fh.read().decode())


def _safe_extract(tar, dest):
    """Extract guarding against path traversal (CVE-2007-4559) AND link-based escapes.

    A legitimate Sysible bundle contains only regular files and directories
    (create_backup copies real file bytes into the staging tree — no links), so any
    symlink/hardlink/device member is rejected outright. Without this, a crafted import
    archive could ship a symlink member whose *linkname* is an absolute path (which a
    name-only check misses) plus a follow-up regular member written 'through' it,
    landing an attacker file anywhere as the root user running restore."""
    dest = Path(dest).resolve()
    for member in tar.getmembers():
        if member.issym() or member.islnk() or member.isdev():
            raise ValueError(
                f"unsafe archive member (links/devices not allowed): {member.name}")
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(dest) + os.sep) and target != dest:
            raise ValueError(f"unsafe path in archive: {member.name}")
    tar.extractall(dest)


def restore_backup(archive, roots=None, force=False, verify=True):
    """Restore an archive's components into `roots`. Refuses to overwrite an
    existing DB unless force=True. Returns the restored manifest.

    Run OFFLINE (controller stopped): it overwrites the live DB and keys."""
    roots = roots or resolve_roots()
    staging = Path(tempfile.mkdtemp(prefix="sysible-restore-"))
    try:
        with tarfile.open(archive, "r:gz") as tar:
            _safe_extract(tar, staging)
        base = staging / "sysible-backup"
        manifest = json.loads((base / "manifest.json").read_text())

        if verify:
            _verify_archive(base, manifest)

        # DB
        db_files = manifest["components"].get("db")
        if db_files:
            src = base / "db" / "sysible.db"
            dst = roots["db"]
            if dst.exists() and not force:
                raise FileExistsError(
                    f"{dst} exists; refusing to overwrite without force=True "
                    "(stop the controller first).")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

        # data/cert/run — restore each file to its root, preserving key perms.
        for comp in ("data", "cert", "run"):
            root = Path(roots[comp]).resolve()
            for entry in manifest["components"].get(comp, []):
                rel = entry["path"]
                # entry["path"] comes from the (untrusted) manifest and bypasses the
                # tar-member guard, so re-validate it: reject absolute/".." paths and
                # confirm the destination stays inside the component root, or a
                # crafted archive could write a root-owned file anywhere.
                if os.path.isabs(rel) or ".." in Path(rel).parts:
                    raise ValueError(f"unsafe path in manifest: {rel!r}")
                dst = (root / rel).resolve()
                if root != dst and not str(dst).startswith(str(root) + os.sep):
                    raise ValueError(f"path escapes {comp} root: {rel!r}")
                src = base / comp / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                # Restore recorded mode for secrets (keys must land 0600). Mask to the
                # permission bits only — never honour setuid/setgid/sticky from the
                # (untrusted) manifest, or a crafted bundle could drop a setuid-root
                # binary inside a component root that any local user then executes.
                mode = entry.get("mode")
                if mode:
                    try:
                        os.chmod(dst, int(mode, 8) & 0o777)
                    except (ValueError, OSError):
                        pass
        return manifest
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _verify_archive(base: Path, manifest: dict):
    """Re-hash every file against the manifest and re-check DB integrity."""
    for comp, entries in manifest["components"].items():
        sub = "db" if comp == "db" else comp
        for entry in entries:
            f = base / sub / entry["path"]
            if not f.exists():
                raise ValueError(f"archive missing {comp}/{entry['path']}")
            if _sha256(f) != entry["sha256"]:
                raise ValueError(f"checksum mismatch for {comp}/{entry['path']}")
    db_files = manifest["components"].get("db")
    if db_files:
        conn = sqlite3.connect(str(base / "db" / "sysible.db"))
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            if not row or row[0] != "ok":
                raise ValueError(f"restored DB failed integrity_check: {row}")
        finally:
            conn.close()
