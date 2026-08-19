###############################################################################
# Sysible Controller — container image
#
# One image, two long-running processes (managed by supervisord):
#   * backend  — uvicorn backend.app:app on :9000  (agents + admin/CLI API, TLS)
#   * webgui   — uvicorn webgui.server:app on :8800 (React web console BFF, TLS)
#
# All persistent state (SQLite DB, TLS certs, API key, cookie secret, portal
# files) lives on the /data volume — declared below — so the container is
# disposable and can be rebuilt without losing enrollments or the pinned cert.
#
# Build:   docker build -t sysible-controller .
# Run:     docker compose up -d      (see docker-compose.yml / DOCKER.md)
###############################################################################

# ---- stage 1: build the React web console into static files ----------------
FROM node:20-slim AS frontend
WORKDIR /src/webgui/frontend
# Install deps first (better layer caching), then build.
COPY webgui/frontend/package.json webgui/frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY webgui/frontend/ ./
RUN npm run build          # -> /src/webgui/frontend/dist

# ---- stage 2: python runtime ----------------------------------------------
FROM python:3.12-slim AS runtime

# Runtime OS packages:
#   openssl    — first-run self-signed TLS cert generation (entrypoint)
#   supervisor — process manager for the two uvicorn services (+ supervisorctl,
#                which the in-container "restart backend" flow uses)
#   ca-certificates — TLS trust for outbound (e.g. license/update checks)
#   tini       — reap zombies / forward signals as PID 1's child of supervisord
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         openssl supervisor ca-certificates tini curl bash \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps (backend + web console). Copied alone first for layer caching.
# NOTE: copy each requirements file to an EXPLICIT distinct destination. A
# multi-source `COPY a b ./deps/` flattens to BASENAMES, so both of these landed
# as deps/requirements.txt (the second silently overwriting the first) and
# deps/webgui/requirements.txt never existed — the pip step then failed with
# "Could not open requirements file: deps/webgui/requirements.txt".
COPY requirements.txt        ./deps/controller-requirements.txt
COPY webgui/requirements.txt ./deps/webgui-requirements.txt
RUN pip install --no-cache-dir -r deps/controller-requirements.txt -r deps/webgui-requirements.txt

# Application code.
COPY . /app
# Expose the control CLI on PATH so `docker exec <ctr> sysible_controller <cmd>`
# works. The script is container-aware (SYSIBLE_CONTAINER=1, set below): it drives
# supervisord + the /data volume instead of systemd + /opt/sysible. Gives back the
# command-line control (reset-admin, rotate-api-key, status, logs, restart) that a
# native install has via /usr/local/bin/sysible_controller.
RUN chmod +x /app/sysible_controller \
 && ln -sf /app/sysible_controller /usr/local/bin/sysible_controller \
 && ln -sf /app/sysible_controller /usr/local/bin/sysible-controller
# Built front end from stage 1 (dist/ is .dockerignore'd from the context).
COPY --from=frontend /src/webgui/frontend/dist /app/webgui/frontend/dist

# Container runtime wiring.
COPY docker/entrypoint.sh /usr/local/bin/sysible-entrypoint
COPY docker/supervisord.conf /etc/supervisor/conf.d/sysible.conf
RUN chmod +x /usr/local/bin/sysible-entrypoint

# --- environment: point every persistent path at the /data volume ----------
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    SYSIBLE_CONTAINER=1 \
    SYSIBLE_DATA_DIR=/data \
    SYSIBLE_CERT_DIR=/data/certs \
    SYSIBLE_RUN_DIR=/data/run \
    SYSIBLE_DB_PATH=/data/sysible.db \
    SYSIBLE_PORTAL_FILES_DIR=/data/portal_files \
    SYSIBLE_API_KEY_FILE=/data/api_key.txt \
    SYSIBLE_API_URL=https://127.0.0.1:9000 \
    SYSIBLE_CA_CERT=/data/certs/server.crt \
    SYSIBLE_WEBGUI_PORT=8800 \
    SYSIBLE_BACKEND_PORT=9000

VOLUME ["/data"]
EXPOSE 9000 8800

# Simple liveness: the web console answers on :8800 (TLS, self-signed -> -k).
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsk https://127.0.0.1:8800/api/health >/dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/sysible-entrypoint"]
CMD ["supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
