"""Controller BFF — trust the SLOP gateway-asserted identity (SSO mode).

When the console runs behind the SLOP gateway (SYSIBLE_WEBGUI_TRUST_SSO=1), the
gateway authenticates the browser against SLOP and asserts the identity as
request headers, stamped with a shared secret that proves the request came
through the gateway. The console then provisions a backend token for that user
and treats them as signed in — no local login. These tests pin the trust
boundary: the identity is honored ONLY with the correct secret, and never when
SSO mode is off. See Sysible-Linux-Operations-Platform/docs/SSO.md.
"""
import os
import tempfile

os.environ.setdefault("SYSIBLE_API_KEY", "test-sso-key")
os.environ.setdefault("SYSIBLE_DATA_DIR", tempfile.mkdtemp(prefix="sysible-sso-"))

from fastapi.testclient import TestClient  # noqa: E402
import webgui.server as w  # noqa: E402

SECRET = "sso-shared-secret-xyz"


def _client():
    # Fresh client per test → no session cookie carried in from another test.
    return TestClient(w.app, base_url="https://testserver")


def _enable(monkeypatch, provisioned_role="sysadmin"):
    monkeypatch.setattr(w, "_TRUST_SSO", True)
    monkeypatch.setattr(w, "_SSO_SECRET", SECRET)
    calls = {"n": 0}

    def fake_provision(username, role):
        calls["n"] += 1
        return {"username": username, "role": provisioned_role,
                "token": "sso-tok-123", "sudo_connect": False}

    monkeypatch.setattr(w.api, "sso_provision", fake_provision)
    return calls


def _gw_headers(user="alice", role="operator"):
    return {"X-Sysible-User": user, "X-Sysible-Role": role, "X-Sysible-Auth": SECRET}


def test_verify_honors_gateway_identity(monkeypatch):
    _enable(monkeypatch)
    r = _client().get("/api/auth/verify", headers=_gw_headers("alice", "operator"))
    assert r.status_code == 200, r.text
    assert r.headers.get("X-Sysible-User") == "alice"
    # operator maps to the controller's sysadmin role (what provisioning returns).
    assert r.headers.get("X-Sysible-Role") == "sysadmin"


def test_me_reflects_gateway_identity(monkeypatch):
    _enable(monkeypatch)
    r = _client().get("/api/me", headers=_gw_headers("bob", "superuser"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["username"] == "bob"
    assert body["role"] == "sysadmin"          # from the mocked provision result
    assert body["must_change_password"] is False


def test_wrong_secret_is_ignored(monkeypatch):
    _enable(monkeypatch)
    h = _gw_headers("mallory", "superuser")
    h["X-Sysible-Auth"] = "not-the-secret"
    r = _client().get("/api/auth/verify", headers=h)
    assert r.status_code == 401  # a bad secret must not authenticate anyone


def test_missing_secret_is_ignored(monkeypatch):
    _enable(monkeypatch)
    h = _gw_headers("mallory", "superuser")
    del h["X-Sysible-Auth"]
    r = _client().get("/api/auth/verify", headers=h)
    assert r.status_code == 401


def test_trust_mode_off_ignores_headers(monkeypatch):
    # Even a well-formed header set is ignored when SSO trust mode is disabled.
    monkeypatch.setattr(w, "_TRUST_SSO", False)
    monkeypatch.setattr(w, "_SSO_SECRET", SECRET)
    r = _client().get("/api/auth/verify", headers=_gw_headers())
    assert r.status_code == 401


def test_empty_secret_fails_closed(monkeypatch):
    # Trust mode on but no configured secret → identity headers are NOT honored.
    monkeypatch.setattr(w, "_TRUST_SSO", True)
    monkeypatch.setattr(w, "_SSO_SECRET", "")
    h = {"X-Sysible-User": "alice", "X-Sysible-Role": "superuser", "X-Sysible-Auth": ""}
    r = _client().get("/api/auth/verify", headers=h)
    assert r.status_code == 401
