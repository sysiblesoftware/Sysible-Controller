# Backup, restore & migration — Community

A tested backup/restore procedure for the controller, plus a portable export/import
for moving a controller to a new host or reinstalling.

## What is backed up

`tools/backup.py` captures everything needed to rebuild a controller, in one
`0600` `tar.gz` with a checksummed `manifest.json`:

| Component | Contents | Why it must be in the backup |
|---|---|---|
| **Database** | `backend/sysible.db` (SQLite, via the online backup API) | agents, admins, tokens, activity log, approvals — the entire control-plane state |
| **Data dir** (`SYSIBLE_DATA_DIR`) | `api_key.txt`, `hosts.json`, `remote_keys/` | install API key + SSH host inventory + per-host SSH private keys |
| **Cert dir** (`SYSIBLE_CERT_DIR`) | `server.crt`, `server.key`, `trust.crt` | the controller's **TLS identity** — restoring it keeps pinned agents verifying the same cert |
| **Run dir** (`SYSIBLE_RUN_DIR`) | `controller_secret.key`, `webgui.secret`, `webgui_sudo.key` | the **secret-vault master key** + session-signing keys — **without the master key, the encrypted secrets in the DB are unrecoverable** |

The DB is copied with SQLite's **online backup API**, which is transactionally
consistent and needs **no downtime** to capture. Restore re-checksums every file and
runs `PRAGMA integrity_check` on the DB copy before writing anything.

> **A full backup is as sensitive as the controller.** It contains private keys and
> the vault master key. Store it **encrypted at rest** — an encrypted volume, or an
> object store with server-side encryption, or pipe the archive through your own
> `gpg`/`age`. Restrict read access to the operators.

## Take a backup

```bash
sudo SYSIBLE_DATA_DIR=/opt/sysible python3 tools/backup.py \
     --output-dir /var/backups/sysible --label nightly
# Backup written: /var/backups/sysible/sysible-backup-20260115T030000Z.tar.gz
```

Then **ship it off-box**: `aws s3 cp`, `rsync` to another site, etc. A backup that
only lives on the controller doesn't survive losing the controller.

## Restore (disaster recovery)

Restore is **offline** — stop the services first. The manifest is validated and every
file re-checksummed, and the DB is `integrity_check`ed, **before** anything is
written, so a corrupt archive fails safe.

```bash
sudo systemctl stop sysible-backend sysible-webgui
sudo python3 tools/restore.py /var/backups/sysible/sysible-backup-XXXX.tar.gz --force
sudo systemctl start sysible-backend sysible-webgui
```

`--force` is required to overwrite an existing database (guards against clobbering a
live controller by accident). Inspect an archive without restoring with
`tools/restore.py <archive> --inspect`.

Because the TLS identity and vault master key are restored, **agents keep
authenticating and encrypted secrets stay decryptable — no re-enrollment**. If you
restore onto a host with a different layout, point `SYSIBLE_DATA_DIR` /
`SYSIBLE_CERT_DIR` / `SYSIBLE_RUN_DIR` at the target paths.

## Migrate / reinstall — portable export & import

For **moving a controller to a new host or reinstalling** — as opposed to full
disaster recovery — the CLI wraps the same engine with a sanitized-by-default bundle
so you can hand the archive around without carrying the crown-jewel keys:

```bash
# Sanitized migration bundle (DEFAULT): DB + inventory + public certs, NO key material.
sudo sysible_controller export /var/backups
#   omitted (re-provision on the target after import):
#     - vault master key + session-signing keys
#     - TLS private key (host identity)
#     - bootstrap/enrollment API key
#     - per-host SSH private keys

# Full 1:1 clone (includes all secrets — treat exactly like a DR backup):
sudo sysible_controller export /var/backups --with-secrets
```

Load it into the target controller (offline, services stopped):

```bash
sudo sysible_controller stop
sudo sysible_controller import /var/backups/sysible-backup-XXXX.tar.gz --force
sudo sysible_controller start
```

`import … --inspect` prints the bundle's manifest without changing anything.

**What sanitized means.** The DB is always included (it holds only hashes and
vault-encrypted blobs). A sanitized bundle strips the plaintext key material listed
above, so after importing one you must **re-provision the vault key + TLS identity**
(re-run the installer's key-generation step) — and because the old vault master key
is gone, any **vault-encrypted values in the DB can't be decrypted**. Use
`--with-secrets` when you want a true clone that keeps the same identity and all
encrypted values intact.

Under the hood, `export`/`import` call `tools/backup.py --sanitize` and
`tools/restore.py`; the manifest checksums, integrity checks, overwrite guard and
layout overrides above all apply identically.

## Schedule it (systemd timer)

`/etc/systemd/system/sysible-backup.service`:
```ini
[Unit]
Description=Sysible controller backup

[Service]
Type=oneshot
Environment=SYSIBLE_DATA_DIR=/opt/sysible
ExecStart=/usr/bin/python3 /opt/sysible/tools/backup.py -o /var/backups/sysible
# Off-box copy + retention pruning go here (or in a wrapper):
ExecStartPost=/usr/local/bin/sysible-backup-ship.sh
```
`/etc/systemd/system/sysible-backup.timer`:
```ini
[Unit]
Description=Nightly Sysible backup
[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true
[Install]
WantedBy=timers.target
```
```bash
sudo systemctl enable --now sysible-backup.timer
```

## Verify the restore

1. `systemctl status sysible-backend sysible-webgui` — both active.
2. Confirm agents heartbeat (dashboard shows hosts online) and an admin can log in.
3. Spot-check the activity log verifies (`/activity-log/verify`) under the restored key.

A backup you've never restored is a hope, not a plan — do a restore drill onto an
isolated host on a schedule and record the recovery time.
