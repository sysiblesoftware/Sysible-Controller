"""
Start/stop/status control for the Webserver Portal.

The portal is a deliberately separate process on its own port rather
than routes bolted onto backend.app:app, so "Start"/"Stop" in the GUI
means something real - the portal should not be reachable at all while
stopped, not just gated by a flag an unauthenticated request could
still probe.

It serves HTTPS using the same self-signed cert/key the controller
itself uses (see CERT_FILE/KEY_FILE below, and install_sysible.sh
for how that cert is generated) - a remote host
operator's browser will show the standard untrusted-certificate
warning the first time, same as visiting the controller's own HTTPS
port directly would, since they have nothing to pin against yet
(getting them that cert is literally part of what logging in and
downloading a bundle does). That's an accepted click-through, not a
gap: it still protects the portal login password and the bundle/files
in transit from passive network snooping, which plain HTTP did not.

PID-file tracked under run/portal.pid, same convention
`sysible_controller` already uses for the backend/client processes. The
actual bound port of a *running* process is tracked in a separate
sidecar file (run/portal.port) - distinct from the "configured" port in
the database, since changing the configured port while the portal is
already running shouldn't retroactively change what a live process
reports; that only takes effect on the next Start.
"""

import os
import signal
import ssl
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

from backend.db import get_controller_config, get_portal_config

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_PORTAL_PORT = int(os.getenv("SYSIBLE_PORTAL_PORT", "8090"))

# Bind address for the self-service onboarding portal. It defaults to 0.0.0.0
# because its whole purpose is to hand agent bundles to hosts being onboarded over
# the network — but on a multi-homed / segmented controller an operator can pin it
# to a single management interface (e.g. 10.0.0.5), or to 127.0.0.1 to serve it only
# behind a reverse proxy. The portal is TLS + login-gated (session cookie or
# Basic-auth with brute-force lockout) and every bundle carries a fresh SINGLE-USE,
# host-capped enrollment token, so this narrows the network attack surface rather
# than closing an auth hole.
DEFAULT_PORTAL_HOST = (os.getenv("SYSIBLE_PORTAL_HOST", "0.0.0.0") or "0.0.0.0").strip()

# The portal port this deployment PUBLISHES, when it runs in a container. A
# container's port publishing is fixed when the container is created, so a port
# the operator picks later in the console cannot be reached from outside until
# the container is recreated — and the portal binding it successfully inside the
# container tells you nothing about that. docker-compose.yml sets this to the
# port it maps; an image created before the portal was published leaves it unset,
# which means nothing outside can reach the portal at all.
PUBLISHED_PORT_ENV = "SYSIBLE_PORTAL_PORT"

RUN_DIR = Path(os.getenv("SYSIBLE_RUN_DIR", str(PROJECT_ROOT / "run")))
PORTAL_PID_FILE = RUN_DIR / "portal.pid"
PORTAL_PORT_FILE = RUN_DIR / "portal.port"

LOG_DIR = Path(os.getenv("SYSIBLE_LOG_DIR", str(PROJECT_ROOT / "logs")))
PORTAL_LOG_FILE = LOG_DIR / "portal.log"

# Same cert/key the controller's own HTTPS listener uses (see
# install_sysible.sh) - SYSIBLE_CERT_FILE matches the env var
# backend/agent_bundle.py already reads, so one override covers both.
CERT_FILE = Path(os.getenv("SYSIBLE_CERT_FILE", str(PROJECT_ROOT / "certs" / "server.crt")))
KEY_FILE = Path(os.getenv("SYSIBLE_KEY_FILE", str(PROJECT_ROOT / "certs" / "server.key")))

STARTUP_TIMEOUT_S = 5
STARTUP_POLL_INTERVAL_S = 0.2


def _is_container() -> bool:
    """Whether this controller runs in a container. The image sets
    SYSIBLE_CONTAINER=1; /.dockerenv is the fallback (same test as
    backend/app.py and backend/agent_bundle.py)."""
    if os.getenv("SYSIBLE_CONTAINER") == "1":
        return True
    try:
        return os.path.exists("/.dockerenv")
    except OSError:
        return False


