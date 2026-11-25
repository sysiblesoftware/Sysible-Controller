"""
Shared pytest fixtures for the Sysible Controller API test-suite.

The suite drives the real FastAPI apps through Starlette's TestClient — no
network, no live controller — against a throwaway SQLite DB and JSON side-stores
in a temp dir. Environment variables that the apps read AT IMPORT TIME (the API
key, data dir) are set here before the apps are imported.

Run with:  pytest -q            (from the repo root)
Needs:     pip install pytest httpx
"""
import os
import secrets
import tempfile
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# These MUST be set before importing the apps: backend.auth resolves the API
# key once at import, and several modules read the data dir at import.
# ---------------------------------------------------------------------------
_TMP = Path(tempfile.mkdtemp(prefix="sysible-tests-"))
API_KEY = "test-api-key-abcdef"
os.environ["SYSIBLE_API_KEY"] = API_KEY
os.environ["SYSIBLE_DATA_DIR"] = str(_TMP)
os.environ["SYSIBLE_HOSTS_FILE"] = str(_TMP / "hosts.json")
os.environ["SYSIBLE_INTEGRITY_STATE"] = str(_TMP / "integrity.json")
# Keep the login throttle small window so rate-limit tests are fast/deterministic.
os.environ.setdefault("SYSIBLE_WEBGUI_LOGIN_MAX_ATTEMPTS", "8")
# No background loops in a test process. The apps start them AT IMPORT, and
# _isolate below unlinks the SQLite file and its -wal/-shm sidecars before every
# test. Deleting a WAL index under an open connection is explicitly unsafe in
# SQLite: the loop thread keeps the old -shm mmap'd, and reading through that
# stale mapping after the file is recreated raises SIGBUS and takes the whole
# process down -- no failing test, no traceback in the report, just a dead run.
# Seen twice on full runs, at db.py's `PRAGMA journal_mode=WAL` with
# sysible-task-reclaim as the other thread. Nothing here asserts on those loops;
# the tests that cover their work call the functions directly.
os.environ["SYSIBLE_DISABLE_BACKGROUND_LOOPS"] = "1"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import backend.db as db  # noqa: E402

# Redirect the DB to the temp dir. _connect() reads this module global on every
# call, so setting it here reroutes all DB access.
db.DB_PATH = _TMP / "sysible.db"

import backend.app as app_module  # noqa: E402
from backend import portal_auth  # noqa: E402
import backend.remote_routes as remote_routes  # noqa: E402


# ---------------------------------------------------------------------------
# Per-test isolation: a fresh DB, fresh JSON stores and cleared in-memory state
# before every test, so ordering never matters.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolate():
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db.DB_PATH) + suffix)
        if p.exists():
            p.unlink()
    for name in ("hosts.json", "integrity.json", "agent_ssh_state.json"):
        f = _TMP / name
        if f.exists():
            f.unlink()
    db.init_db()
    # Clear module-level in-memory maps that would otherwise leak across tests.
    remote_routes._PTY.clear()
    remote_routes._TERMINAL_SESSIONS.clear()
    app_module._PENDING_BECOME.clear()
    yield


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------
@pytest.fixture
def controller():
    """TestClient for the controller API (backend/app.py)."""
    return TestClient(app_module.app)


@pytest.fixture
def bff():
    """TestClient for the web console BFF (webgui/server.py). Imported lazily so
    a controller-only test run doesn't need the BFF's deps."""
    import webgui.server as srv
    return TestClient(srv.app)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def key_headers(extra=None):
    h = {"X-API-Key": API_KEY}
    if extra:
        h.update(extra)
    return h


@pytest.fixture
def make_admin():
    """Create an administrator of the given role and return a live login token."""
    def _make(username="root-admin", role="superuser", password="Password123!",
              expiry_offset=3600):
        salt, pw_hash = portal_auth.hash_password(password)
        db.add_administrator(username, pw_hash, salt, must_change_password=0, role=role)
        token = secrets.token_hex(16)
        db.create_admin_token(token, username, role, time.time() + expiry_offset)
        return token
    return _make


@pytest.fixture
def superuser_headers(make_admin):
    return key_headers({"X-Sysible-Admin-Token": make_admin("super", "superuser")})


@pytest.fixture
def sysadmin_headers(make_admin):
    return key_headers({"X-Sysible-Admin-Token": make_admin("ops", "sysadmin")})


@pytest.fixture
def auditor_headers(make_admin):
    return key_headers({"X-Sysible-Admin-Token": make_admin("audit", "auditor")})


@pytest.fixture
def enroll_token():
    def _make():
        tok = "enroll-" + secrets.token_hex(12)
        db.create_enroll_token(tok)
        return tok
    return _make


@pytest.fixture
def agent():
    """Seed an enrolled agent directly in the DB; return (host_id, agent_secret)."""
    def _make(host_id="host-1", secret="agent-secret-1", hostname="web1", ip="10.0.0.11"):
        db.create_or_update_agent(host_id, hostname, "linux", "6.1", "online",
                                  time.time(), secret, ip)
        return host_id, secret
    return _make


