"""
Builds the zip file the Webserver Portal hands a logged-in host
operator: host_agent/agent.py plus everything it needs to run against
*this* controller with zero manual configuration - no more setting
SYSIBLE_CONTROLLER/SYSIBLE_ENROLL_TOKEN/SYSIBLE_CA_CERT by hand.

Built entirely in memory (io.BytesIO) - nothing touches disk, so
there's no stale-bundle cleanup to worry about and every download is
generated fresh from whatever Controller Configuration currently says
plus a brand-new one-time enrollment token (see backend/portal_app.py).
"""

import io
import os
import socket
import zipfile
from pathlib import Path

import psutil

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AGENT_SOURCE_FILE = PROJECT_ROOT / "host_agent" / "agent.py"
# host_agent's own requirements file (not the project root one, which also
# lists backend/client dependencies the agent has no use for) - bundled
# into the zip verbatim and installed by run_agent.sh/disenroll_agent.sh,
# so adding a dependency to the agent only ever means updating this one
# file instead of also hand-editing the install scripts below.
AGENT_REQUIREMENTS_FILE = PROJECT_ROOT / "host_agent" / "requirements.txt"
CERT_FILE = Path(os.getenv("SYSIBLE_CERT_FILE", str(PROJECT_ROOT / "certs" / "server.crt")))
# What new agent bundles should pin as their trust anchor - see
# backend/tls_manager.py's module docstring. trust.crt is the issuing
# CA chain when a PKI-issued cert has been installed, or just doesn't
# exist yet on a controller still running its original self-signed
# cert (CERT_FILE is its own valid anchor in that case - see
# _trust_cert_path below).
TRUST_FILE = Path(os.getenv("SYSIBLE_TRUST_FILE", str(PROJECT_ROOT / "certs" / "trust.crt")))

BUNDLE_FILENAME = "sysible-agent-bundle.zip"


def detect_local_ips() -> list[str]:
    """Enumerate this controller's own non-loopback IPv4 addresses across
    every network interface (psutil.net_if_addrs() - already a backend
    dependency, so this adds no new one). Powers both the "pick one IP"
    dropdown in Controller Configuration (GET /controller-config/local-ips)
    and the "all" address_mode below, which skips picking entirely and
    just hands agent bundles every address found here."""
    ips: list[str] = []
    try:
        for addrs in psutil.net_if_addrs().values():
            for addr in addrs:
                if addr.family == socket.AF_INET and not addr.address.startswith("127."):
                    if addr.address not in ips:
                        ips.append(addr.address)
    except Exception:
        pass
    return ips


def resolve_controller_addresses(config: dict) -> list[str]:
    """Turns a controller_config dict (see backend/db.py's
    get_controller_config) into the ordered list of address strings an
    agent bundle should be built against. "all" mode is the odd one out:
    there's no stored address to read because the whole point is to
    avoid the admin having to pick one, so it's resolved fresh here from
    detect_local_ips() every time a bundle is downloaded rather than
    once at save time - if the controller's NICs change later, the next
    download just picks that up instead of shipping a stale list."""
    mode = config.get("address_mode")
    if mode == "all":
        return detect_local_ips()
    if mode == "ip":
        return [config["ip"]] if config.get("ip") else []
    return [config["hostname"]] if config.get("hostname") else []


_CERT_INSTALL_DIR = "/etc/sysible"
_CERT_INSTALL_PATH = f"{_CERT_INSTALL_DIR}/controller.crt"

# Stable, permanent home for the agent on the managed host - distinct
# from the *controller's* own /opt/sysible (api_key.txt, certs/,
# hosts.json - see backend/auth.py, remote_routes.py, install_sysible.sh)
# since on a self-managed controller host both could otherwise collide.
# run_agent.sh below copies agent.py + sysible_agent.env here and points
# the systemd unit at this fixed path, so the service keeps working even
# after the operator deletes the original downloaded/extracted folder.
_AGENT_INSTALL_DIR = "/opt/sysible-agent"

_SERVICE_NAME = "sysible-agent"
_SERVICE_UNIT_PATH = f"/etc/systemd/system/{_SERVICE_NAME}.service"

