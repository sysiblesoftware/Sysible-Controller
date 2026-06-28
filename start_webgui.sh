#!/usr/bin/env bash
# Sysible Web Console launcher — one command to stand up the browser-based
# console on a headless controller. Idempotent: builds the front end if it
# hasn't been built, ensures the cookie-signing secret is stable, enables TLS
# if the controller has certs, then runs the BFF.
#
#   ./start_webgui.sh [PORT]
#
# Defaults: port 8800. Reads SYSIBLE_* from the environment if already set.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PORT="${1:-${SYSIBLE_WEBGUI_PORT:-8800}}"
RUN_DIR="${SYSIBLE_RUN_DIR:-$HERE/run}"
SECRET_FILE="$RUN_DIR/webgui.secret"
CERT_FILE="${SYSIBLE_CERT_FILE:-$HERE/certs/server.crt}"
KEY_FILE="${SYSIBLE_KEY_FILE:-$HERE/certs/server.key}"
FRONTEND="$HERE/webgui/frontend"

PY="${PYTHON:-python3}"

echo "[webgui] project: $HERE"

# 1) Front end: build if dist/ is missing OR any source file is newer than the
#    built bundle, so a plain restart always serves the latest code (no full
#    reinstall needed after a git pull). Set SYSIBLE_WEBGUI_NOBUILD=1 to skip.
need_build=0
if [ ! -f "$FRONTEND/dist/index.html" ]; then
  need_build=1
elif [ -d "$FRONTEND/src" ]; then
  # newest source file (src + index.html + configs) vs the built index.html
  newest_src="$(find "$FRONTEND/src" "$FRONTEND/index.html" "$FRONTEND/package.json" \
                  "$FRONTEND/vite.config.js" -type f -newer "$FRONTEND/dist/index.html" 2>/dev/null | head -1)"
  [ -n "$newest_src" ] && need_build=1
fi

if [ "${SYSIBLE_WEBGUI_NOBUILD:-0}" = "1" ]; then
  echo "[webgui] SYSIBLE_WEBGUI_NOBUILD=1 — skipping front-end build."
elif [ "$need_build" = "1" ]; then
  if command -v npm >/dev/null 2>&1; then
    echo "[webgui] building front end (source changed or first run)…"
    ( cd "$FRONTEND" && npm install --no-audit --no-fund && npm run build )
  else
    echo "[webgui] WARNING: npm not found and the front end isn't up to date." >&2
    echo "          Install Node.js 18+ and re-run, or the console will only" >&2
    echo "          serve a stale or 'frontend not built' placeholder." >&2
  fi
else
  echo "[webgui] front end already up to date (webgui/frontend/dist)."
fi

# 2) Stable cookie-signing secret (0600), so restarts don't log everyone out.
mkdir -p "$RUN_DIR"
if [ -z "${SYSIBLE_WEBGUI_SECRET:-}" ]; then
  if [ ! -f "$SECRET_FILE" ]; then
    "$PY" -c "import secrets; print(secrets.token_hex(32))" > "$SECRET_FILE"
    chmod 600 "$SECRET_FILE"
    echo "[webgui] generated cookie secret: $SECRET_FILE"
  fi
  SYSIBLE_WEBGUI_SECRET="$(cat "$SECRET_FILE")"
  export SYSIBLE_WEBGUI_SECRET
fi

# 3) TLS if the controller has its self-signed cert; mark cookies Secure.
TLS_ARGS=()
if [ -f "$CERT_FILE" ] && [ -f "$KEY_FILE" ]; then
  export SYSIBLE_WEBGUI_HTTPS_ONLY=1
  TLS_ARGS=(--ssl-keyfile "$KEY_FILE" --ssl-certfile "$CERT_FILE")
  SCHEME="https"
else
  SCHEME="http"
  echo "[webgui] NOTE: no TLS cert found ($CERT_FILE)." >&2
  echo "          Serving plain HTTP — put this behind a TLS reverse proxy" >&2
  echo "          (nginx/Caddy) before exposing it off localhost." >&2
fi

echo "[webgui] starting on ${SCHEME}://0.0.0.0:${PORT}"
exec "$PY" -m uvicorn webgui.server:app \
  --host 0.0.0.0 --port "$PORT" --log-level info "${TLS_ARGS[@]}"