@pytest.fixture
def ssh_host():
    """Seed an SSH host record in hosts.json; return its name."""
    def _make(name="ssh1", ip="10.0.0.99", user="root"):
        hosts = remote_routes.load_hosts()
        hosts[name] = {"ip": ip, "user": user, "key_path": "/tmp/k", "environment": ""}
        remote_routes.save_hosts(hosts)
        return name
    return _make


# A representative set of hostile string payloads reused across tests.
SQLI_PAYLOADS = [
    "'; DROP TABLE administrators; --",
    "' OR '1'='1",
    "admin'--",
    "1; DELETE FROM agents WHERE '1'='1",
    "\" OR \"\"=\"",
    "'; UPDATE administrators SET role='superuser' WHERE username='x'; --",
]

XSS_PAYLOADS = [
    "<script>alert('xss')</script>",
    "<img src=x onerror=alert(1)>",
    "\"><svg/onload=alert(1)>",
    "javascript:alert(document.cookie)",
    "<iframe src='javascript:alert(1)'></iframe>",
]

UNICODE_PAYLOADS = [
    "héllo-wörld",
    "服务器-01",
    "café☕",
    "Ω≈ç√∫˜µ≤",
    "emoji-🚀-host",
    "zero​width",
]


# ---------------------------------------------------------------------------
# Running a product command against a SIMULATED host
# ---------------------------------------------------------------------------
#
# These suites build a directory of fake tools and run the real shell the
# product would send, so the fakes define what the host "has". That works only
# if the fakes are the ONLY ufw/firewall-cmd/ip on PATH.
#
# They were not. client/_api_dispatch._with_sbin_path prepends
#
#     export PATH="/usr/local/sbin:/usr/sbin:/sbin:$PATH";
#
# to every command bound for a host -- correct on a real host, where admin
# binaries live in sbin and a minimal systemd or runuser PATH omits it. In a
# test it puts the RUNNER'S OWN /usr/sbin/ufw ahead of the fake, so:
#
#   * a host simulated as having ufw drove the runner's real ufw, which
#     answered "Skipping adding existing rule" and exited 0 where the test
#     expected a refusal;
#   * a host simulated as having NO firewall found the runner's real one and
#     never said "No firewall is installed".
#
# It passed on a developer container because that image ships no ufw, no
# firewall-cmd and no ip -- the fakes won by default. 32 tests in this suite
# depended on that accident, and nothing noticed until the suite ran on a
# machine that had the tools.
#
# host_command() keeps the product's export (so what is sent is unchanged, and
# anything asserting on the command string still sees it) and re-pins PATH
# immediately after it, so the simulated host is the only host.
# Nothing by default. Putting /usr/bin on a simulated host's PATH is how
# firewall-cmd leaked back in after /usr/sbin was excluded -- it lives in
# /usr/bin on Debian/Ubuntu. A sandbox that means "this host has no firewall"
# cannot also expose a directory containing one. Tests whose command genuinely
# needs coreutils call link_stock_tools() instead, which puts the named tools
# in the sandbox WITHOUT bringing their whole directory along.
def host_command(command, bindir, extra_dirs=()):
    """The product's command, with the sandbox made authoritative over PATH."""
    from client._api_dispatch import _SBIN_PATH_EXPORT, _with_sbin_path

    prepared = _with_sbin_path(command)
    pin = 'export PATH="%s"; ' % ":".join([str(bindir), *extra_dirs])
    if prepared.startswith(_SBIN_PATH_EXPORT):
        # Straight after the product's own export, so ours has the last word.
        return _SBIN_PATH_EXPORT + pin + prepared[len(_SBIN_PATH_EXPORT):]
    return pin + prepared


# The tools a real host has and a test has no reason to fake. Symlinked INTO the
# sandbox one by one rather than by exposing /usr/bin, so adding a coreutil
# never silently adds a firewall backend beside it.
STOCK_TOOLS = (
    "awk", "basename", "cat", "chmod", "chown", "comm", "cp", "cut", "date",
    "diff", "dirname", "du", "env", "expr", "find", "grep", "head", "id",
    "ln", "ls", "mkdir", "mktemp", "mv", "printf", "readlink", "sed", "seq",
    "sleep", "sort", "stat", "tail", "tee", "test", "touch", "tr", "true",
    "uname", "uniq", "wc", "xargs",
)
# `rm` is deliberately NOT here. In a hosted session it resolves to a guard
# stub rather than coreutils, and a sandbox that quietly linked the real one
# would be reaching around that. No test in this suite has needed it; one that
# does should say so rather than inherit it by default.


def link_stock_tools(bindir, names=STOCK_TOOLS):
    """Symlink the ordinary host tools into a sandbox bin dir.

    Skips any the runner does not have and any the test has already faked --
    a fake must always win over the real tool of the same name.
    """
    import os
    import shutil
    from pathlib import Path

    bindir = Path(bindir)
    linked = []
    for name in names:
        dest = bindir / name
        if dest.exists() or dest.is_symlink():
            continue  # the test faked it; leave the fake alone
        real = shutil.which(name, path="/usr/bin:/bin:/usr/local/bin")
        if not real:
            continue
        os.symlink(real, dest)
        linked.append(name)
    return linked
