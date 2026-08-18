#!/usr/bin/env bash
# Sysible Controller container entrypoint — idempotent first-run bootstrap.
#
# Everything below is safe to run on EVERY start: it only creates what is
# missing on the /data volume, so restarts and image upgrades never rotate the
# TLS cert (which agents pin) or reset the database. After bootstrap it execs
# the CMD (supervisord), which runs the backend + web console.
set -euo pipefail

DATA="${SYSIBLE_DATA_DIR:-/data}"
CERT_DIR="${SYSIBLE_CERT_DIR:-$DATA/certs}"
RUN_DIR="${SYSIBLE_RUN_DIR:-$DATA/run}"
PORTAL_DIR="${SYSIBLE_PORTAL_FILES_DIR:-$DATA/portal_files}"
CERT_FILE="$CERT_DIR/server.crt"
KEY_FILE="$CERT_DIR/server.key"
TRUST_FILE="$CERT_DIR/trust.crt"

log() { echo "[sysible-entrypoint] $*"; }

# 1) Persistent directory layout on the volume.
mkdir -p "$CERT_DIR" "$RUN_DIR" "$PORTAL_DIR/uploads" "$PORTAL_DIR/downloads"

# 2) Self-signed TLS certificate (only if absent — never rotate a pinned cert).
#    SAN covers localhost + the container hostname, plus any public DNS/IPs the
#    operator lists in SYSIBLE_CONTROLLER_HOSTNAMES (comma-separated) so agents
#    reaching the controller by its host address pass verification. More can be
#    added later from the web console (Settings -> Regenerate certificate).
if [[ ! -f "$CERT_FILE" || ! -f "$KEY_FILE" ]]; then
  log "generating self-signed TLS certificate..."
  SAN="DNS:localhost,IP:127.0.0.1,DNS:$(hostname)"
  # container's own IP(s)
  for ip in $(hostname -i 2>/dev/null || true); do
    case "$ip" in *:*) SAN="$SAN,IP:$ip";; *.*) SAN="$SAN,IP:$ip";; esac
  done
  # operator-supplied public hostnames / IPs
  if [[ -n "${SYSIBLE_CONTROLLER_HOSTNAMES:-}" ]]; then
    IFS=',' read -ra _extra <<< "$SYSIBLE_CONTROLLER_HOSTNAMES"
    for h in "${_extra[@]}"; do
      h="$(echo "$h" | xargs)"; [[ -z "$h" ]] && continue
      if [[ "$h" =~ ^[0-9.]+$ || "$h" == *:* ]]; then SAN="$SAN,IP:$h"; else SAN="$SAN,DNS:$h"; fi
    done
  fi
  log "certificate SAN: $SAN"
  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$KEY_FILE" -out "$CERT_FILE" -days 3650 \
    -subj "/CN=sysible-controller" -addext "subjectAltName=$SAN"
  chmod 600 "$KEY_FILE"; chmod 644 "$CERT_FILE"
else
  log "TLS certificate already present — leaving it in place."
fi
# trust anchor for agent pinning (self-signed leaf is its own anchor).
[[ -f "$CERT_FILE" && ! -f "$TRUST_FILE" ]] && { cp -f "$CERT_FILE" "$TRUST_FILE"; chmod 644 "$TRUST_FILE"; }

# 3) API key (backend <-> BFF/CLI). Honors SYSIBLE_API_KEY if set; else file.
python3 -c "from backend.auth import get_or_create_api_key; get_or_create_api_key()" \
  && log "API key ready ($SYSIBLE_API_KEY_FILE)."

# 4) Stable web-console cookie-signing secret (survives restarts, so sessions
#    aren't invalidated on every bounce). Exported for the webgui process.
SECRET_FILE="$RUN_DIR/webgui.secret"
if [[ -z "${SYSIBLE_WEBGUI_SECRET:-}" ]]; then
  if [[ ! -f "$SECRET_FILE" ]]; then
    python3 -c "import secrets; print(secrets.token_hex(32))" > "$SECRET_FILE"
    chmod 600 "$SECRET_FILE"
    log "generated web-console cookie secret."
  fi
  export SYSIBLE_WEBGUI_SECRET="$(cat "$SECRET_FILE")"
fi
# TLS is on, so the session cookie must be marked Secure.
export SYSIBLE_WEBGUI_HTTPS_ONLY=1

# 5) Initialise the DB and seed the first superuser if none exists. The password
#    is taken from SYSIBLE_ADMIN_PASSWORD if provided, otherwise generated and
#    printed ONCE here (never passed on argv). must_change_password forces a
#    reset at first login. Mirrors install_sysible.sh's seed logic.
ADMIN_USER="${SYSIBLE_ADMIN_USERNAME:-admin}"
seed_out="$(python3 - "$ADMIN_USER" "${SYSIBLE_ADMIN_PASSWORD:-}" <<'PY'
import sys, secrets, string
user = sys.argv[1]
supplied = sys.argv[2] if len(sys.argv) > 2 else ""
try:
    from backend.db import count_administrators, add_administrator  # imports run init_db()
    from backend import portal_auth
    if count_administrators() != 0:
        print("exists"); sys.exit(0)
    pw = supplied
    if not pw:
        try:
            from backend.policy import generate_compliant_password
            from backend.db import get_admin_password_policy
            pw = generate_compliant_password(get_admin_password_policy())
        except Exception:
            pw = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20)) + "!aA1"
    salt, h = portal_auth.hash_password(pw)
    # If the operator supplied the password, don't force a change; otherwise do.
    add_administrator(user, h, salt, must_change_password=(0 if supplied else 1),
                      created_by="container-entrypoint", role="superuser")
    print("created"); print(pw)
except Exception as e:
    sys.stderr.write("admin seed error: %s\n" % e)
    print("dberror")
PY
)"
status="$(printf '%s\n' "$seed_out" | sed -n 1p)"
case "$status" in
  created)
    pw="$(printf '%s\n' "$seed_out" | sed -n 2p)"
    if [[ -n "${SYSIBLE_ADMIN_PASSWORD:-}" ]]; then
      log "seeded superuser '$ADMIN_USER' (password from SYSIBLE_ADMIN_PASSWORD)."
    else
      log "============================================================"
      log " Seeded initial superuser — CHANGE THIS PASSWORD AT LOGIN:"
      log "     username: $ADMIN_USER"
      log "     password: $pw"
      log " (shown once; set SYSIBLE_ADMIN_PASSWORD to choose your own)"
      log "============================================================"
    fi ;;
  exists)  log "administrators already exist — not seeding." ;;
  *)       log "WARNING: could not seed the initial admin (see error above); the console may have no login yet." ;;
esac

log "bootstrap complete — starting services (backend :${SYSIBLE_BACKEND_PORT:-9000}, web console :${SYSIBLE_WEBGUI_PORT:-8800})."
exec "$@"
