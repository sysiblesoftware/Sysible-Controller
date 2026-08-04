"""
Controller backup/restore (Community) — tools/backup_lib.py.

Proves a backup round-trips: the SQLite DB (captured via SQLite's online backup)
plus the install key, per-host SSH keys, TLS identity, and the vault master key all
come back, secrets keep 0600, the manifest's checksums gate the restore, and a
sanitized migration export omits plaintext key material while keeping the DB.
"""
import json
import os
import sqlite3

import pytest

from tools import backup_lib


def _seed_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE agents (host_id TEXT PRIMARY KEY, label TEXT)")
        conn.execute("INSERT INTO agents VALUES ('h1', 'host-one')")
        conn.commit()
    finally:
        conn.close()


def _seed_state(base):
    roots = {
        "db": base / "backend" / "sysible.db",
        "data": base / "data",
        "cert": base / "cert",
        "run": base / "run",
    }
    _seed_db(roots["db"])
    for k in ("data", "cert", "run"):
        roots[k].mkdir(parents=True, exist_ok=True)
    (roots["data"] / "api_key.txt").write_text("KEY123")
    os.chmod(roots["data"] / "api_key.txt", 0o600)
    (roots["data"] / "hosts.json").write_text('{"h1": {"ip": "10.0.0.1"}}')
    (roots["data"] / "remote_keys").mkdir()
    (roots["data"] / "remote_keys" / "h1.pem").write_text("PRIVATE-KEY-BYTES")
    (roots["cert"] / "server.crt").write_text("CERTDATA")
    (roots["cert"] / "server.key").write_text("KEYDATA")
    os.chmod(roots["cert"] / "server.key", 0o600)
    (roots["run"] / "controller_secret.key").write_text("MASTER-VAULT-KEY")
    os.chmod(roots["run"] / "controller_secret.key", 0o600)
    return roots


def test_backup_restore_roundtrip(tmp_path):
    roots = _seed_state(tmp_path / "orig")
    archive = backup_lib.create_backup(tmp_path / "backups", roots=roots, now=1_700_000_000)

    assert archive.exists()
    assert oct(archive.stat().st_mode & 0o777) == "0o600"       # archive is 0600
    m = backup_lib.read_manifest(archive)
    assert m["db_kind"] == "sqlite" and m["db_integrity"] == "ok"
    assert set(m["components"]) >= {"db", "data", "cert", "run"}

    roots2 = {
        "db": tmp_path / "restored" / "backend" / "sysible.db",
        "data": tmp_path / "restored" / "data",
        "cert": tmp_path / "restored" / "cert",
        "run": tmp_path / "restored" / "run",
    }
    for k in ("data", "cert", "run"):
        roots2[k].mkdir(parents=True, exist_ok=True)
    backup_lib.restore_backup(archive, roots=roots2, force=True)

    # DB restored: the seeded row is present in the restored copy.
    conn = sqlite3.connect(str(roots2["db"]))
    try:
        row = conn.execute("SELECT label FROM agents WHERE host_id='h1'").fetchone()
    finally:
        conn.close()
    assert row and row[0] == "host-one"

    # Files came back byte-for-byte, including recursive remote_keys/ and the vault key.
    assert (roots2["data"] / "api_key.txt").read_text() == "KEY123"
    assert (roots2["data"] / "remote_keys" / "h1.pem").read_text() == "PRIVATE-KEY-BYTES"
    assert (roots2["cert"] / "server.key").read_text() == "KEYDATA"
    assert (roots2["run"] / "controller_secret.key").read_text() == "MASTER-VAULT-KEY"

    # Secret files keep 0600 after restore.
    assert oct((roots2["run"] / "controller_secret.key").stat().st_mode & 0o777) == "0o600"
    assert oct((roots2["cert"] / "server.key").stat().st_mode & 0o777) == "0o600"


def test_restore_refuses_existing_db_without_force(tmp_path):
    roots = _seed_state(tmp_path / "orig")
    archive = backup_lib.create_backup(tmp_path / "backups", roots=roots)
    # roots["db"] still exists → refuse without force, apply with it.
    with pytest.raises(FileExistsError):
        backup_lib.restore_backup(archive, roots=roots, force=False)
    backup_lib.restore_backup(archive, roots=roots, force=True)


def test_restore_rejects_manifest_path_traversal(tmp_path):
    import io
    import tarfile
    roots = _seed_state(tmp_path / "orig")
    archive = backup_lib.create_backup(tmp_path / "backups", roots=roots)
    with tarfile.open(archive, "r:gz") as t:
        man = json.loads(t.extractfile("sysible-backup/manifest.json").read())
    man["components"]["data"] = [{"path": "../../evil.txt", "sha256": "0" * 64, "size": 1}]
    forged = tmp_path / "forged.tar.gz"
    with tarfile.open(archive, "r:gz") as src, tarfile.open(forged, "w:gz") as dst:
        for m in src.getmembers():
            if m.name.endswith("manifest.json"):
                data = json.dumps(man).encode()
                m.size = len(data)
                dst.addfile(m, io.BytesIO(data))
            else:
                dst.addfile(m, src.extractfile(m))
    with pytest.raises(ValueError):
        backup_lib.restore_backup(forged, roots=roots, force=True, verify=False)


