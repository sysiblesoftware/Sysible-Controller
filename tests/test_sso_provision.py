"""Backend bridge for SLOP single sign-on: POST /admin/sso-provision.

The web console (holding the root-only API key) calls this to turn a
gateway-asserted identity into a normal admin token: provision the account if
new, realign its role to what SLOP asserts, and mint a token. See
Sysible-Linux-Operations-Platform/docs/SSO.md.
"""
from conftest import key_headers
from backend import db


def test_provision_creates_account_and_mints_resolvable_token(controller):
    r = controller.post("/admin/sso-provision",
                        json={"username": "sso-alice", "role": "sysadmin"},
                        headers=key_headers())
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
                    headers=key_headers())
    assert db.get_administrator("sso-bob")["role"] == "auditor"
    # SLOP later promotes bob to superuser → the local account role follows.
    r = controller.post("/admin/sso-provision", json={"username": "sso-bob", "role": "superuser"},
                        headers=key_headers())
    assert r.status_code == 200
    assert db.get_administrator("sso-bob")["role"] == "superuser"


def test_provision_unknown_role_falls_closed_to_auditor(controller):
    r = controller.post("/admin/sso-provision", json={"username": "sso-carol", "role": "root"},
                        headers=key_headers())
    assert r.status_code == 200
    assert r.json()["role"] == "auditor"


def test_provision_requires_api_key(controller):
    r = controller.post("/admin/sso-provision", json={"username": "x", "role": "auditor"})
    assert r.status_code in (401, 403)


def test_provision_requires_username(controller):
    r = controller.post("/admin/sso-provision", json={"username": "  ", "role": "auditor"},
                        headers=key_headers())
    assert r.status_code == 400
