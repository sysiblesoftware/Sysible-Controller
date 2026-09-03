"""The terminal API Sysible Connect proxies through — and who its shells run as.

TWO regressions live here.

1. The API was deleted. "Remove the built-in in-browser terminal (Connect owns
   terminals now)" excised the Controller's own browser terminal AND the backend
   it shared with Connect: /remote/hosts/{name}/terminal/open,
   /remote/terminal/{id}/{read,write,resize,close}, and the agent PTY bridge
   /agents/{id}/pty/{sid}/{output,io}. Connect's entire fleet-terminal transport
   is those endpoints — it is how you get a shell on a NAT'd, agent-only host with
   no inbound SSH — so every terminal on a Controller-synced host answered

       POST /remote/hosts/web01/terminal/open -> 404 {"detail":"Not Found"}

   and Connect showed "Could not open a terminal on 'web01': Not Found". The
   Controller's own UI staying removed is correct; the API must stay.

2. There was no run-as under SSO. _resolve_admin_username read only
   X-Sysible-Admin-Token, which a SLOP operator never has — they have no local
   Controller password, because the SLOP sign-in IS their login. So the run-as
   identity was always None: every SSO terminal ran as the Controller's default
   account, unattributed, and Connect's console said "no run-as".

Both are pinned below, together with the auth floor (the shared secret is the
transport credential, and nothing weaker gets in) and the auditor gate on the
SSO path, which had no coverage at all because that path resolved no identity.
"""
import json
import os

import pytest

import backend.app as app_module
import backend.db as db
import backend.remote_routes as remote_routes
from tests.conftest import key_headers

SECRET = "gateway-shared-secret-for-tests"

# The wire contract between Connect and the Controller. Connect sets exactly these
# names in backend/controller.py::_request; the Controller reads exactly these in
# _resolve_admin_username / _reject_auditor / _gateway_secret_ok. A rename on one
# side alone silently drops the identity, which is failure mode 2 all over again.
H_AUTH, H_USER, H_ROLE = "X-Sysible-Auth", "X-Sysible-User", "X-Sysible-Role"


@pytest.fixture
def sso(monkeypatch):
    monkeypatch.setenv("SYSIBLE_SSO_SHARED_SECRET", SECRET)
    return SECRET


def sso_headers(user=None, role=None, secret=SECRET):
    h = {H_AUTH: secret}
    if user is not None:
        h[H_USER] = user
    if role is not None:
        h[H_ROLE] = role
    return h


def _paths():
    """Every routed path. The remote endpoints hang off an included APIRouter that
    the app wraps in a lazy `_IncludedRouter`, so app.routes alone does not list
    them — walk the router too, or this check passes while the routes are gone."""
    out = {getattr(r, "path", None) for r in app_module.app.routes}
    out |= {getattr(r, "path", None) for r in remote_routes.router.routes}
    return out


# ---- 1. the endpoints Connect drives must exist ---------------------------
@pytest.mark.parametrize("path", [
    "/remote/hosts/{name}/terminal/open",
    "/remote/terminal/{session_id}/read",
    "/remote/terminal/{session_id}/write",
    "/remote/terminal/{session_id}/resize",
    "/remote/terminal/{session_id}/close",
])
def test_the_controller_terminal_api_is_routed(path):
    assert path in _paths(), (
        f"{path} is not registered — Sysible Connect proxies its fleet terminals "
        "through this route, so removing it breaks every terminal on a "
        "Controller-synced host (NAT'd agent hosts have no other transport).")


@pytest.mark.parametrize("path", [
    "/agents/{host_id}/pty/{session_id}/output",
    "/agents/{host_id}/pty/{session_id}/io",
])
def test_the_agent_pty_bridge_is_routed(path):
    assert path in _paths(), (
        f"{path} is not registered — this is the agent's half of the terminal "
        "bridge: it POSTs shell output up and long-polls input down. Without it an "
        "agent-hosted shell can never stream, however the session opens.")


# ---- the auth floor on that API -------------------------------------------
def test_the_terminal_api_refuses_an_unauthenticated_caller(controller):
    r = controller.post("/remote/hosts/web01/terminal/open")
    assert r.status_code == 401, r.text