# The exact fragment in host_agent/agent.py whose default we patch
# below - kept as one constant so the patch and the loud failure if it
# stops matching live next to each other. Was the whole
# `CONTROLLER = os.getenv(...)` line before the multi-IP failover
# feature wrapped that same os.getenv(...) call inside a
# _CONTROLLER_CANDIDATES list comprehension instead - matching just
# this fragment (rather than the full line it now sits inside) means
# this keeps working regardless of what surrounds it.
_CONTROLLER_DEFAULT_LINE = 'os.getenv("SYSIBLE_CONTROLLER", "https://127.0.0.1:9000")'


def _env_file(controller_url: str, token: str, include_cert: bool) -> str:
    lines = [
        f"SYSIBLE_CONTROLLER={controller_url}",
        f"SYSIBLE_ENROLL_TOKEN={token}",
    ]

    if include_cert:
        # Absolute path, not a path relative to the bundle folder -
        # run_agent.sh below actually installs the cert here, so this
        # keeps working regardless of where the agent itself ends up
        # running from (now always _AGENT_INSTALL_DIR, via systemd).
        lines.append(f"SYSIBLE_CA_CERT={_CERT_INSTALL_PATH}")

    return "\n".join(lines) + "\n"


def _systemd_unit() -> str:
    """A managed host running the agent in a foreground terminal used to
    mean: the operator's session shows every "[agent] running task ..."
    line live, and the agent dies the moment that terminal closes
    (unless they remembered nohup/screen/tmux, which nothing here ever
    told them to do). Installing this unit instead means the agent runs
    detached, restarts itself on crash or reboot, and its output goes to
    the journal (`journalctl -u sysible-agent`) instead of an open
    terminal - exactly the "should all be in the background as a
    systemd process" ask."""
    return f"""[Unit]
Description=Sysible host agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile={_AGENT_INSTALL_DIR}/sysible_agent.env
WorkingDirectory={_AGENT_INSTALL_DIR}
ExecStart=/usr/bin/python3 {_AGENT_INSTALL_DIR}/agent.py
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
"""


def _run_script(include_cert: bool) -> str:
    cert_steps = (
        f'echo "Installing controller certificate to {_CERT_INSTALL_PATH}..."\n'
        f"mkdir -p {_CERT_INSTALL_DIR}\n"
        f"cp -f ./server.crt {_CERT_INSTALL_PATH}\n"
        f"chmod 644 {_CERT_INSTALL_PATH}\n\n"
        if include_cert
        else "# NOTE: no TLS cert was bundled (controller has none yet at build\n"
             "# time) - the agent will warn and fall back to default cert\n"
             "# verification, which will likely fail against a self-signed\n"
             "# controller. Re-download once the controller has a cert, or set\n"
             "# SYSIBLE_CA_CERT yourself once you've copied one over.\n\n"
    )

    return f"""#!/bin/bash
# Generated by the Sysible Webserver Portal - run this on the host you
# want managed by Sysible. Installs the agent as a systemd service
# ({_SERVICE_NAME}) that runs in the background, restarts itself on
# crash/reboot, and logs to the journal instead of this terminal.
# Requires Python 3, pip3, and systemd - everything else (see
# requirements.txt) is installed automatically below.
#
# Must be run as root: it writes the controller cert to
# {_CERT_INSTALL_DIR}, installs the agent under {_AGENT_INSTALL_DIR},
# the agent persists its state under /var/lib/sysible, and it registers
# a unit under /etc/systemd/system - all system locations.
set -e
cd "$(dirname "${{BASH_SOURCE[0]}}")"

if [[ "$EUID" -ne 0 ]]; then
  echo "This script needs root (writes to {_CERT_INSTALL_DIR}, {_AGENT_INSTALL_DIR}, /var/lib/sysible, and /etc/systemd/system) - re-run with sudo." >&2
  exit 1
fi

{cert_steps}echo "Installing agent to {_AGENT_INSTALL_DIR}..."
mkdir -p {_AGENT_INSTALL_DIR}
cp -f ./agent.py {_AGENT_INSTALL_DIR}/agent.py
cp -f ./sysible_agent.env {_AGENT_INSTALL_DIR}/sysible_agent.env
chmod 600 {_AGENT_INSTALL_DIR}/sysible_agent.env

# Installed system-wide (not --user): the systemd service below runs as
# root with a minimal environment, where a --user install's reliance on
# $HOME being set the way an interactive sudo shell sets it is one more
# thing that could silently not line up.
echo "Installing Python dependencies (./requirements.txt)..."
if ! pip3 install -r ./requirements.txt; then
  echo "pip3 install failed - retrying with --break-system-packages (newer Debian/Ubuntu blocks system-wide installs by default, PEP 668)..."
  pip3 install --break-system-packages -r ./requirements.txt
fi

echo "Installing systemd service ({_SERVICE_NAME})..."
cp -f ./{_SERVICE_NAME}.service {_SERVICE_UNIT_PATH}
systemctl daemon-reload
systemctl enable --now {_SERVICE_NAME}.service

echo
echo "Agent installed and running in the background as a systemd service."
echo "  Status:  systemctl status {_SERVICE_NAME}"
echo "  Logs:    journalctl -u {_SERVICE_NAME} -f"
echo "  Stop:    systemctl disable --now {_SERVICE_NAME}"
"""


