#!/bin/bash
set -e

echo "Installing Sysible Controller..."

BASE="/opt/sysible"
VENV="$BASE/venv"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root"
  exit 1
fi

# =========================================================
# DETECT PACKAGE MANAGER / OS FAMILY
# Sysible Controller itself (this installer's own dependencies)
# now supports RHEL/CentOS/Fedora (dnf, falling back to yum on
# older releases), openSUSE/SLES (zypper), and Debian/Ubuntu
# (apt) - the same dnf > yum > zypper > apt-get detection order
# used at runtime for managed-host package commands (see
# client/api.py's _pkgmgr_detect_fragment()), so "which package
# manager does this machine use" is answered the same way
# whether it's the controller host or a host the controller
# manages. Package names differ slightly across the three
# families (e.g. python3-devel vs python3-dev, and Debian/Ubuntu
# splits venv into its own python3-venv package while Fedora/
# RHEL/SUSE bundle it into python3 already), so each branch has
# its own explicit list.
# =========================================================
if command -v dnf >/dev/null 2>&1; then
  PKGMGR=dnf
elif command -v yum >/dev/null 2>&1; then
  PKGMGR=yum
elif command -v zypper >/dev/null 2>&1; then
  PKGMGR=zypper
elif command -v apt-get >/dev/null 2>&1; then
  PKGMGR=apt-get
else
  echo "No supported package manager found (looked for dnf, yum, zypper, apt-get)."
  exit 1
fi

echo "Detected package manager: $PKGMGR"

case "$PKGMGR" in
  dnf|yum)
    "$PKGMGR" install -y \
      python3 \
      python3-pip \
      python3-devel \
      gcc \
      passwd \
      util-linux \
      systemd \
      procps-ng \
      curl \
      sqlite \
      libffi-devel \
      rsync \
      lsof \
      openssl
    ;;
  zypper)
    zypper --non-interactive refresh
    # --non-interactive already auto-confirms; do NOT add -y (an apt/dnf
    # idiom). -y is only a zypper alias since the 1.14 line (SLE/Leap 15),
    # so on older zypper it errors and, under `set -e`, aborts the install.
    zypper --non-interactive install \
      python3 \
      python3-pip \
      python3-devel \
      gcc \
      passwd \
      util-linux \
      systemd \
      procps \
      curl \
      sqlite3 \
      libffi-devel \
      rsync \
      lsof \
      openssl
    ;;
  apt-get)
    apt update

    apt install -y \
      python3 \
      python3-pip \
      python3-venv \
      python3-dev \
      gcc \
      passwd \
      util-linux \
      systemd \
      procps \
      curl \
      sqlite3 \
      libffi-dev \
      rsync \
      lsof \
      openssl
    ;;
esac

# =========================================================
# NODE.JS / npm for building the web console front end.
# The React console ships as source and is compiled to webgui/frontend/dist at
# install time (and rebuilt on update). Without npm the build step below is
# skipped and the web console only serves a "frontend not built" placeholder -
# which is exactly what bit headless boxes like a Raspberry Pi. Install it here.
# Best-effort (package names vary; under `set -e` a missing package must not
# abort the install) - the controller backend works regardless, and Node 18+
# is needed for the Vite build.
# =========================================================
echo "Installing Node.js/npm for the web console front end..."
if ! command -v npm >/dev/null 2>&1; then
  case "$PKGMGR" in
    dnf|yum)  "$PKGMGR" install -y nodejs npm || true ;;
    zypper)   zypper --non-interactive install nodejs npm \
                || zypper --non-interactive install nodejs20 npm20 \
                || zypper --non-interactive install nodejs18 npm18 || true ;;
    apt-get)  apt install -y nodejs npm || true ;;
  esac
fi
if command -v npm >/dev/null 2>&1; then
  echo "Node.js $(node --version 2>/dev/null) / npm $(npm --version 2>/dev/null) available - the web console will be built."
else
  echo "NOTE: Node.js/npm not installed (no distro package found) - the web console"
  echo "      front end can't be built here. Install Node 18+ (e.g. via NodeSource:"
  echo "      https://github.com/nodesource/distributions), then run"
  echo "      'sudo sysible_controller webgui restart' to build and start it."
fi

