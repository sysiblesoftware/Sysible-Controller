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


# ---------------------------------------------------------------------------
# EVERY gate, not just require_login
# ---------------------------------------------------------------------------
# The gap the tests above missed: _require_gateway_when_sso says in its own
# docstring that it is "called by EVERY auth gate", and it was called by two of
# four. require_operator and require_superuser_session called only
# _ensure_sso_session — which mints the session from gateway headers but does
# not REQUIRE them — so the replay closed above for /api/me stayed wide open on
# every write and superuser route: fleet dispatch (root), schedules, enrolment,
# the portal, TLS install. Those are the routes the replay was worth doing.
#
# So the check below is written against the gate functions themselves, found by
# walking the route table, rather than against a hand-picked route. A new gate,
# or a new route on an existing one, is covered the day it is added.
from starlette.requests import Request as _StarletteRequest  # noqa: E402

_GATEWAY_SESSION = {
    "_sso": True, "user": "alice", "role": "superuser",
    "sudo_connect": False, "must_change_password": False,
}


def _request(session=None, headers=None):
    """A Starlette Request with a session, as the gates see it."""
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return _StarletteRequest({"type": "http", "method": "GET", "path": "/api/x",
                              "headers": hdrs, "session": dict(session or {})})


def _route_gates():
    """Every `require_*` dependency reachable from the BFF's route table."""
    from fastapi.routing import APIRoute

    found, seen = {}, set()

    def walk(dep):
        if id(dep) in seen:
            return
        seen.add(id(dep))
        call = getattr(dep, "call", None)
        name = getattr(call, "__name__", "")
        if name.startswith("require_") and getattr(call, "__module__", "") == srv.__name__:
            found[name] = call
        for sub in getattr(dep, "dependencies", ()) or ():
            walk(sub)

    for route in srv.app.routes:
        if isinstance(route, APIRoute):
            walk(route.dependant)
    return found


@pytest.fixture
def live_session(monkeypatch):
    """A session cookie exactly as a gateway visit mints one: SSO-flagged, with a
    controller token that revalidates clean."""
    monkeypatch.setattr(srv.api, "whoami", lambda: {"role": "superuser"})
    sess = dict(_GATEWAY_SESSION)
    sess["token_enc"] = srv._encrypt_token("tok")
    return sess


def test_the_route_table_has_the_gates_we_think_it_has():
    # Guards the walker itself: if it silently finds nothing, every assertion
    # below passes for the wrong reason.
    gates = _route_gates()
    assert set(gates) >= {"require_login", "require_login_changed",
                          "require_operator", "require_superuser_session"}, gates


def test_no_gate_accepts_a_replayed_cookie_without_the_gateway(sso_on, live_session):
    from fastapi import HTTPException
    for name, gate in sorted(_route_gates().items()):
        req = _request(live_session)          # the cookie, no gateway headers
        with pytest.raises(HTTPException) as ei:
            gate(req)
        assert ei.value.status_code == 401, f"{name} answered {ei.value.status_code}"
        assert "Sysible Linux Operations Platform" in str(ei.value.detail), name
        assert req.session == {}, f"{name} left the stale session in place"


def test_every_gate_still_passes_a_real_gateway_request(sso_on, live_session):
    for name, gate in sorted(_route_gates().items()):
        assert gate(_request(live_session, _hdrs(user="alice", role="superuser"))) == "alice", name


def test_standalone_gates_do_not_require_a_gateway(monkeypatch, live_session):
    # No SLOP: the console's own cookie is the only identity there is.
    monkeypatch.setattr(srv, "_TRUST_SSO", False)
    monkeypatch.setattr(srv, "_SSO_SECRET", "")
    for name, gate in sorted(_route_gates().items()):
        assert gate(_request(live_session)) == "alice", name


@pytest.fixture
def tls_bff():
    """A client on https://, so the Secure session cookie is actually REPLAYED.

    The plain-http `bff` fixture never sends it back (https_only is on by
    default), which is why a route-level replay test over http passes whether
    the gate is there or not — the 401 it sees is "no cookie at all".
    """
    from starlette.testclient import TestClient
    return TestClient(srv.app, base_url="https://testserver")


def _assert_cookie_replays(client):
    jar = client.cookies
    assert "sysible_web" in jar, "expected the BFF to mint a session cookie"