def _disenroll_script() -> str:
    """Cleanly removes the agent from a host: notifies the controller
    (self-disenroll via POST /agents/{host_id}/disenroll, authenticated
    with this host's own host_id+agent_secret straight out of
    agent_state.json - the same credential pair heartbeat()/fetch_tasks()
    already use, never a plaintext password) so the host stops showing
    up enrolled in the GUI, then tears down everything run_agent.sh
    installed: the systemd service, {_AGENT_INSTALL_DIR}, the pinned
    cert, and the agent's local state.

    The controller call happens FIRST, before any local file is
    deleted - agent_state.json is the only copy of the agent_secret
    that proves this host's identity to the controller, so deleting it
    first would leave an orphaned "still enrolled" row behind with no
    way to clean it up short of the admin-only DELETE route."""
    return f"""#!/bin/bash
# Generated by the Sysible Webserver Portal - run this on an enrolled
# host to cleanly remove the agent: tells the controller to drop this
# host's enrollment, then uninstalls everything run_agent.sh installed.
#
# Must be run as root - same locations run_agent.sh wrote to.
set -e
cd "$(dirname "${{BASH_SOURCE[0]}}")"

STATE_FILE="/var/lib/sysible/agent_state.json"
ENV_FILE="{_AGENT_INSTALL_DIR}/sysible_agent.env"
CERT_FILE="{_CERT_INSTALL_PATH}"
UNIT_FILE="{_SERVICE_UNIT_PATH}"

if [[ "$EUID" -ne 0 ]]; then
  echo "This script needs root (same locations run_agent.sh wrote to) - re-run with sudo." >&2
  exit 1
fi

# --------------------------------------------------------
# 1. Notify the controller while we still have credentials to prove
#    who we are. Once STATE_FILE is removed in step 3 there is no way
#    to authenticate a self-disenroll, so this has to happen first -
#    otherwise the controller would keep showing this host as enrolled
#    forever with no local trace left to fix it.
# --------------------------------------------------------
if [[ -f "$STATE_FILE" ]]; then
  echo "Notifying controller..."
  if ! pip3 install -r ./requirements.txt; then
    echo "pip3 install failed - retrying with --break-system-packages (newer Debian/Ubuntu blocks system-wide installs by default, PEP 668)..."
    pip3 install --break-system-packages -r ./requirements.txt
  fi
  python3 - "$STATE_FILE" "$ENV_FILE" "$CERT_FILE" <<'PYEOF'
import json
import os
import sys

state_file, env_file, cert_file = sys.argv[1], sys.argv[2], sys.argv[3]

try:
    with open(state_file) as f:
        state = json.load(f)
except (OSError, json.JSONDecodeError):
    print("[disenroll] no readable state file - skipping controller notification")
    sys.exit(0)

host_id = state.get("host_id")
agent_secret = state.get("agent_secret")
if not host_id or not agent_secret:
    print("[disenroll] state file missing host_id/agent_secret - skipping controller notification")
    sys.exit(0)

controller = "https://127.0.0.1:9000"
try:
    with open(env_file) as f:
        for line in f:
            if line.startswith("SYSIBLE_CONTROLLER="):
                controller = line.strip().split("=", 1)[1]
                break
except OSError:
    pass

verify = cert_file if os.path.exists(cert_file) else True

try:
    import requests
    r = requests.post(
        f"{{controller}}/agents/{{host_id}}/disenroll",
        json={{"host_id": host_id, "agent_secret": agent_secret}},
        verify=verify,
        timeout=10,
    )
    if r.ok:
        print("[disenroll] controller acknowledged - host removed from enrollment")
    else:
        print(f"[disenroll] controller responded {{r.status_code}}: {{r.text}} - continuing with local cleanup anyway")
except Exception as e:
    print(f"[disenroll] could not reach controller ({{e}}) - continuing with local cleanup anyway")
PYEOF
else
  echo "No local agent state found (already disenrolled, or never enrolled) - skipping controller notification."
fi

# --------------------------------------------------------
# 2. Stop and disable the service
# --------------------------------------------------------
if systemctl list-unit-files {_SERVICE_NAME}.service &>/dev/null; then
  echo "Stopping {_SERVICE_NAME}..."
  systemctl disable --now {_SERVICE_NAME}.service 2>/dev/null || true
fi

# --------------------------------------------------------
# 3. Remove everything run_agent.sh installed
# --------------------------------------------------------
echo "Removing installed files..."
rm -f "$UNIT_FILE"
systemctl daemon-reload 2>/dev/null || true
rm -rf {_AGENT_INSTALL_DIR}
rm -f "$CERT_FILE"
rm -f "$STATE_FILE"
rmdir --ignore-fail-on-non-empty /var/lib/sysible 2>/dev/null || true
rmdir --ignore-fail-on-non-empty {_CERT_INSTALL_DIR} 2>/dev/null || true

echo
echo "Agent disenrolled and removed from this host."
"""