# =========================================================
# DEPLOY PROJECT FILES TO BASE
# Sysible Controller always runs out of $BASE - the sysible CLI
# hardcodes it. This installer can be run
# from anywhere (e.g. a freshly transferred ~/Documents/sysible_v2
# folder); if it's not already sitting at $BASE, sync it there.
# --delete keeps re-installs (code updates) from leaving stale
# files behind, but the excludes protect anything already
# running: the venv, the live database, the admin API key, logs.
# =========================================================
mkdir -p "$BASE"

if [[ "$SRC_DIR" != "$BASE" ]]; then
  echo "Copying project files from $SRC_DIR to $BASE..."
  rsync -a --delete \
    --exclude venv \
    --exclude run \
    --exclude logs \
    --exclude '*.db' \
    --exclude api_key.txt \
    --exclude hosts.json \
    --exclude remote_keys \
    --exclude agent_state.json \
    --exclude certs \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.git' \
    --exclude node_modules \
    "$SRC_DIR"/ "$BASE"/
fi

# Record the source checkout path so `destroy --purge` can remove it too.
if [[ "$SRC_DIR" != "$BASE" ]]; then
  echo "$SRC_DIR" > "$BASE/.install_src" 2>/dev/null || true
fi

cd "$BASE"

python3 -m venv "$VENV"
source "$VENV/bin/activate"

pip install --upgrade pip
pip install -r "$BASE/requirements.txt"

# =========================================================
# WEB CONSOLE (browser-based, headless-friendly)
# Extra Python deps the BFF needs, plus a production build of the React
# front end so 'sysible_controller start' can serve it immediately.
# Best-effort: if Node isn't present the controller is unaffected - the
# web console self-heals (builds) on its first start instead.
# =========================================================
if [[ -f "$BASE/webgui/requirements.txt" ]]; then
  echo "Installing web console Python dependencies..."
  pip install -r "$BASE/webgui/requirements.txt"
fi
if [[ -d "$BASE/webgui/frontend" ]]; then
  if command -v npm >/dev/null 2>&1; then
    echo "Building web console front end (npm)..."
    ( cd "$BASE/webgui/frontend" && npm install --no-audit --no-fund && npm run build ) \
      || echo "WARNING: web console front-end build failed - it will be retried on first 'sysible_controller webgui start'."
  else
    echo "NOTE: Node.js/npm not found - skipping web console front-end build."
    echo "      Install Node 18+, then run 'sysible_controller webgui start' to build it."
  fi
fi

chmod +x "$BASE/sysible_controller"

# =========================================================
# PROVISION THE TLS CERTIFICATE
# The API used to be plain HTTP, which put the admin API key,
# SSH enrollment passwords, and password hashes on the wire in
# the clear. sysible_controller now requires a cert/key pair to
# launch uvicorn with TLS. There's no public domain on a LAN
# deployment, so a CA-signed cert isn't an option - generate a
# self-signed one instead, scoped to localhost/127.0.0.1 plus
# whatever LAN IP and hostname this machine currently has.
#
# Only generated if missing, so re-running the installer (e.g.
# a code redeploy) never rotates - and breaks - a cert that's
# already been copied out to agents for pinning.
# =========================================================
CERT_DIR="$BASE/certs"
CERT_FILE="$CERT_DIR/server.crt"
KEY_FILE="$CERT_DIR/server.key"

if [[ ! -f "$CERT_FILE" || ! -f "$KEY_FILE" ]]; then
  echo "Generating self-signed TLS certificate..."
  mkdir -p "$CERT_DIR"

  # Every IP `hostname -I` reports, not just the first - Controller
  # Configuration's "All Detected IPs (failover)" address mode can ship
  # an agent bundle pointed at any of this machine's NICs, and TLS
  # hostname/SAN verification (requests' verify=<ca file>) checks
  # whichever address is actually in the request URL, so the cert has
  # to cover every one of them up front or failover to a non-first NIC
  # would otherwise fail verification.
  SAN="DNS:localhost,IP:127.0.0.1,DNS:$(hostname)"
  for ip in $(hostname -I 2>/dev/null); do
    SAN="$SAN,IP:$ip"
  done

  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$KEY_FILE" \
    -out "$CERT_FILE" \
    -days 3650 \
    -subj "/CN=sysible-controller" \
    -addext "subjectAltName=$SAN"

  chmod 600 "$KEY_FILE"
  chmod 644 "$CERT_FILE"
