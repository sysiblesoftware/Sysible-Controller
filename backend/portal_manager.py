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
    url = f"https://127.0.0.1:{port}/health"

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

    return {
        "running": running,
        "port": _read_running_port() if running else None,
        "configured_port": _configured_port(),
        "pid": pid if running else None,
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
            "--host", "0.0.0.0",
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
        return {"running": True, "port": port, "configured_port": _configured_port(), "pid": proc.pid}

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
            "error": (
                f"Portal process exited immediately (code {exit_code}). "
                f"Last log lines:\n{_log_tail()}"
            ),
        }

    # Still alive, just slow to answer /health (e.g. a sluggish first
    # import) - don't punish a slow-starting process by killing it.
    return {"running": True, "port": port, "configured_port": _configured_port(), "pid": proc.pid}


def stop():
    pid = _read_pid()

    if not _is_alive(pid):
        PORTAL_PID_FILE.unlink(missing_ok=True)
        PORTAL_PORT_FILE.unlink(missing_ok=True)
        return {"running": False, "port": None, "configured_port": _configured_port(), "pid": None}

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
