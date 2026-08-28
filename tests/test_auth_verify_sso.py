"""SSO auth-probe endpoint on the web console BFF (GET /api/auth/verify).

SLOP's gateway calls this via Caddy `forward_auth` before proxying to any fronted
app: a 200 lets the request through (and copies X-Sysible-User/X-Sysible-Role onto
the upstream request); a 401 makes the gateway redirect the browser to the login.
The critical, security-relevant behavior is the 401 for an unauthenticated caller —
that is the gate. The 200 path must carry the identity headers so a fronted app can
trust the forwarded user/role. See Sysible-Linux-Operations-Platform/docs/SSO.md.
"""
import os
import tempfile

os.environ.setdefault("SYSIBLE_API_KEY", "test-authverify-key")
os.environ.setdefault("SYSIBLE_DATA_DIR", tempfile.mkdtemp(prefix="sysible-authverify-"))

from fastapi.testclient import TestClient  # noqa: E402
import webgui.server as w  # noqa: E402

# https base_url so the Secure session cookie (the enterprise-safe default) is
# carried between the login and the probe, mirroring a real TLS-fronted console.
client = TestClient(w.app, base_url="https://testserver")


def test_unauthenticated_probe_is_401():
    # No session cookie → the gate must deny, so the gateway redirects to login.
    r = client.get("/api/auth/verify")
    assert r.status_code == 401
    # A denied probe must never leak an identity header.
    assert "X-Sysible-User" not in r.headers
    assert "X-Sysible-Role" not in r.headers


def test_authenticated_probe_returns_identity_headers(monkeypatch):
    # Establish a real signed session the way the console does: POST /api/login,
    # with the controller round-trip stubbed to accept the credentials.
    monkeypatch.setattr(
        w.api, "admin_login",
        lambda username, password: {"role": "sysadmin", "token": "tok-abc"},
    )
    lr = client.post("/api/login", json={"username": "verify-user", "password": "pw"})
    assert lr.status_code == 200, lr.text

    # TestClient carries the session cookie forward to the probe.
    r = client.get("/api/auth/verify")
    assert r.status_code == 200
    assert r.headers.get("X-Sysible-User") == "verify-user"
    assert r.headers.get("X-Sysible-Role") == "sysadmin"
    # The decision must not be cached by any intermediary proxy.
    assert r.headers.get("Cache-Control") == "no-store"
    body = r.json()
    assert body == {"user": "verify-user", "role": "sysadmin"}


def test_probe_is_a_safe_get_not_gated_by_csrf():
    # forward_auth issues a plain GET with the browser's cookies but no Origin; the
    # CSRF backstop only guards mutating methods, so the probe must not be refused.
    r = client.get("/api/auth/verify", headers={"origin": "https://slop.lan"})
    assert r.status_code in (200, 401)  # never 403 Cross-origin
    assert "Cross-origin" not in r.text
