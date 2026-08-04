#!/usr/bin/env python3
"""Back up the Sysible controller (Community).

Captures a consistent, portable snapshot — the SQLite DB (via SQLite's online
backup, so NO downtime is needed to take a backup), the install/API key, hosts.json
and per-host SSH keys, the TLS cert+key+trust bundle, and the secret-vault master
key + session keys — into a single 0600 tar.gz with a checksummed manifest.

    sudo python3 tools/backup.py --output-dir /var/backups/sysible

Schedule it (systemd timer / cron) and ship the archive OFF-BOX. A full archive
holds private keys and the vault master key — treat it as sensitive as the
controller and store it encrypted at rest. Pass --sanitize for a portable
migration bundle that omits that key material. See docs/BACKUP_RESTORE.md.

Set SYSIBLE_DATA_DIR / SYSIBLE_CERT_DIR / SYSIBLE_RUN_DIR to match the running
service so this operates on the real state.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import backup_lib  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Back up the Sysible controller.")
    ap.add_argument("--output-dir", "-o", default=".", help="where to write the archive")
    ap.add_argument("--label", default="", help="optional label stored in the manifest")
    ap.add_argument("--sanitize", action="store_true",
                    help="omit plaintext key material (vault master key, TLS private "
                         "key, bootstrap API key, per-host SSH keys) — for a portable "
                         "migration bundle rather than a full disaster-recovery backup")
    args = ap.parse_args()

    roots = backup_lib.resolve_roots()
    if not roots["db"].exists():
        print(f"WARNING: database not found at {roots['db']} — backing up keys/config only.",
              file=sys.stderr)

    archive = backup_lib.create_backup(args.output_dir, roots=roots, label=args.label,
                                       sanitize=args.sanitize)
    manifest = backup_lib.read_manifest(archive)
    comps = ", ".join(f"{k}({len(v)})" for k, v in manifest["components"].items())
    print(f"Backup written: {archive}")
    print(f"  components: {comps or '(none)'}")
    print(f"  db integrity: {manifest.get('db_integrity')}")
    for w in manifest.get("warnings", []):
        print(f"  ⚠ WARNING: {w}", file=sys.stderr)
    if manifest.get("sanitized"):
        omitted = backup_lib.omitted_labels(manifest)
        print("  sanitized: yes — plaintext key material was NOT included.")
        if omitted:
            print("  omitted (re-provision on the target after import):")
            for label in omitted:
                print(f"    - {label}")
        print("  note: the DB is included but any vault-encrypted values become")
        print("        undecryptable without the vault master key — re-run with")
        print("        --with-secrets for a full clone that keeps them.")
    else:
        print("Store this archive OFF-BOX and encrypted at rest (it contains private keys).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