else
  echo "TLS certificate already present, leaving it in place."
fi

# trust.crt is the trust anchor copied out to agents for
# pinning (see backend/tls_manager.py) - distinct from CERT_FILE once an
# externally-issued PKI cert is installed via Sysible Settings, since a
# PKI leaf does not verify against itself the way a self-signed one
# does. For the default self-signed case above, the leaf IS its own
# valid anchor, so just seed trust.crt as a copy of it. Only done if
# missing, same as the cert itself - never clobbers an already-installed
# PKI trust.crt on a redeploy.
TRUST_FILE="$CERT_DIR/trust.crt"
if [[ -f "$CERT_FILE" && ! -f "$TRUST_FILE" ]]; then
  cp -f "$CERT_FILE" "$TRUST_FILE"
  chmod 644 "$TRUST_FILE"
fi

# =========================================================
# PROVISION THE ADMIN API KEY
# Every admin/API endpoint requires this key (X-API-Key header).
# Generate it now (mode 600) so it exists with the right
# permissions before the service is ever started; the backend
# will also auto-generate it on first run if it's missing.
# =========================================================
export PYTHONPATH="$BASE"

python3 -c "from backend.auth import get_or_create_api_key; get_or_create_api_key()"

# =========================================================
# INSTALL THE BACKEND AS A SYSTEMD SERVICE
# Running the API from a foreground script meant it died with
# whatever terminal/session launched it, and didn't come back on its
# own after a crash or reboot. This mirrors the same pattern already
# used for managed hosts' agent (sysible-agent, see
# backend/agent_bundle.py's _systemd_unit()): a real systemd unit,
# restarted automatically, logs to the journal instead of a shell.
#
# Only (re)written if missing or different, so a redeploy never
# clobbers an admin's local edits to the unit file. Enabled but not
# started here - bringing it up is a separate step, via
# `sysible_controller start`.
# =========================================================
SERVICE_FILE="/etc/systemd/system/sysible-backend.service"

NEW_UNIT="[Unit]
Description=Sysible Controller backend
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$BASE
Environment=PYTHONPATH=$BASE
# Bind address for the controller API. Defaults to 0.0.0.0 because remote agents
# connect here — but this port also carries the admin/console surface (behind the
# root-only API key). HARDENING: set SYSIBLE_CONTROLLER_BIND to a specific NIC IP
# before install to bind only that interface, and firewall :9000 to your management
# + agent networks. (Resolved at install time; edit this ExecStart to change later.)
ExecStart=$VENV/bin/uvicorn backend.app:app --host ${SYSIBLE_CONTROLLER_BIND:-0.0.0.0} --port 9000 --ssl-keyfile $KEY_FILE --ssl-certfile $CERT_FILE --log-level info
Restart=always
RestartSec=5
User=root
# Baseline systemd hardening. These directives don't restrict the file
# writes, process exec (systemctl/systemd-run), or networking this service
# needs, so they're safe to ship on by default. Fuller confinement
# (NoNewPrivileges, ProtectSystem=strict, SystemCallFilter, a locked-down
# CapabilityBoundingSet) is recommended but must be validated per-site since
# the controller execs privileged host tooling — see SECURITY.md.
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ProtectClock=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true

[Install]
WantedBy=multi-user.target
"

if [[ ! -f "$SERVICE_FILE" ]] || [[ "$(cat "$SERVICE_FILE")" != "$NEW_UNIT" ]]; then
  echo "Installing systemd service (sysible-backend)..."
  echo "$NEW_UNIT" > "$SERVICE_FILE"
  systemctl daemon-reload
  systemctl enable sysible-backend.service
else
  echo "systemd service already up to date."
fi