def unreachable_reason(port):
    """Why a RUNNING portal still cannot be reached at the address the console
    advertises — or None when there is no reason to think it can't.

    The portal is a separate listener on its own port. In a container it binds
    inside the container, and the start-up health check that proves it is alive
    runs on container-loopback, so "Running" was reported for a portal the
    network could never reach: the console printed
    "Reachable at https://<controller>:8090" and an nmap of that port from the
    LAN said `closed`. Nothing in the product said why, because nothing looked.

    This is decided from what the container was CREATED with, not from a probe,
    so it is certain rather than a guess about the network.
    """
    if not _is_container():
        return None

    published = (os.getenv(PUBLISHED_PORT_ENV) or "").strip()
    if not published:
        return (
            "This controller runs in a container that does not publish the "
            "portal's port, so nothing outside the container can reach it — the "
            "portal is listening, but only inside the container. Update the "
            "controller and recreate the container (sysible_ctl controller "
            "update) to pick up a compose file that publishes it."
        )

    try:
        published_port = int(published)
    except ValueError:
        return None

    if published_port != port:
        return (
            f"This controller runs in a container that publishes port "
            f"{published_port} for the portal, but the portal is set to port "
            f"{port}. A container's published ports are fixed when it is created, "
            f"so port {port} cannot be reached from outside. Either set the portal "
            f"back to {published_port}, or set SYSIBLE_PORTAL_PORT={port} in the "
            f"controller's .env and recreate the container."
        )

    return None


def _configured_port():
    """The port the GUI has configured (Webserver Portal Configuration) -
    used the *next* time the portal is started. Not necessarily the port
    a currently-running process is actually bound to."""
    try:
        return int(get_portal_config().get("port") or DEFAULT_PORTAL_PORT)
    except Exception:
        return DEFAULT_PORTAL_PORT


def _read_pid():
    if not PORTAL_PID_FILE.exists():
        return None

    try:
        return int(PORTAL_PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None


def _read_running_port():
    if not PORTAL_PORT_FILE.exists():
        return None

    try:
        return int(PORTAL_PORT_FILE.read_text().strip())
    except (ValueError, OSError):
        return None


def _is_alive(pid):
    if pid is None:
        return False

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, just owned by someone else - treat as alive.
        return True


def _log_tail(n=20):
    try:
        lines = PORTAL_LOG_FILE.read_text().splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return ""


def _wait_for_health(port, deadline):
    # Probe the loopback when bound to all interfaces (the common case); otherwise
    # probe the exact bind address, since a specific-interface bind may not answer
    # on 127.0.0.1.
    probe_host = "127.0.0.1" if DEFAULT_PORTAL_HOST in ("0.0.0.0", "", "::") else DEFAULT_PORTAL_HOST
    url = f"https://{probe_host}:{port}/health"

    # Verify against the same cert we just told uvicorn to serve,
    # rather than skipping verification - this doubles as a check that
    # what's listening is actually using the cert we expect (mirrors
    # `sysible_controller`'s `curl --cacert` check for the main API).
    ctx = ssl.create_default_context(cafile=str(CERT_FILE))

    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1, context=ctx) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError, TimeoutError):
            pass

        time.sleep(STARTUP_POLL_INTERVAL_S)

    return False


def status():
    pid = _read_pid()
    running = _is_alive(pid)

    if not running:
        # Stale PID/port files left behind by a crash or unclean stop -
        # clean them up so a later start() doesn't get confused.
        PORTAL_PID_FILE.unlink(missing_ok=True)
        PORTAL_PORT_FILE.unlink(missing_ok=True)
        pid = None

    live_port = _read_running_port() if running else None
    return {
        "running": running,
        "port": live_port,
        "configured_port": _configured_port(),
        "pid": pid if running else None,
        # Why the advertised address still will not answer, when that is knowable.
        # None means there is no known reason it cannot be reached.
        "unreachable_reason": unreachable_reason(live_port) if running and live_port else None,
    }