def test_verify_detects_corruption(tmp_path):
    import io
    import tarfile
    roots = _seed_state(tmp_path / "orig")
    archive = backup_lib.create_backup(tmp_path / "backups", roots=roots)
    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(archive, "r:gz") as src, tarfile.open(tampered, "w:gz") as dst:
        for m in src.getmembers():
            if m.name.endswith("data/api_key.txt"):
                data = b"TAMPERED"
                m.size = len(data)
                dst.addfile(m, io.BytesIO(data))
            else:
                dst.addfile(m, src.extractfile(m))
    roots2 = {
        "db": tmp_path / "r2" / "backend" / "sysible.db",
        "data": tmp_path / "r2" / "data",
        "cert": tmp_path / "r2" / "cert",
        "run": tmp_path / "r2" / "run",
    }
    for k in ("data", "cert", "run"):
        roots2[k].mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValueError):
        backup_lib.restore_backup(tampered, roots=roots2, force=True)


def test_sanitized_export_omits_key_material_keeps_data(tmp_path):
    # A sanitized migration bundle omits plaintext key material while still carrying
    # the DB and non-secret config/public certs.
    roots = _seed_state(tmp_path / "orig")
    archive = backup_lib.create_backup(tmp_path / "backups", roots=roots, sanitize=True)
    m = backup_lib.read_manifest(archive)

    assert m["sanitized"] is True
    assert "db" in m["components"]
    data_paths = {e["path"] for e in m["components"].get("data", [])}
    cert_paths = {e["path"] for e in m["components"].get("cert", [])}
    assert "hosts.json" in data_paths
    assert "server.crt" in cert_paths
    assert "run" not in m["components"]                     # vault + session keys
    assert "server.key" not in cert_paths                   # TLS private key
    assert "api_key.txt" not in data_paths                  # bootstrap key
    assert not any(p.startswith("remote_keys") for p in data_paths)  # per-host SSH keys

    assert "run/controller_secret.key" in m["omitted"]
    assert "cert/server.key" in m["omitted"]
    assert "data/api_key.txt" in m["omitted"]
    labels = backup_lib.omitted_labels(m)
    assert any("vault master key" in l for l in labels)
    assert any("TLS private key" in l for l in labels)
    assert any("bootstrap" in l for l in labels)

    # A sanitized bundle still restores cleanly — only the secret files are absent.
    roots2 = {
        "db": tmp_path / "san" / "backend" / "sysible.db",
        "data": tmp_path / "san" / "data",
        "cert": tmp_path / "san" / "cert",
        "run": tmp_path / "san" / "run",
    }
    for k in ("data", "cert", "run"):
        roots2[k].mkdir(parents=True, exist_ok=True)
    backup_lib.restore_backup(archive, roots=roots2, force=True)
    assert (roots2["data"] / "hosts.json").exists()
    assert not (roots2["run"] / "controller_secret.key").exists()
    assert not (roots2["cert"] / "server.key").exists()


def test_full_export_includes_secrets_by_default(tmp_path):
    roots = _seed_state(tmp_path / "orig")
    archive = backup_lib.create_backup(tmp_path / "backups", roots=roots)
    m = backup_lib.read_manifest(archive)
    assert m["sanitized"] is False
    assert m["omitted"] == []
    assert "run" in m["components"]
    assert backup_lib.omitted_labels(m) == []


def test_restore_rejects_symlink_member(tmp_path):
    # SEC: reject any symlink/hardlink/device member (link-based path escape → root
    # file write on import). A legit bundle never contains one.
    import tarfile
    roots = _seed_state(tmp_path / "orig")
    archive = backup_lib.create_backup(tmp_path / "backups", roots=roots)
    forged = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "r:gz") as src, tarfile.open(forged, "w:gz") as dst:
        for m in src.getmembers():
            dst.addfile(m, src.extractfile(m) if m.isreg() else None)
        link = tarfile.TarInfo("sysible-backup/pwn")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/cron.d"
        dst.addfile(link)
    with pytest.raises(ValueError):
        backup_lib.restore_backup(forged, roots=roots, force=True, verify=False)


def test_restore_strips_setuid_from_manifest_mode(tmp_path):
    # SEC: a crafted 0o6755 mode must not restore a setuid binary; mask to 0o777.
    import io
    import json
    import tarfile
    roots = _seed_state(tmp_path / "orig")
    archive = backup_lib.create_backup(tmp_path / "backups", roots=roots)
    with tarfile.open(archive, "r:gz") as t:
        man = json.loads(t.extractfile("sysible-backup/manifest.json").read())
    for e in man["components"]["data"]:
        if e["path"] == "hosts.json":
            e["mode"] = "0o6755"
    forged = tmp_path / "setuid.tar.gz"
    with tarfile.open(archive, "r:gz") as src, tarfile.open(forged, "w:gz") as dst:
        for m in src.getmembers():
            if m.name.endswith("manifest.json"):
                data = json.dumps(man).encode()
                m.size = len(data)
                dst.addfile(m, io.BytesIO(data))
            else:
                dst.addfile(m, src.extractfile(m) if m.isreg() else None)
    roots2 = {
        "db": tmp_path / "suid" / "backend" / "sysible.db",
        "data": tmp_path / "suid" / "data",
        "cert": tmp_path / "suid" / "cert",
        "run": tmp_path / "suid" / "run",
    }
    for k in ("data", "cert", "run"):
        roots2[k].mkdir(parents=True, exist_ok=True)
    backup_lib.restore_backup(forged, roots=roots2, force=True, verify=False)
    st = os.stat(roots2["data"] / "hosts.json").st_mode
    assert not (st & 0o7000)
    assert (st & 0o777) == 0o755
