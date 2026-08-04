#!/usr/bin/env python3
"""Restore a Sysible controller from a backup archive (Community).

Run this OFFLINE, with the controller stopped — it overwrites the live database,
keys, and config:

    sudo systemctl stop sysible-backend sysible-webgui
    sudo python3 tools/restore.py /var/backups/sysible/sysible-backup-XXXX.tar.gz --force
    sudo systemctl start sysible-backend sysible-webgui

The archive's manifest is validated and every file re-checksummed, and the DB is
integrity-checked, BEFORE anything is written. Restoring the vault master key + TLS
identity means agents keep authenticating and encrypted secrets stay decryptable —
no re-enrollment. Set SYSIBLE_DATA_DIR / SYSIBLE_CERT_DIR / SYSIBLE_RUN_DIR to match
the target layout (defaults mirror the installer).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import backup_lib  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Restore the Sysible controller from a backup.")
    ap.add_argument("archive", help="path to a sysible-backup-*.tar.gz")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing database (required for an in-place restore)")
    ap.add_argument("--inspect", action="store_true",
                    help="print the manifest and exit without restoring")
    args = ap.parse_args()

    if args.inspect:
        m = backup_lib.read_manifest(args.archive)
        import json
        print(json.dumps(m, indent=2))
        return 0

    roots = backup_lib.resolve_roots()
    manifest = backup_lib.restore_backup(args.archive, roots=roots, force=args.force)
    comps = ", ".join(f"{k}({len(v)})" for k, v in manifest["components"].items())
    print(f"Restored: {comps}")
    print(f"  db → {roots['db']}")
    if manifest.get("sanitized"):
        omitted = backup_lib.omitted_labels(manifest)
        print("This was a SANITIZED bundle — plaintext key material was not included.")
        if omitted:
            print("Re-provision on this controller before agents can reconnect:")
            for label in omitted:
                print(f"  - {label}")
        print("Run the installer's key-generation step (or restore a --with-secrets")
        print("bundle) to mint a new vault key + TLS identity; enrolled agents pinned")
        print("to the old identity must be re-enrolled.")
    print("Start the controller and verify: /activity-log/verify, agent heartbeats, admin login.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