# =========================================================
# DEFAULT WEB-CONSOLE ADMIN
# On a fresh install there's no first-run wizard to create the first
# administrator, so the browser console would have nothing to log in with.
# Seed a default superuser (only when NO administrators exist yet) with a
# random password, flagged must-change. Printed once at the end of install.
# =========================================================
DEFAULT_ADMIN_USER="admin"
DEFAULT_ADMIN_PASS=""
# Seed the default superuser. Everything DB-touching (count, password-policy read,
# insert) lives in ONE Python call that catches errors and prints an explicit
# "dberror" instead of dying silently — a silent miss used to be misreported below as
# "admins already exist", which would hide a fresh install left with no console login.
# The password is generated inside that call (with a DB-free fallback) and returned on
# stdout line 2, so it never touches argv (/proc/<pid>/cmdline is world-readable).
SEEDED_ADMIN="dberror"
for _try in $(seq 1 15); do
  _seed_out="$($VENV/bin/python - "$DEFAULT_ADMIN_USER" <<'PY'
import sys, secrets, string
try:
    from backend.db import count_administrators, add_administrator
    from backend import portal_auth
    if count_administrators() != 0:
        print("exists"); sys.exit(0)
    try:
        from backend.policy import generate_compliant_password
        from backend.db import get_admin_password_policy
        pw = generate_compliant_password(get_admin_password_policy())
    except Exception:
        pw = "".join(secrets.choice(string.ascii_letters + string.digits)
                     for _ in range(20)) + "!aA1"
    salt, h = portal_auth.hash_password(pw)
    add_administrator(sys.argv[1], h, salt, must_change_password=1,
                      created_by="installer", role="superuser")
    print("created"); print(pw)
except Exception as e:
    sys.stderr.write("admin seed: %s\n" % e)
    print("dberror")
PY
)"
  _seed_status="$(printf '%s\n' "$_seed_out" | sed -n 1p)"
  if [[ "$_seed_status" == "created" ]]; then
    SEEDED_ADMIN="created"
    DEFAULT_ADMIN_PASS="$(printf '%s\n' "$_seed_out" | sed -n 2p)"
    break
  elif [[ "$_seed_status" == "exists" ]]; then
    SEEDED_ADMIN="exists"
    break
  fi
  sleep 2   # datastore not reachable yet — wait and retry
done

# =========================================================
# INSTALL THE WEB CONSOLE AS ITS OWN SYSTEMD SERVICE
# Separate from the backend, with its own start/stop
# (sysible_controller webgui start|stop). Runs start_webgui.sh, which handles
# the cookie secret, TLS, and a first-run front-end build.
# =========================================================
WEBGUI_SERVICE_FILE="/etc/systemd/system/sysible-webgui.service"
WEBGUI_UNIT="[Unit]
Description=Sysible Web Console
After=network-online.target sysible-backend.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$BASE
Environment=PYTHON=$VENV/bin/python
ExecStart=$BASE/start_webgui.sh
Restart=on-failure
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
"
if [[ ! -f "$WEBGUI_SERVICE_FILE" ]] || [[ "$(cat "$WEBGUI_SERVICE_FILE")" != "$WEBGUI_UNIT" ]]; then
  echo "Installing systemd service (sysible-webgui)..."
  echo "$WEBGUI_UNIT" > "$WEBGUI_SERVICE_FILE"
  systemctl daemon-reload
fi
# Enable the web console unit at install (like the backend), so it comes back on
# reboot even if the admin only ever ran `sysible_controller start`. Enabled, not
# started -- bringing it up stays a separate `sysible_controller webgui start`.
systemctl enable sysible-webgui.service 2>/dev/null || true
chmod +x "$BASE/start_webgui.sh" 2>/dev/null || true

# =========================================================
# INSTALL THE `sysible_controller` CLI
# Single global command (start/stop/restart/status/logs/webgui/destroy)
# - see ./sysible_controller. Named after the product (Sysible
# Controller, one product under the Sysible Enterprise Software
# suite).
# =========================================================
echo "Installing sysible_controller CLI to /usr/local/bin/sysible_controller..."
cp -f "$BASE/sysible_controller" /usr/local/bin/sysible_controller
chmod +x /usr/local/bin/sysible_controller

# =========================================================
# RUNTIME DIR (portal.pid, last_update.{json,log}, web-console secret)
# $BASE is root-owned from this installer running as root; every
# process that writes here - the backend, the web console, and the
# portal - runs as root under systemd, so a plain root-owned run dir
# is all that's needed.
# =========================================================
mkdir -p "$BASE/run"
chmod 750 "$BASE/run"

