"""Under SLOP SSO, the gateway is the ONLY way into the web console.

The console's BFF also listens on its own published port (:8800), so "signed in
via SLOP" is not the same as "only reachable via SLOP". Two doors were open:

  * /api/login still accepted a controller-local credential — an account SLOP
    Administration does not manage, and one that a SLOP sign-out cannot end;
  * _ensure_sso_session mints a signed session cookie from the gateway headers,
    and require_login accepted that cookie ON ITS OWN. A browser that had used
    the console through SLOP therefore kept a working cookie it could replay
    straight at :8800 — after signing out of SLOP, and for the cookie's full
    12-hour life.

Both are closed when SYSIBLE_WEBGUI_TRUST_SSO=1 with a shared secret set:
identity must be asserted by the gateway on the request being served. Standalone
deployments (no SSO configured) keep their own login and are untouched.
"""
import pytest

from webgui import server as srv


@pytest.fixture
def sso_on(monkeypatch):
    monkeypatch.setattr(srv, "_TRUST_SSO", True)
    monkeypatch.setattr(srv, "_SSO_SECRET", "shared-secret-for-tests")


def _hdrs(user="alice", role="operator", secret="shared-secret-for-tests"):
    return {"X-Sysible-Auth": secret, "X-Sysible-User": user, "X-Sysible-Role": role}


def test_sso_only_is_off_until_both_halves_are_configured(monkeypatch):
    monkeypatch.setattr(srv, "_TRUST_SSO", False)
    monkeypatch.setattr(srv, "_SSO_SECRET", "")
    assert srv.sso_only() is False
    # Trust on but no secret: we cannot prove a request came through the gateway,
    # so the console must NOT switch into gateway-only mode and lock itself out.
    monkeypatch.setattr(srv, "_TRUST_SSO", True)
    assert srv.sso_only() is False
    monkeypatch.setattr(srv, "_SSO_SECRET", "s")
    assert srv.sso_only() is True


def test_local_login_is_refused_when_slop_owns_identity(bff, sso_on):
    r = bff.post("/api/login", json={"username": "admin", "password": "whatever"})
    assert r.status_code == 403
    assert "Sysible Linux Operations Platform" in r.json()["detail"]


def test_a_session_minted_through_the_gateway_does_not_work_without_it(bff, sso_on,
                                                                       monkeypatch):
    """The replay that outlived sign-out: use the console through the gateway,
    keep the cookie, then hit the console's own port with it."""
    monkeypatch.setattr(srv.api, "sso_provision",
                        lambda user, role: {"role": role, "token": "tok", "sudo_connect": False})
    # Through the gateway: authenticated, and a session cookie is set.
    r = bff.get("/api/me", headers=_hdrs())
    assert r.status_code == 200, r.text
    assert bff.cookies, "expected the BFF to mint a session cookie"

    # Same client, same cookie, no gateway headers — i.e. straight at :8800.
    r = bff.get("/api/me")
    assert r.status_code == 401
    assert "Sysible Linux Operations Platform" in r.json()["detail"]


def test_the_stale_cookie_is_cleared_not_just_refused(bff, sso_on, monkeypatch):
    monkeypatch.setattr(srv.api, "sso_provision",
                        lambda user, role: {"role": role, "token": "tok", "sudo_connect": False})
    assert bff.get("/api/me", headers=_hdrs()).status_code == 200
    bff.get("/api/me")                      # refused, and clears the session
    # Even re-presented, it stays refused — there is nothing left in it to replay.
    assert bff.get("/api/me").status_code == 401


def test_a_forged_secret_is_not_an_identity(bff, sso_on):
    r = bff.get("/api/me", headers=_hdrs(secret="wrong"))
    assert r.status_code == 401


def test_the_gateway_path_still_works(bff, sso_on, monkeypatch):
    # The point is to close the second door, not the first.
    monkeypatch.setattr(srv.api, "sso_provision",
                        lambda user, role: {"role": role, "token": "tok", "sudo_connect": False})
    assert bff.get("/api/me", headers=_hdrs()).status_code == 200


def test_standalone_console_keeps_its_own_login(bff, monkeypatch):
    monkeypatch.setattr(srv, "_TRUST_SSO", False)
    monkeypatch.setattr(srv, "_SSO_SECRET", "")
    # Not 403 — the local login is still the way in when there is no SLOP.
    r = bff.post("/api/login", json={"username": "nope", "password": "nope"})
    assert r.status_code != 403