def test_the_terminal_api_refuses_a_wrong_shared_secret(controller, sso):
    r = controller.post("/remote/hosts/web01/terminal/open",
                        headers=sso_headers(secret="not-the-secret"))
    assert r.status_code == 401, r.text


def test_the_shared_secret_alone_is_accepted_as_connects_transport(controller, sso):
    """Connect attaches over SSO with no machine API key: the gateway secret IS its
    credential. 'host not found' proves it got past auth and into the handler."""
    r = controller.post("/remote/hosts/nosuchhost/terminal/open", headers=sso_headers())
    assert r.status_code == 404 and "host not found" in r.text, r.text


def test_the_terminal_api_is_closed_when_no_secret_is_configured(controller, monkeypatch):
    """Fail closed: with SSO unconfigured an empty secret must not match an empty
    header and let an anonymous caller in."""
    monkeypatch.setenv("SYSIBLE_SSO_SHARED_SECRET", "")
    r = controller.post("/remote/hosts/web01/terminal/open", headers={H_AUTH: ""})
    assert r.status_code == 401, r.text


# ---- 2. the run-as identity ------------------------------------------------
class _Req:
    """Minimal stand-in for a Starlette Request: only .headers is consulted."""
    def __init__(self, headers):
        from starlette.datastructures import Headers
        self.headers = Headers(headers)


def test_a_gateway_asserted_operator_becomes_the_run_as(sso):
    who = remote_routes._resolve_admin_username(_Req(sso_headers("alice", "operator")))
    assert who == "alice", (
        "the SLOP operator must be the run-as identity — otherwise every SSO "
        "terminal runs as the Controller's default account with no attribution")


def test_the_asserted_identity_is_ignored_without_the_shared_secret(sso):
    assert remote_routes._resolve_admin_username(
        _Req({H_USER: "alice", H_ROLE: "operator"})) is None, \
        "a caller that cannot present the secret must not be able to name the run-as"
    assert db.get_administrator("alice") is None, "and must not provision an account"


def test_the_asserted_identity_is_ignored_with_a_wrong_secret(sso):
    assert remote_routes._resolve_admin_username(
        _Req(sso_headers("alice", "operator", secret="wrong"))) is None
    assert db.get_administrator("alice") is None


def test_a_first_seen_operator_is_provisioned_and_becomes_the_run_as(sso):
    """The console's BFF provisions an SSO user on its first page load, but an
    operator who goes straight to Connect never loads that page. Without
    provisioning here, whether your shells run as you would depend on which app you
    happened to open first. SLOP's "operator" maps to the Controller's sysadmin."""
    who = remote_routes._resolve_admin_username(_Req(sso_headers("carol", "operator")))
    assert who == "carol"
    rec = db.get_administrator("carol")
    assert rec and rec["role"] == "sysadmin" and rec["created_by"] == "sso"


def test_an_unknown_asserted_role_is_provisioned_read_only(sso):
    """Fail closed: an unrecognised role must never become an operator, let alone a
    superuser, just because a header said something new."""
    remote_routes._resolve_admin_username(_Req(sso_headers("dave", "wizard")))
    assert db.get_administrator("dave")["role"] == "auditor"


def test_a_locally_managed_admin_of_the_same_name_is_never_regraded(sso, make_admin):
    """SSO owns only the accounts it created. A local admin must not be silently
    promoted (or demoted) because SLOP asserts a name that collides."""
    make_admin("localadmin", "auditor")          # created_by is not 'sso'
    assert remote_routes._resolve_admin_username(
        _Req(sso_headers("localadmin", "superuser"))) is None
    assert db.get_administrator("localadmin")["role"] == "auditor", "role was re-graded"


def test_a_header_injection_attempt_cannot_become_the_run_as(sso):
    """A malformed asserted name is refused outright — no run-as AND no account
    row, so it never reaches the host command or the audit trail."""
    for bad in ("root; rm -rf /", "root'\ndanger", "$(id)", "../../etc/passwd",
                "-oProxyCommand=x", "a" * 65, "", "  "):
        assert remote_routes._resolve_admin_username(
            _Req(sso_headers(bad, "superuser"))) is None, bad
        assert db.get_administrator(bad.strip()) is None, bad