def test_a_write_route_refuses_the_replayed_cookie(tls_bff, sso_on, monkeypatch):
    """The same thing end to end, through a route that actually dispatches."""
    monkeypatch.setattr(srv.api, "sso_provision",
                        lambda user, role: {"role": "sysadmin", "token": "tok",
                                            "sudo_connect": False})
    monkeypatch.setattr(srv.api, "whoami", lambda: {"role": "sysadmin"})
    assert tls_bff.get("/api/schedules", headers=_hdrs()).status_code == 200
    _assert_cookie_replays(tls_bff)

    r = tls_bff.get("/api/schedules")        # straight at :8800 with that cookie
    assert r.status_code == 401, r.text
    assert "Sysible Linux Operations Platform" in r.json()["detail"]


def test_a_superuser_route_refuses_the_replayed_cookie(tls_bff, sso_on, monkeypatch):
    monkeypatch.setattr(srv.api, "sso_provision",
                        lambda user, role: {"role": "superuser", "token": "tok",
                                            "sudo_connect": False})
    monkeypatch.setattr(srv.api, "whoami", lambda: {"role": "superuser"})
    monkeypatch.setattr(srv.api, "get_portal_status", lambda: {"running": False})
    assert tls_bff.get("/api/portal/status",
                       headers=_hdrs(role="superuser")).status_code == 200
    _assert_cookie_replays(tls_bff)

    r = tls_bff.get("/api/portal/status")
    assert r.status_code == 401, r.text
    assert "Sysible Linux Operations Platform" in r.json()["detail"]


def test_the_replay_is_only_blocked_because_of_the_gate(tls_bff, monkeypatch):
    """Control: with SLOP out of the picture the very same replay WORKS.

    Without this, the two tests above would also pass if the cookie were simply
    never sent back — which is exactly how the original /api/me test came to
    pass with the gap still open.
    """
    monkeypatch.setattr(srv, "_TRUST_SSO", False)
    monkeypatch.setattr(srv, "_SSO_SECRET", "")
    monkeypatch.setattr(srv.api, "admin_setup", lambda u, p: {"role": "superuser", "token": "tok"})
    monkeypatch.setattr(srv.api, "whoami", lambda: {"role": "superuser"})
    monkeypatch.setattr(srv.api, "get_portal_status", lambda: {"running": False})
    assert tls_bff.post("/api/admin/setup",
                        json={"username": "admin", "password": "Hunter2!!"}).status_code == 200
    _assert_cookie_replays(tls_bff)
    assert tls_bff.get("/api/portal/status").status_code == 200


def test_first_run_setup_is_refused_when_slop_owns_identity(bff, sso_on):
    """/api/login is closed under SSO; /api/admin/setup was not.

    Nothing in SLOP creates the controller's local admin row until someone
    visits through the gateway, so on a fresh platform install
    admin_setup_required() is true and this route would mint a superuser — with
    a password, a session, and no presence in SLOP Administration — to whoever
    reached :8800 first.
    """
    r = bff.post("/api/admin/setup", json={"username": "backdoor", "password": "Hunter2!!"})
    assert r.status_code == 403, r.text
    assert "Sysible Linux Operations Platform" in r.json()["detail"]


def test_the_spa_is_not_offered_a_setup_form_under_sso(bff, sso_on, monkeypatch):
    # The console asks this before rendering anything. Answering "yes" under SSO
    # puts a create-administrator wizard in front of an anonymous visitor.
    monkeypatch.setattr(srv.api, "admin_setup_required", lambda: True)
    assert bff.get("/api/admin/setup-required").json() == {"setup_required": False}


def test_standalone_first_run_setup_still_works(bff, monkeypatch):
    monkeypatch.setattr(srv, "_TRUST_SSO", False)
    monkeypatch.setattr(srv, "_SSO_SECRET", "")
    monkeypatch.setattr(srv.api, "admin_setup_required", lambda: True)
    assert bff.get("/api/admin/setup-required").json() == {"setup_required": True}
    monkeypatch.setattr(srv.api, "admin_setup",
                        lambda u, p: {"role": "superuser", "token": "tok"})
    r = bff.post("/api/admin/setup", json={"username": "admin", "password": "Hunter2!!"})
    assert r.status_code == 200, r.text
