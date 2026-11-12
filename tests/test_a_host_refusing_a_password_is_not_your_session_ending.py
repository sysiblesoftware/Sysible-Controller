"""A managed host refusing an SSH password must not end the console session.

Reported, after a first fix that addressed a different cause:

    "When i try to enroll a host it's still logging out of controller"

The chain, which is three lines in three files:

  1. backend/remote_routes.py answered a paramiko AuthenticationException --
     the TARGET MACHINE refusing the credentials typed into the form --
     with `HTTPException(status_code=401)`.

  2. webgui/server.py's _superuser_request mirrors the upstream status
     verbatim: `raise HTTPException(status_code=r.status_code, ...)`.

  3. webgui/frontend/src/api.js: `if (res.status === 401) _fireUnauthorized()`.

To a browser, 401 means one thing: this session is over. So a mistyped SSH
password signed the operator out of the Controller. Three routes did it --
enrolling a host, opening a terminal, and browsing files -- so the same typo
would log you out from three different screens.

The earlier fix was for a real but DIFFERENT path (the gateway's HTML error
page), which is why it changed nothing here.

Two layers, because the shape outlives these three routes: the routes say 400
now, and the BFF no longer mirrors an upstream 401 without first checking the
token it sent was actually the thing rejected.

Run: pytest tests/
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ROUTES = (REPO / "backend" / "remote_routes.py").read_text(encoding="utf-8")
SERVER = (REPO / "webgui" / "server.py").read_text(encoding="utf-8")
APIJS = (REPO / "webgui" / "frontend" / "src" / "api.js").read_text(encoding="utf-8")


# ---- the source ----------------------------------------------------------
def test_no_route_answers_an_ssh_refusal_with_401():
    """401 is reserved for 'your credentials for THIS console are no good'."""
    bad = []
    for m in re.finditer(r"status_code=401, detail=\"([^\"]*)\"", ROUTES):
        if "admin token" not in m.group(1).lower():
            line = ROUTES[:m.start()].count("\n") + 1
            bad.append(f"line {line}: {m.group(1)}")
    assert not bad, (
        "these answer 401 for something that is not the caller's session, and "
        "the browser ends the session on any 401:\n  " + "\n  ".join(bad))


def test_every_ssh_auth_failure_is_a_400():
    """All three sites — enrolling, opening a terminal, browsing files."""
    sites = re.findall(r"status_code=(\d+), detail=\"SSH authentication failed\"", ROUTES)
    assert len(sites) == 3, (
        f"expected the three SSH-auth refusals; found {len(sites)}. If a route "
        f"was added or removed, check it does not answer 401.")
    assert set(sites) == {"400"}, f"an SSH refusal still answers {set(sites)}"


def test_the_admin_token_refusal_is_still_a_401():
    """The one case where 401 is right: the console's own token was rejected,
    and the session really is over."""
    assert re.search(r'status_code=401, detail="Invalid or expired admin token"', ROUTES), \
        "the admin-token refusal no longer reports a session problem"


# ---- the BFF safety net --------------------------------------------------
def test_the_bff_does_not_mirror_an_upstream_401_blindly():
    m = re.search(r"if not r\.ok:(.*?)return r\.json\(\)", SERVER, re.S)
    assert m, "_superuser_request's error path is no longer in the expected shape"
    body = m.group(1)
    assert "status == 401" in body, (
        "an upstream 401 is still passed straight to the browser, where it "
        "ends the session whatever it was about")
    assert "api.whoami" in body, \
        "nothing checks whether the token we sent was the thing rejected"
    assert "502" in body, "a 401 that was not about the session is not remapped"


def test_the_browser_still_ends_the_session_on_a_real_401():
    """The other half. A token that really was rejected must still sign out."""
    assert re.search(r'res\.status === 401 && path !== "/api/login"', APIJS), \
        "the console no longer reacts to a genuine 401"


# ---- and the whole chain, against the real BFF ---------------------------
@pytest.fixture()
def bff(monkeypatch):
    """webgui.server with the controller stubbed, so the three layers are the
    real ones and only the machine at the far end is a stand-in."""
    import sys
    sys.path.insert(0, str(REPO))
    import webgui.server as srv
    from fastapi.testclient import TestClient

    class _Resp:
        def __init__(self, status, payload, reason="Bad Request"):
            self.status_code, self._p, self.reason = status, payload, reason
            self.content = b"x"

        @property
        def ok(self):
            return self.status_code < 400

        def json(self):
            return self._p

    state = {"upstream": None, "token_valid": True, "whoami_calls": 0}

    def fake_request(method, url, **kw):
        return state["upstream"]

    def fake_whoami():
        state["whoami_calls"] += 1
        if state["token_valid"]:
            return {"username": "admin", "role": "superuser"}
        err = Exception("unauthorized")
        err.response = type("R", (), {"status_code": 401})()
        raise err

    monkeypatch.setattr(srv.api._SESSION, "request", fake_request)
    monkeypatch.setattr(srv.api, "whoami", fake_whoami)
    monkeypatch.setattr(srv, "_session_token", lambda request: "a-token")
    return srv, state, _Resp


def _call(srv, state):
    """Drive _superuser_request and report the status the BROWSER would see."""
    from fastapi import HTTPException

    class _Req:
        session = {"token": "a-token"}

    try:
        srv._superuser_request("POST", "/remote/enroll-ssh", _Req())
        return 200, None
    except HTTPException as e:
        return e.status_code, e.detail


def test_a_host_refusing_a_password_does_not_reach_the_browser_as_401(bff):
    """The bug, end to end. The upstream says 401 about the TARGET machine and
    the token we sent is still perfectly good."""
    srv, state, Resp = bff
    state["upstream"] = Resp(401, {"detail": "SSH authentication failed"})
    state["token_valid"] = True
    status, detail = _call(srv, state)
    assert status != 401, (
        "a managed host refusing a password still reaches the browser as 401, "
        "which signs the operator out")
    assert status == 502, status
    assert "SSH authentication failed" in detail, \
        f"the reason was lost on the way: {detail!r}"
    assert state["whoami_calls"] == 1, "the token was never actually checked"


def test_a_genuinely_rejected_token_still_ends_the_session(bff):
    """The other half, and the reason this checks rather than assumes: when the
    token IS the thing that was rejected, 401 is correct and must survive."""
    srv, state, Resp = bff
    state["upstream"] = Resp(401, {"detail": "Invalid or expired admin token"})
    state["token_valid"] = False
    status, detail = _call(srv, state)
    assert status == 401, (
        "a revoked token no longer ends the session, so a signed-out admin "
        "keeps the console open")


def test_a_400_is_passed_through_untouched(bff):
    """What the routes now send. No extra round trip, no remapping."""
    srv, state, Resp = bff
    state["upstream"] = Resp(400, {"detail": "SSH authentication failed"})
    status, detail = _call(srv, state)
    assert status == 400 and detail == "SSH authentication failed"
    assert state["whoami_calls"] == 0, \
        "the token was checked for a status that has nothing to do with it"


def test_an_unverifiable_401_keeps_its_status(bff):
    """If the check itself cannot be made we cannot tell the two apart, and
    ending the session is the safer of the two wrong answers."""
    srv, state, Resp = bff
    state["upstream"] = Resp(401, {"detail": "something ambiguous"})

    def boom():
        raise RuntimeError("controller blip")

    srv.api.whoami = boom
    status, _ = _call(srv, state)
    assert status == 401