# Owner-only on the whole install tree. Everything under $BASE (the SQLite DB
# with admin hashes/agent secrets/live tokens, the API key, TLS private key,
# SSH keys, sudo-store key) is read only by the root-run services, so no other
# local account should be able to traverse in and read them.
chmod 700 "$BASE"

# =========================================================
# OPEN THE CONTROLLER'S OWN FIREWALL PORTS
# The agent API (9000) and the web console (8800) must be reachable, but on
# RHEL/Rocky/SLES firewalld is enabled by default and blocks them, so a fresh
# install was silently unreachable until the admin opened the ports by hand.
# Open them here when a firewall is active. Best-effort (never abort the install
# under `set -e`); if no supported firewall is managing the host, just print the
# commands so the admin can open them on whatever they run.
# =========================================================
BACKEND_PORT=9000
CONSOLE_PORT=8800
if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
  echo "Opening firewalld ports ${BACKEND_PORT}/tcp and ${CONSOLE_PORT}/tcp..."
  firewall-cmd --permanent --add-port=${BACKEND_PORT}/tcp >/dev/null 2>&1 || true
  firewall-cmd --permanent --add-port=${CONSOLE_PORT}/tcp >/dev/null 2>&1 || true
  firewall-cmd --reload >/dev/null 2>&1 || true
elif command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi "Status: active"; then
  echo "Opening ufw ports ${BACKEND_PORT}/tcp and ${CONSOLE_PORT}/tcp..."
  ufw allow ${BACKEND_PORT}/tcp >/dev/null 2>&1 || true
  ufw allow ${CONSOLE_PORT}/tcp >/dev/null 2>&1 || true
else
  echo "NOTE: no active firewalld/ufw detected. If a firewall is in front of this host,"
  echo "      open TCP ${BACKEND_PORT} (agent API) and ${CONSOLE_PORT} (web console) manually."
fi

echo ""
echo "Installation complete"
echo "Installed to: $BASE"
echo "Admin API key: $BASE/api_key.txt (mode 600, root-only)"
echo "TLS certificate: $CERT_FILE"
echo ""
echo "Sysible Controller now serves HTTPS only. Copy $CERT_FILE (not server.key) to:"
echo "  - every host you run host_agent/agent.py on"
echo "and point them at it with the SYSIBLE_CA_CERT env var so they can"
echo "verify this controller instead of trusting it blindly."
echo ""
echo "==================================================================="
echo " SERVICES (each started separately):"
echo "   Controller backend : sudo sysible_controller start"
echo "   Web console        : sudo sysible_controller webgui start   ->  https://<this-host>:8800/"
echo "==================================================================="
if [[ "$SEEDED_ADMIN" == "created" ]]; then
  R='\033[1;91m'; Z='\033[0m'   # bold bright red / reset
  echo ""
  echo -e "${R} WEB CONSOLE LOGIN (default admin created for this fresh install):${Z}"
  echo -e "${R}     username:  $DEFAULT_ADMIN_USER${Z}"
  echo -e "${R}     password:  $DEFAULT_ADMIN_PASS${Z}"
  echo -e "${R} Change it after first login (Settings -> My Account). This is shown${Z}"
  echo -e "${R} only once - copy it now.${Z}"
  echo ""
elif [[ "$SEEDED_ADMIN" == "exists" ]]; then
  echo ""
  echo " Administrators already exist, so no default was created. To set a web"
  echo " console login password, run:  sudo sysible_controller reset-admin"
  echo ""
else
  # DB was unreachable while seeding — do NOT claim admins already exist (that would
  # hide a fresh install left with no console login). Tell the operator plainly.
  R='\033[1;91m'; Z='\033[0m'
  echo ""
  echo -e "${R} WARNING: could not create the default web-console admin — the database${Z}"
  echo -e "${R} was not reachable during install. No console login exists yet. Check that${Z}"
  echo -e "${R} the datastore is up and the controller can reach it, then run:${Z}"
  echo -e "${R}     sudo sysible_controller reset-admin${Z}"
  echo ""
fi
echo "Run: sudo sysible_controller start  &&  sudo sysible_controller webgui start"
echo ""
echo "On first start the controller enrolls ITSELF as a managed host (a privileged"
echo "local agent), so it appears in the fleet like any other box. Opt out with"
echo "SYSIBLE_NO_SELF_ENROLL=1, or add it later with: sudo sysible_controller self-enroll"
