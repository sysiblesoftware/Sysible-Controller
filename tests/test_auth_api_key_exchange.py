"""POST /auth/api-key — exchange a Controller superuser's console credentials for
the backend API key, so a sibling tool (Sysible Linux Engineering Platform) can
connect without an operator copying /opt/sysible/api_key.txt by hand.

The endpoint is deliberately NOT behind require_api_key (needing the key to get
the key is the friction it removes), so it must be gated as hard as a login:
correct superuser creds → the key; wrong creds → 401; a non-superuser with the
right password → 403 (never the key); and per-username throttling on brute force.

Network gate (hardening against pulling the master key off the LAN-exposed :9000):
the request must originate from loopback OR carry the SLOP gateway shared secret.
The TestClient peer is not loopback, so these tests present the shared secret to
reach the credential logic — mirroring the co-located gateway/BFF.
"""
import os

import pytest

import backend.db as db
import backend.portal_auth as portal_auth
from conftest import key_headers, API_KEY  # noqa: F401

_SECRET = "api-key-gateway-secret-test"


@pytest.fixture(autouse=True)
def _sso_secret(monkeypatch):
    monkeypatch.setenv("SYSIBLE_SSO_SHARED_SECRET", _SECRET)


def _gw():
    """The gateway shared-secret header proving the request transited the gateway."""
    return {"X-Sysible-Auth": _SECRET}


def _make_admin_only(username, role, password):
    """Create an admin WITHOUT issuing a token (we're testing the password path)."""
    salt, pw_hash = portal_auth.hash_password(password)
    db.add_administrator(username, pw_hash, salt, must_change_password=0, role=role)


def test_superuser_creds_return_the_api_key(controller):
    _make_admin_only("su-key", "superuser", "Correct-Horse-9!")
    # No X-API-Key header — the whole point is obtaining it without already having it.
    r = controller.post("/auth/api-key", headers=_gw(),
                        json={"username": "su-key", "password": "Correct-Horse-9!"})
    assert r.status_code == 200, r.text
    assert r.json()["api_key"] == API_KEY


def test_wrong_password_401(controller):
    _make_admin_only("su-bad", "superuser", "Correct-Horse-9!")
    r = controller.post("/auth/api-key", headers=_gw(),
                        json={"username": "su-bad", "password": "nope"})
    assert r.status_code == 401


def test_unknown_user_401(controller):
    r = controller.post("/auth/api-key", headers=_gw(),
                        json={"username": "ghost-xyz", "password": "whatever"})
    assert r.status_code == 401


def test_non_superuser_is_denied_the_key(controller):
    """A sysadmin authenticates correctly but must NOT receive the master key."""
    _make_admin_only("sa-key", "sysadmin", "Correct-Horse-9!")
    r = controller.post("/auth/api-key", headers=_gw(),
                        json={"username": "sa-key", "password": "Correct-Horse-9!"})
    assert r.status_code == 403
    assert "api_key" not in r.json()


def test_brute_force_is_throttled_per_username(controller):
    _make_admin_only("su-throttle", "superuser", "Correct-Horse-9!")
    codes = [controller.post("/auth/api-key", headers=_gw(),
                             json={"username": "su-throttle", "password": "wrong"}).status_code
             for _ in range(12)]
    assert 429 in codes, "repeated failures should eventually lock the account"


# --- Regression guards for the network gate ------------------------------------

def test_off_gateway_request_is_refused(controller):
    """The pentest path: correct superuser creds but the request neither originates
    from loopback nor carries the gateway secret → 403, and NO key is returned, even
    on valid credentials. (The TestClient peer is non-loopback and no secret is sent.)"""
    _make_admin_only("su-lan", "superuser", "Correct-Horse-9!")
    r = controller.post("/auth/api-key",
                        json={"username": "su-lan", "password": "Correct-Horse-9!"})
    assert r.status_code == 403
    assert "api_key" not in r.json()


def test_wrong_gateway_secret_is_refused(controller):
    _make_admin_only("su-lan2", "superuser", "Correct-Horse-9!")
    r = controller.post("/auth/api-key", headers={"X-Sysible-Auth": "not-the-secret"},
                        json={"username": "su-lan2", "password": "Correct-Horse-9!"})
    assert r.status_code == 403
    assert "api_key" not in r.json()