def test_the_admin_token_still_wins_for_a_standalone_attach(sso, make_admin):
    tok = make_admin("bob", "superuser")
    who = remote_routes._resolve_admin_username(
        _Req({"X-Sysible-Admin-Token": tok, **sso_headers("alice", "operator")}))
    assert who == "bob", "an explicit admin token is the stronger claim; it must win"


def test_an_invalid_admin_token_does_not_fall_through_to_the_headers(sso):
    """A stale/forged token is a failed claim, not an invitation to try the next
    source — otherwise a revoked token silently becomes whoever the headers say."""
    assert remote_routes._resolve_admin_username(
        _Req({"X-Sysible-Admin-Token": "revoked-or-forged",
              **sso_headers("alice", "operator")})) is None


# ---- the auditor gate on the SSO path -------------------------------------
def test_an_sso_auditor_cannot_open_a_shell(sso, make_admin):
    from fastapi import HTTPException
    make_admin("reader", "auditor")
    with pytest.raises(HTTPException) as ei:
        remote_routes._reject_auditor(_Req(sso_headers("reader", "auditor")))
    assert ei.value.status_code == 403


def test_an_sso_auditor_is_caught_by_their_provisioned_role_too(sso, make_admin):
    """Belt and braces: even if the asserted role is inflated in transit, the
    administrators row still says auditor."""
    from fastapi import HTTPException
    make_admin("reader2", "auditor")
    with pytest.raises(HTTPException) as ei:
        remote_routes._reject_auditor(_Req(sso_headers("reader2", "operator")))
    assert ei.value.status_code == 403


def test_an_sso_operator_passes_the_auditor_gate(sso, make_admin):
    make_admin("opsy", "sysadmin")
    remote_routes._reject_auditor(_Req(sso_headers("opsy", "operator")))   # no raise


def test_an_unsigned_auditor_assertion_is_not_trusted_either_way(sso, make_admin):
    """No secret → no trusted identity → this gate has nothing to judge and defers
    to the route's own credential check (which already refused the request)."""
    make_admin("reader3", "auditor")
    remote_routes._reject_auditor(_Req({H_USER: "reader3", H_ROLE: "auditor"}))