def _readme(include_cert: bool) -> str:
    cert_line = (
        "  - server.crt          Controller's TLS certificate (installed to\n"
        f"                         {_CERT_INSTALL_PATH} by run_agent.sh)\n"
        if include_cert
        else ""
    )

    return f"""Sysible Agent Bundle
=====================

Contents:
  - agent.py                  The Sysible host agent
  - requirements.txt          Python packages the agent needs (installed by run_agent.sh)
  - sysible_agent.env         Controller address + your one-time enrollment token
{cert_line}  - {_SERVICE_NAME}.service   systemd unit installed by run_agent.sh
  - run_agent.sh               Installs the agent + service with the above pre-configured
  - disenroll_agent.sh         Cleanly removes the agent from this host (see below)

Quick start:
  1. Copy this whole folder to the host you want managed.
  2. chmod +x run_agent.sh
  3. sudo ./run_agent.sh

This installs the agent under {_AGENT_INSTALL_DIR} and runs it as a
systemd service ({_SERVICE_NAME}) that starts in the background, restarts
itself automatically (on crash or reboot), and logs to the journal
instead of your terminal. Once it's running you can disconnect or close
this terminal - the agent keeps going.

  Check on it:   systemctl status {_SERVICE_NAME}
  Watch its log: journalctl -u {_SERVICE_NAME} -f
  Stop for good: systemctl disable --now {_SERVICE_NAME}

To remove this host from Sysible entirely (not just stop it - also
tells the controller to drop the enrollment so it stops showing up in
the GUI):
  4. sudo ./disenroll_agent.sh

Root is required: the script installs the controller's certificate to
{_CERT_INSTALL_DIR}, the agent itself under {_AGENT_INSTALL_DIR}, the
service unit under /etc/systemd/system, and the agent persists its
state under /var/lib/sysible - all system directories.

The enrollment token in sysible_agent.env is single-use and expires in
365 days - if you didn't use it, generate a fresh download from the
portal rather than reusing this folder on a second host.
"""


