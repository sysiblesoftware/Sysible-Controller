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