# ---- end to end: the shell the agent is told to open --------------------
def test_an_agent_terminal_is_opened_as_the_sso_operator(controller, sso, agent):
    """The whole point, checked on the wire: open a terminal the way Connect does
    and read back the pty_open task the agent will execute. Its "user" is who the
    shell runs as on the host."""
    host_id, _ = agent(host_id="h-term", hostname="web01", ip="10.0.0.21")

    r = controller.post("/remote/hosts/web01/terminal/open",
                        headers=sso_headers("alice", "operator"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["via"] == "agent" and body["opened"] is True and body["session_id"]

    tasks = db.fetch_pending_tasks(host_id)
    pty = [t for t in tasks if t.get("kind") == "pty_open"]
    assert pty, f"no pty_open task was queued for the agent: {tasks}"
    payload = json.loads(pty[0]["command"])
    assert payload["session_id"] == body["session_id"]
    assert payload["user"] == "alice", (
        f'the shell would run as {payload["user"]!r}, not the signed-in operator — '
        "that is the missing run-as")


def test_an_agent_terminal_without_a_trusted_identity_has_no_run_as(controller, sso, agent):
    """An api-key-only attach (no identity) still works — it just runs as the
    Controller's default account. Empty, not a guess."""
    host_id, _ = agent(host_id="h-term2", hostname="web02", ip="10.0.0.22")
    r = controller.post("/remote/hosts/web02/terminal/open", headers=key_headers())
    assert r.status_code == 200, r.text
    payload = json.loads([t for t in db.fetch_pending_tasks(host_id)
                          if t.get("kind") == "pty_open"][0]["command"])
    assert payload["user"] == ""


def test_an_offline_agent_fails_fast_instead_of_hanging(controller, sso):
    """A queued pty_open an offline agent never collects would show 'Connected.'
    and a dead cursor for minutes. 503 with a reason instead."""
    db.create_or_update_agent("h-old", "web03", "linux", "6.1", "online",
                              0, "s", "10.0.0.23")
    r = controller.post("/remote/hosts/web03/terminal/open", headers=sso_headers())
    assert r.status_code == 503 and "isn't checking in" in r.text, r.text


def test_an_sso_owned_role_is_realigned_when_slop_changes_it(sso):
    """SLOP is the identity authority. If an operator is demoted there, the next
    request must not still run with the role the Controller cached."""
    remote_routes._resolve_admin_username(_Req(sso_headers("erin", "superuser")))
    assert db.get_administrator("erin")["role"] == "superuser"
    remote_routes._resolve_admin_username(_Req(sso_headers("erin", "auditor")))
    assert db.get_administrator("erin")["role"] == "auditor"


# ---- the full bridge, both halves, one round trip -------------------------
def test_a_shell_streams_end_to_end_through_the_bridge(controller, sso, agent):
    """Every hop Connect and the agent actually use, in order, in one test:

        Connect  -> POST /remote/hosts/web01/terminal/open      (SSO secret)
        agent    -> GET  /agents/h/pty/<sid>/io                 (picks up input)
        agent    -> POST /agents/h/pty/<sid>/output             (shell output up)
        Connect  -> GET  /remote/terminal/<sid>/read            (output down)
        Connect  -> POST /remote/terminal/<sid>/write           (keystrokes up)
        agent    -> GET  /agents/h/pty/<sid>/io                 (keystrokes down)
        Connect  -> POST /remote/terminal/<sid>/close

    Route-existence checks alone would not have caught a half-restored bridge:
    the browser half and the agent half were removed by the same commit, and one
    without the other is a session that opens and then never draws.
    """
    host_id, secret = agent(host_id="h-e2e", hostname="web01", ip="10.0.0.31")

    r = controller.post("/remote/hosts/web01/terminal/open",
                        headers=sso_headers("alice", "operator"))
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]

    # The agent collects the pty_open task, then streams a prompt up.
    tasks = db.fetch_pending_tasks(host_id)
    assert any(t.get("kind") == "pty_open" for t in tasks), tasks
    up = controller.post(f"/agents/{host_id}/pty/{sid}/output",
                         json={"agent_secret": secret, "data": "alice@web01:~$ ",
                               "ended": False})
    assert up.status_code == 200, up.text

    # Connect reads it back off the Controller.
    down = controller.get(f"/remote/terminal/{sid}/read", headers=sso_headers())
    assert down.status_code == 200, down.text
    assert down.json()["data"] == "alice@web01:~$ "
    assert down.json()["closed"] is False

    # Keystrokes travel the other way: Connect writes, the agent picks them up.
    w = controller.post(f"/remote/terminal/{sid}/write", headers=sso_headers(),
                        json={"data": "id\n"})
    assert w.status_code == 200, w.text
    io = controller.get(f"/agents/{host_id}/pty/{sid}/io",
                        headers={"X-Agent-Secret": secret})
    assert io.status_code == 200, io.text
    # The agent's wire format for an input message (host_agent/agent.py: t == "i").
    assert {"t": "i", "d": "id\n"} in io.json()["msgs"], io.json()

    # And the close propagates to the agent's side of the bridge.
    c = controller.post(f"/remote/terminal/{sid}/close", headers=sso_headers())
    assert c.status_code == 200, c.text
    io2 = controller.get(f"/agents/{host_id}/pty/{sid}/io",
                         headers={"X-Agent-Secret": secret})
    assert io2.json()["closed"] is True


def test_the_agent_half_of_the_bridge_needs_the_agent_secret(controller, sso, agent):
    host_id, secret = agent(host_id="h-e2e2", hostname="web02", ip="10.0.0.32")
    sid = controller.post("/remote/hosts/web02/terminal/open",
                          headers=sso_headers()).json()["session_id"]
    bad = controller.post(f"/agents/{host_id}/pty/{sid}/output",
                          json={"agent_secret": "wrong", "data": "x", "ended": False})
    assert bad.status_code in (401, 403), bad.text
    assert controller.get(f"/agents/{host_id}/pty/{sid}/io",
                          headers={"X-Agent-Secret": "wrong"}).status_code in (401, 403)