def _patch_agent_controller_default(agent_source: str, controller_url: str) -> str:
    """Bake the resolved controller URL into agent.py's own default
    (still overridable by SYSIBLE_CONTROLLER) - so the agent points at
    the right controller even if its EnvironmentFile (sysible_agent.env)
    is ever missing or not picked up, e.g. someone runs
    `python3 agent.py` by hand outside of the systemd unit run_agent.sh
    installs.

    controller_url may itself be several comma-joined candidate URLs
    (the "all" address_mode case) - that's fine here, since this just
    swaps it in as the os.getenv() fallback string verbatim, and
    agent.py's own _CONTROLLER_CANDIDATES parsing (the code right after
    this fragment) is what actually splits a comma-joined default back
    apart, exactly the same as it would for the env var itself."""
    if _CONTROLLER_DEFAULT_LINE not in agent_source:
        raise RuntimeError(
            "host_agent/agent.py's CONTROLLER default fragment has changed - "
            "update backend/agent_bundle.py's _CONTROLLER_DEFAULT_LINE/"
            "_patch_agent_controller_default to match."
        )

    patched_fragment = f'os.getenv("SYSIBLE_CONTROLLER", "{controller_url}")'

    return agent_source.replace(_CONTROLLER_DEFAULT_LINE, patched_fragment, 1)


def _trust_cert_path():
    """The trust anchor to hand new agent bundles - trust.crt if a cert
    has been installed via Sysible Settings' TLS section (covers both
    the default self-signed case, where install_sysible.sh/tls_manager
    just seed it as a copy of server.crt, and a PKI-issued cert, where
    it's the actual issuing CA chain), falling back to server.crt
    directly for a controller that predates trust.crt entirely. Returns
    None if neither file exists yet (no cert at all, e.g. mid-install)."""
    if TRUST_FILE.exists():
        return TRUST_FILE
    if CERT_FILE.exists():
        return CERT_FILE
    return None


def build_agent_bundle(controller_addresses, controller_port: int, token: str):
    """Returns (filename, zip_bytes). `controller_addresses` is either a
    single hostname/IP string (the normal "hostname"/"ip" address_mode)
    or a list of addresses ("all" mode - see resolve_controller_addresses
    above): every address becomes its own https://host:port URL, joined
    with commas into one SYSIBLE_CONTROLLER value that
    host_agent/agent.py's failover logic splits back apart and tries in
    order until one connects. Raises FileNotFoundError if
    host_agent/agent.py or host_agent/requirements.txt is missing
    (shouldn't happen in a normal checkout, but fail loudly rather than
    shipping an incomplete bundle)."""

    agent_source = AGENT_SOURCE_FILE.read_text()
    requirements_text = AGENT_REQUIREMENTS_FILE.read_text()

    if isinstance(controller_addresses, str):
        controller_addresses = [controller_addresses]
    if not controller_addresses:
        raise ValueError("build_agent_bundle: no controller address(es) to build against")

    controller_url = ",".join(f"https://{addr}:{controller_port}" for addr in controller_addresses)

    agent_source = _patch_agent_controller_default(agent_source, controller_url)

    trust_path = _trust_cert_path()
    include_cert = trust_path is not None

    buf = io.BytesIO()

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("agent.py", agent_source)
        zf.writestr("requirements.txt", requirements_text)
        zf.writestr("sysible_agent.env", _env_file(controller_url, token, include_cert))

        zf.writestr(f"{_SERVICE_NAME}.service", _systemd_unit())

        run_script = _run_script(include_cert)
        zf.writestr("run_agent.sh", run_script)
        # zipfile doesn't preserve unix permissions by default - set
        # the executable bit explicitly so it doesn't need a manual
        # chmod after extracting (the README still mentions it too,
        # in case the host's unzip tool drops external_attr anyway).
        info = zf.getinfo("run_agent.sh")
        info.external_attr = (0o755 & 0xFFFF) << 16

        zf.writestr("disenroll_agent.sh", _disenroll_script())
        info = zf.getinfo("disenroll_agent.sh")
        info.external_attr = (0o755 & 0xFFFF) << 16

        if include_cert:
            # Zip entry is still named server.crt - that's the filename
            # run_agent.sh's cp step (see _run_script above) installs to
            # _CERT_INSTALL_PATH for the agent to pin; only the source
            # of its *content* changes here (trust.crt's content, not
            # necessarily server.crt's).
            zf.writestr("server.crt", trust_path.read_text())

        zf.writestr("README.txt", _readme(include_cert))

    return BUNDLE_FILENAME, buf.getvalue()