def start(port=None):
    current = status()

    if current["running"]:
        return current

    if not CERT_FILE.exists() or not KEY_FILE.exists():
        return {
            "running": False,
            "port": None,
            "configured_port": _configured_port(),
            "pid": None,
            "unreachable_reason": None,
            "error": (
                f"TLS certificate not found at {CERT_FILE} / {KEY_FILE} - "
                "the portal requires the same cert the controller uses. "
                "Run install_sysible.sh to generate one, then retry."
            ),
        }

    if not get_controller_config().get("configured"):
        # Without this, the portal happily starts and silently bakes
        # this machine's own (often unreachable, e.g. a .local mDNS
        # name) hostname into every agent bundle it hands out - a host
        # operator just gets a bundle that can never reach the
        # controller, with nothing in the UI explaining why. Fail loud
        # here instead, before the portal is even reachable.
        return {
            "running": False,
            "port": None,
            "configured_port": _configured_port(),
            "pid": None,
            "unreachable_reason": None,
            "error": (
                "Controller Configuration hasn't been set yet. Open "
                "Sysible Controller Configuration, set a Hostname or IP "
                "Address every managed host can reach this controller "
                "at, and Save - then start the portal."
            ),
        }

    port = port or _configured_port()

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    log_fh = open(PORTAL_LOG_FILE, "a")

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "backend.portal_app:app",
            "--host", DEFAULT_PORTAL_HOST,
            "--port", str(port),
            "--ssl-keyfile", str(KEY_FILE),
            "--ssl-certfile", str(CERT_FILE),
            "--log-level", "info",
        ],
        cwd=str(PROJECT_ROOT),
        env=os.environ.copy(),
        stdout=log_fh,
        stderr=log_fh,
        start_new_session=True,
    )

    PORTAL_PID_FILE.write_text(str(proc.pid))
    PORTAL_PORT_FILE.write_text(str(port))

    deadline = time.time() + STARTUP_TIMEOUT_S
    healthy = _wait_for_health(port, deadline)

    if healthy:
        # Healthy means it answered on THIS machine's loopback. In a container
        # that is the container's loopback, which says nothing about whether the
        # network the console advertises can reach it.
        return {"running": True, "port": port, "configured_port": _configured_port(),
                "pid": proc.pid, "unreachable_reason": unreachable_reason(port)}

    if proc.poll() is not None:
        # Process actually exited - this is a real failure, not just a
        # slow start. Clean up so status() doesn't lie about it.
        exit_code = proc.poll()
        PORTAL_PID_FILE.unlink(missing_ok=True)
        PORTAL_PORT_FILE.unlink(missing_ok=True)

        return {
            "running": False,
            "port": None,
            "configured_port": _configured_port(),
            "pid": None,
            "unreachable_reason": None,
            "error": (
                f"Portal process exited immediately (code {exit_code}). "
                f"Last log lines:\n{_log_tail()}"
            ),
        }

    # Still alive, just slow to answer /health (e.g. a sluggish first
    # import) - don't punish a slow-starting process by killing it.
    return {"running": True, "port": port, "configured_port": _configured_port(),
            "pid": proc.pid, "unreachable_reason": unreachable_reason(port)}


def stop():
    pid = _read_pid()

    if not _is_alive(pid):
        PORTAL_PID_FILE.unlink(missing_ok=True)
        PORTAL_PORT_FILE.unlink(missing_ok=True)
        return {"running": False, "port": None, "configured_port": _configured_port(),
                "pid": None, "unreachable_reason": None}

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass

    # Give it a moment to actually exit before reporting success, same
    # as `sysible_controller stop`'s plain `kill` (no forced wait) - but a short
    # grace period here means the GUI's very next status() check
    # doesn't show a contradictory "still running".
    for _ in range(20):
        if not _is_alive(pid):
            break
        time.sleep(0.1)

    PORTAL_PID_FILE.unlink(missing_ok=True)
    PORTAL_PORT_FILE.unlink(missing_ok=True)

    return {"running": False, "port": None, "configured_port": _configured_port(), "pid": None}
