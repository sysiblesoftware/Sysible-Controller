"""Backend bridge for SLOP single sign-on: POST /admin/sso-provision.

The web console (holding the root-only API key) calls this to turn a
gateway-asserted identity into a normal admin token: provision the account if
new, realign its role to what SLOP asserts, and mint a token. See
Sysible-Linux-Operations-Platform/docs/SSO.md.

Trust boundary: the API key ALONE is not enough — the request must also prove it
transited the SLOP gateway by carrying the shared secret (X-Sysible-Auth), and the
bridge only manages accounts it created (created_by='sso'), so a bare-key caller
on the LAN can't mint or promote a superuser through this path.
"""
import os

import pytest

from conftest import key_headers, API_KEY  # noqa: F401
from backend import db

_SECRET = "sso-shared-secret-test"


@pytest.fixture(autouse=True)
def _sso_secret(monkeypatch):
    monkeypatch.setenv("SYSIBLE_SSO_SHARED_SECRET", _SECRET)


def _gw_headers():
    """API key + the gateway shared secret (proof the request came via the gateway)."""
    return key_headers({"X-Sysible-Auth": _SECRET})


def test_provision_creates_account_and_mints_resolvable_token(controller):
    r = controller.post("/admin/sso-provision",
                        json={"username": "sso-alice", "role": "sysadmin"},
                        headers=_gw_headers())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["username"] == "sso-alice"
    assert body["role"] == "sysadmin"
    assert body["token"]
    # The account now exists with the asserted role...
    acct = db.get_administrator("sso-alice")
    assert acct is not None and acct["role"] == "sysadmin"
    # ...and the minted token resolves to that identity (survives the account
    # cross-check inside resolve_admin_token).
    ident = db.resolve_admin_token(body["token"])
    assert ident == {"username": "sso-alice", "role": "sysadmin"}


def test_provision_realigns_role_when_slop_changes_it(controller):
    controller.post("/admin/sso-provision", json={"username": "sso-bob", "role": "auditor"},
                    headers=_gw_headers())
    assert db.get_administrator("sso-bob")["role"] == "auditor"
    # SLOP later promotes bob to superuser → the local (SSO-owned) account role follows.
    r = controller.post("/admin/sso-provision", json={"username": "sso-bob", "role": "superuser"},
                        headers=_gw_headers())
    assert r.status_code == 200
    assert db.get_administrator("sso-bob")["role"] == "superuser"


def test_provision_unknown_role_falls_closed_to_auditor(controller):
    r = controller.post("/admin/sso-provision", json={"username": "sso-carol", "role": "root"},
                        headers=_gw_headers())
    assert r.status_code == 200
    assert r.json()["role"] == "auditor"


def test_provision_requires_api_key(controller):
    r = controller.post("/admin/sso-provision", json={"username": "x", "role": "auditor"})
    assert r.status_code in (401, 403)


def test_provision_requires_username(controller):
    r = controller.post("/admin/sso-provision", json={"username": "  ", "role": "auditor"},
                        headers=_gw_headers())
    assert r.status_code == 400


# --- Regression guards for the hardened trust boundary --------------------------

def test_bare_api_key_without_gateway_secret_is_refused(controller):
    """The exact bare-API-key path the pentest abused to mint a superuser: correct
    API key, NO gateway shared secret → 403, and no account is created."""
    r = controller.post("/admin/sso-provision",
                        json={"username": "attacker-svc", "role": "superuser"},
                        headers=key_headers())
    assert r.status_code == 403
    assert db.get_administrator("attacker-svc") is None


def test_wrong_gateway_secret_is_refused(controller):
    r = controller.post("/admin/sso-provision",
                        json={"username": "attacker-svc2", "role": "superuser"},
                        headers=key_headers({"X-Sysible-Auth": "not-the-secret"}))
    assert r.status_code == 403
    assert db.get_administrator("attacker-svc2") is None


def test_secret_unset_fails_closed(controller, monkeypatch):
    """SSO off / secret unset on the backend → refuse even with a header present."""
    monkeypatch.delenv("SYSIBLE_SSO_SHARED_SECRET", raising=False)
    r = controller.post("/admin/sso-provision",
                        json={"username": "attacker-svc3", "role": "superuser"},
                        headers=key_headers({"X-Sysible-Auth": _SECRET}))
    assert r.status_code == 403


def test_refuses_to_regrade_a_locally_managed_admin(controller, make_admin):
    """A locally-managed admin (created_by != 'sso') must never be re-graded via the
    SSO bridge — closes the 'promote an existing low-priv admin to superuser' path."""
    # make_admin creates a normal (created_by=NULL) sysadmin account.
    make_admin("local-ops", "sysadmin")
    r = controller.post("/admin/sso-provision",
                        json={"username": "local-ops", "role": "superuser"},
                        headers=_gw_headers())
    assert r.status_code == 409
    assert db.get_administrator("local-ops")["role"] == "sysadmin"
