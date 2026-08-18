# Running Sysible Controller in a container

The controller ships as a single container image that runs both long-running
services under `supervisord`:

| Service    | Port | Purpose                                                        |
|------------|------|----------------------------------------------------------------|
| backend    | 9000 | Controller API — agents enroll/poll here; the CLI + console use it |
| web console| 8800 | React web console (BFF) — serves the UI, proxies admin actions |

All persistent state lives on the **`/data`** volume, so the container itself is
disposable — rebuild or upgrade the image without losing enrollments, the
database, or the pinned TLS certificate:

```
/data
├── sysible.db        # SQLite database (agents, admins, config, metrics)
├── certs/            # server.crt / server.key / trust.crt (agents pin these)
├── api_key.txt       # backend API key (backend <-> BFF/CLI)
├── run/              # cookie secret, pid/port sidecars
└── portal_files/     # host <-> controller file transfers
```

## Quick start (docker compose)

```bash
# Cover the address agents/browsers will use to reach this host in the TLS cert:
export SYSIBLE_CONTROLLER_HOSTNAMES="controller.example.com,10.0.0.5"

docker compose up -d --build
docker compose logs -f          # first run prints the seeded admin password ONCE
```

Then open **https://<host>:8800**. The certificate is self-signed, so your
browser warns once — that's expected on a LAN controller. Log in with the
`admin` user and the password from the logs (you'll be forced to change it).

To choose the first password yourself instead of having one generated:

```bash
SYSIBLE_ADMIN_USERNAME=admin SYSIBLE_ADMIN_PASSWORD='pick-a-strong-one' \
  docker compose up -d --build
```

## Quick start (plain docker)

```bash
docker build -t sysible-controller .
docker run -d --name sysible-controller \
  -p 8800:8800 -p 9000:9000 \
  -e SYSIBLE_CONTROLLER_HOSTNAMES="controller.example.com,10.0.0.5" \
  -v sysible-data:/data \
  sysible-controller
docker logs -f sysible-controller
```

## TLS and agents

Agents **pin** the controller's certificate, so it must be valid for the exact
host address they use. Set `SYSIBLE_CONTROLLER_HOSTNAMES` before the first start
(comma-separated DNS names and/or IPs). The cert is generated only once and never
rotated automatically on restart — the pin stays stable.

To add addresses later, or rotate the cert, use the web console:
**Settings → Regenerate certificate**. In the container this restarts the backend
via `supervisorctl` (no systemd needed), and re-trusts the controller's own agent.

## Environment variables

| Variable | Default | Meaning |
|----------|---------|---------|
| `SYSIBLE_CONTROLLER_HOSTNAMES` | *(empty)* | Extra DNS/IPs to put in the TLS cert SAN |
| `SYSIBLE_ADMIN_USERNAME` | `admin` | First superuser's name (only used when seeding) |
| `SYSIBLE_ADMIN_PASSWORD` | *(generated)* | First superuser's password; if unset one is generated and logged once |
| `SYSIBLE_WEBGUI_PORT` | `8800` | Web console port (inside the container) |
| `SYSIBLE_BACKEND_PORT` | `9000` | Controller API port (inside the container) |

Persistent paths are pre-pointed at `/data` via the image's environment
(`SYSIBLE_DB_PATH`, `SYSIBLE_CERT_DIR`, `SYSIBLE_RUN_DIR`, `SYSIBLE_API_KEY_FILE`,
`SYSIBLE_PORTAL_FILES_DIR`) — override them only if you mount the volume
elsewhere.

## Backups

Back up the whole `/data` volume (it holds the DB, certs, and secrets):

```bash
docker run --rm -v sysible-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/sysible-data.tgz -C /data .
```

## Notes / limitations

- **Self-update** (the console's "pull & restart to apply update" flow) is a
  native-install feature that reinstalls from git. In a container the update
  model is instead: pull the new image and `docker compose up -d` — so that
  button isn't the upgrade path here.
- The image runs its services as root **inside the container** (matching the
  native `User=root` service); it holds no host mounts beyond the named data
  volume. Put it behind your own reverse proxy / network policy as usual.
- Enterprise (Postgres) backends: point the backend at your database with
  `SYSIBLE_DB_URL` and the DB won't use the on-volume SQLite file. (This image
  defaults to the SQLite/Community path.)
