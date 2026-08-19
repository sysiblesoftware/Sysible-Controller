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

## Command-line control

You get the **same `sysible_controller <command>` experience as a native
install**. Install the host wrapper once (it forwards each command into the
running container), then use it exactly like the native CLI:

```bash
# one-time install on the Docker host:
sudo cp docker/sysible-controller /usr/local/bin/sysible_controller
sudo chmod +x /usr/local/bin/sysible_controller

# then, from the host — no `docker exec` needed:
sysible_controller status          # both services
sysible_controller logs            # follow backend logs
sysible_controller restart         # restart both services
sysible_controller reset-admin     # reset the admin password (prints it once)
sysible_controller rotate-api-key
```

The wrapper finds the controller container automatically (override the name with
`SYSIBLE_CONTAINER_NAME` if you renamed it). It needs an image that includes the
container-aware CLI — if you're on an older image, rebuild with
`docker compose up -d --build` first.

Prefer not to install the wrapper? The same commands work spelled out in full:

```bash
docker exec -it sysible-controller sysible_controller status
docker exec -it sysible-controller sysible_controller reset-admin
```

Container *lifecycle* (start / stop / upgrade / destroy) is managed from the host
with `docker compose` / `docker`, not from inside — e.g. `docker compose restart`,
`docker compose up -d` to upgrade the image, `docker compose down` to stop.

## Recovering the admin password

The admin credential lives in the DB on the `/data` volume. If you changed the
password and it is "not recognized", first confirm the DB actually persisted — if
the container was recreated **without** the named volume, the DB was reset and a
fresh one-time password was printed to the logs:

```bash
docker volume ls | grep sysible-data                              # the named volume must exist
docker logs sysible-controller | grep -A4 -i "seeded initial"     # the one-time initial password
```

Always start it with `docker compose up -d` (which mounts `sysible-data:/data`) —
never a bare `docker run` without `-v sysible-data:/data`, or state will not
survive a restart.

To set a known password (it forces a change at next login):

```bash
docker exec -it sysible-controller sysible_controller reset-admin admin 'YourNewPass#123'
```

On an image built **before** this CLI became container-aware, drive the backend
directly instead (same effect — the DB is at `/data/sysible.db`):

```bash
docker exec -i sysible-controller python3 - <<'PYEOF'
from backend import portal_auth
from backend.db import get_administrator, update_administrator_password, add_administrator
salt, h = portal_auth.hash_password("YourNewPass#123")
if get_administrator("admin"):
    update_administrator_password("admin", h, salt, must_change_password=0)
else:
    add_administrator("admin", h, salt, must_change_password=0, created_by="exec", role="superuser")
print("admin password set")
PYEOF
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

## Troubleshooting: build fails with a DNS error on auth.docker.io

```
failed to fetch anonymous token: Get "https://auth.docker.io/token?...":
dial tcp: lookup auth.docker.io on 172.16.254.2:53: no such host
```

This is **BuildKit**, not the Dockerfile. BuildKit reads `/etc/resolv.conf`
directly, so on a host whose DNS is a NAT/internal resolver (VMware's
`172.16.254.x`, a corporate server) it can fail even though the host itself
resolves fine — `getent hosts auth.docker.io` and `docker pull` both work.

**Quickest fix** — pull the two base images once, then build without BuildKit
(the legacy builder uses local images and never contacts the registry):

```bash
docker pull node:20-slim && docker pull python:3.12-slim
DOCKER_BUILDKIT=0 docker build -t sysible-controller:latest .
docker compose up -d          # note: no --build; it uses the image just built
```

**Or give BuildKit working DNS** by running its builder on the host network:

```bash
docker buildx rm sysible 2>/dev/null || true   # required if it already exists
docker buildx create --use --name sysible --driver docker-container --driver-opt network=host
docker buildx inspect --bootstrap
docker compose up -d --build
```

(`buildx create` fails with *"existing instance for … but no append mode"* if a
builder of that name is already present — remove it first, as above.)

**Fully offline / air-gapped:** load the base images from a machine that can
reach Docker Hub, then build as normal:

```bash
# on a connected machine
docker pull node:20-slim && docker pull python:3.12-slim
docker save node:20-slim python:3.12-slim -o ~/base-images.tar
# on the controller host (write somewhere you own — /opt may be root-only)
docker load -i ~/base-images.tar
DOCKER_BUILDKIT=0 docker build -t sysible-controller:latest .
```
