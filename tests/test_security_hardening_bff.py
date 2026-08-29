"""BFF hardening regression guards:

  * must_change_password gate on mutating admin-management/config routes
    (pentest: a temporary-credential holder could create a second superuser while
    only the change-password modal was shown);
  * login-throttle X-Forwarded-For fix (rightmost hop, not the spoofable leftmost);
  * CSRF backstop fails closed for a cookie'd mutating request with no Origin/Referer.
"""
import os
import tempfile
import types

os.environ.setdefault("SYSIBLE_API_KEY", "test-hardening-key")
os.environ.setdefault("SYSIBLE_DATA_DIR", tempfile.mkdtemp(prefix="sysible-hardening-"))

from fastapi.testclient import TestClient  # noqa: E402
import webgui.server as w  # noqa: E402

# Same-origin header set so the CSRF backstop lets legitimate mutating calls through.
# https so the Secure session cookie (enterprise-safe default) is carried between calls.
_ORIGIN = {"origin": "https://testserver", "host": "testserver"}


def _client():
    return TestClient(w.app, base_url="https://testserver")


def _login(client, monkeypatch, must_change=False, role="superuser"):
    monkeypatch.setattr(
        w.api, "admin_login",
        lambda u, p: {"role": role, "token": "tok-xyz",
                      "must_change_password": must_change, "sudo_connect": False})
    # Stub the session-revalidation round-trip so require_login doesn't reach a
    # (non-existent) live controller.
    monkeypatch.setattr(w.api, "whoami", lambda: {"username": "temp-admin", "role": role})
    r = client.post("/api/login", json={"username": "temp-admin", "password": "pw"},
                    headers=_ORIGIN)
    assert r.status_code == 200, r.text


# --- must_change_password gate --------------------------------------------------

def test_must_change_blocks_add_admin(monkeypatch):
    client = _client()
    _login(client, monkeypatch, must_change=True)
    r = client.post("/api/admins",
                    json={"username": "attacker", "password": "Xx!12345", "role": "superuser"},
                    headers=_ORIGIN)
    assert r.status_code == 403
    assert "temporary password" in r.text


def test_must_change_blocks_config_write(monkeypatch):
    client = _client()
    _login(client, monkeypatch, must_change=True)
    r = client.post("/api/password-policy", json={"minlen": 8}, headers=_ORIGIN)
    assert r.status_code == 403


def test_must_change_still_allows_credential_change(monkeypatch):
    """The change-credentials route stays reachable so the flag can be cleared —
    it must NOT be blocked by the gate (auth passes; the controller call is stubbed)."""
    client = _client()
    _login(client, monkeypatch, must_change=True)
    monkeypatch.setattr(w.api, "change_admin_credentials",
                        lambda *a, **k: {"username": "temp-admin", "status": "updated"})
    r = client.post("/api/admin/change-credentials",
                    json={"current_password": "pw", "new_password": "New-Passw0rd!"},
                    headers=_ORIGIN)
    # Not the 403 'temporary password' gate — the request is allowed through.
    assert r.status_code != 403 or "temporary password" not in r.text


def test_changed_password_admin_can_add_admin(monkeypatch):
    client = _client()
    _login(client, monkeypatch, must_change=False)
    monkeypatch.setattr(w.api, "add_administrator", lambda *a, **k: {"status": "added"})
    r = client.post("/api/admins",
                    json={"username": "ops2", "password": "Xx!12345", "role": "sysadmin"},
                    headers=_ORIGIN)
    assert r.status_code == 200, r.text


# --- Login throttle: rightmost X-Forwarded-For ---------------------------------

def _fake_request(xff=None, peer="10.9.9.9"):
    headers = {}
    if xff is not None:
        headers["x-forwarded-for"] = xff
    return types.SimpleNamespace(
        headers=types.SimpleNamespace(get=headers.get),
        client=types.SimpleNamespace(host=peer))


def test_client_ip_takes_rightmost_hop_when_trusting_proxy(monkeypatch):
    monkeypatch.setattr(w, "_TRUST_PROXY", True)
    monkeypatch.setattr(w, "_TRUSTED_HOPS", 1)
    # Attacker spoofs a rotating leftmost value; the trusted proxy appended the real
    # peer (1.2.3.4) on the right. The throttle must key on the real peer.
    ip = w._client_ip(_fake_request(xff="9.9.9.9, 1.2.3.4"))
    assert ip == "1.2.3.4"


def test_client_ip_ignores_xff_without_trusted_proxy(monkeypatch):
    monkeypatch.setattr(w, "_TRUST_PROXY", False)
    ip = w._client_ip(_fake_request(xff="9.9.9.9, 1.2.3.4", peer="10.0.0.7"))
    assert ip == "10.0.0.7"


# --- CSRF backstop fails closed for a cookie'd originless mutating request ------

def test_csrf_fails_closed_for_cookied_request_without_origin(monkeypatch):
    client = _client()
    _login(client, monkeypatch, must_change=False)   # establishes the session cookie
    # A mutating POST carrying the session cookie but NO Origin/Referer is refused.
    r = client.post("/api/password-policy", json={"minlen": 8})
    assert r.status_code == 403 and "Cross-origin" in r.text


def test_csrf_allows_cookieless_originless_request():
    # Cookieless tooling (no ambient credential) is not a CSRF vector → allowed
    # through the guard (auth then handles it).
    client = _client()
    r = client.post("/api/password-policy", json={"minlen": 8})
    assert "Cross-origin" not in r.text
