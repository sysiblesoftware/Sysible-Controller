"""The whole capture chain, from the operator's click to a version they can diff.

test_flashback_relay.py covers the middle hop (agent -> controller -> Flashback).
Nothing covered the chain an operator actually drives, and that is where it kept
breaking:

    Flashback console "Back up now"
      -> Flashback asks the CONTROLLER to park a capture request
      -> the agent's next config-backup poll is told a capture was asked for
      -> the agent reads the real files off disk and posts a snapshot
      -> the controller relays it to Flashback
      -> a version appears in the console

Every piece here is the real code: the real Flashback app on a real store, the
real controller app and its BFF, and the agent's own _d3_collect /
_d3_send_snapshot reading actual files from disk. Only the two HTTP hops are
bridged in-process, so the request and response SHAPES are pinned against the
real servers rather than against my belief about them.

"Flashback isn't backing things up" has been reported more than once, and every
time the fault was somewhere in this chain rather than in Flashback itself.
"""
import importlib
import os
import sys

import pytest

from tests.conftest import key_headers as _key
from tests.test_flashback_relay import TOKEN

FB_DIR = "/home/user/sysible-linux-operations-platform/flashback"
AGENT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "host_agent")


@pytest.fixture
def chain(tmp_path, monkeypatch, controller, agent):
    """Wire Flashback -> controller -> Flashback, and give the agent real files.

    Returns an object with the three ends plus the temp directory the agent is
    told to back up, so a test can change a file on disk and capture again.
    """
    if not os.path.isdir(FB_DIR):
        pytest.skip("the SLOP checkout is not next to this repo")

    fb_client, fb_store, fb_ctrl = _load_flashback(tmp_path)

    # --- controller -> Flashback (the relay the agent's snapshot rides)
    from backend import flashback as fbmod
    def relay(method, url, headers=None, timeout=None, **kw):
        return fb_client.request(method, url.replace("http://flashback.test", ""),
                                 headers=headers, **kw)
    monkeypatch.setattr(fbmod, "FLASHBACK_URL", "http://flashback.test")
    monkeypatch.setattr(fbmod, "AGENT_TOKEN", TOKEN)
    monkeypatch.setattr(fbmod.requests, "request", relay)

    # --- Flashback -> the CONTROLLER'S BFF (the "Back up now" request)
    #
    # Flashback talks to :8800, which is the web console's BFF — not the backend
    # API. That distinction matters: the route it calls
    # (/api/host/{id}/backup-now) exists only there, and the BFF is what turns it
    # into the backend's /agents/{id}/request-capture. Getting this wrong is
    # precisely how a "Back up now" click can 502 with everything else healthy.
    import webgui.server as srv
    from fastapi.testclient import TestClient as _TC
    srv.app.dependency_overrides[srv.require_login] = lambda: "chris"
    monkeypatch.setattr(srv, "_as_admin", lambda request, fn: fn())
    monkeypatch.setattr(srv.api, "request_config_capture",
                        lambda hid: controller.post(f"/agents/{hid}/request-capture",
                                                    headers=_key()).json())
    bff_client = _TC(srv.app)

    class _Resp:
        def __init__(self, r): self._r = r; self.status_code = r.status_code; self.text = r.text
        def json(self): return self._r.json()
    class _Client:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, headers=None, **kw):
            return _Resp(bff_client.get(_ctrl_path(url), headers=headers))
        def post(self, url, headers=None, **kw):
            return _Resp(bff_client.post(_ctrl_path(url), headers=headers))
    monkeypatch.setattr(fb_ctrl, "_CONTROLLER", "controller.test")
    monkeypatch.setattr(fb_ctrl, "_SSO_SECRET", "sso-secret")
    monkeypatch.setattr(fb_ctrl.httpx, "Client", _Client)

    # --- the agent, pointed at the controller, backing up a real directory
    agt = _agent_module()
    etc = tmp_path / "etc"; etc.mkdir()
    (etc / "hosts").write_text("127.0.0.1 localhost\n")
    (etc / "resolv.conf").write_text("nameserver 1.1.1.1\n")
    monkeypatch.setattr(agt, "D3_PATHS", str(etc))
    def agent_request(method, path, **kw):
        kw.pop("timeout", None)
        return controller.request(method, path, **kw)
    monkeypatch.setattr(agt, "_request", agent_request)

    host_id, secret = agent("host-1", "secret-1", "web1")

    class Chain:
        pass
    c = Chain()
    c.fb, c.fb_store, c.ctrl, c.agent_mod = fb_client, fb_store, controller, agt
    c.etc = etc
    c.state = {"host_id": host_id, "agent_secret": secret}
    c.host_id = host_id
    c.bff = bff_client
    yield c
    srv.app.dependency_overrides.pop(srv.require_login, None)


def _ctrl_path(url: str) -> str:
    for pre in ("https://controller.test", "http://controller.test"):
        if url.startswith(pre):
            return url[len(pre):]
    return url


def _load_flashback(tmp_path):
    """The real Flashback app, its store, AND the controller-client module the
    RUNNING APP holds.

    That last one is the whole reason this doesn't just reuse the relay test's
    helper. Flashback's package is also called `backend`, so loading it means
    displacing ours and putting them back afterwards — which leaves the app
    holding module objects that are no longer in sys.modules. Importing
    `backend.controller` a second time therefore yields a DIFFERENT object, and
    patching that one configures a module the app never calls: the app goes on
    reporting "no Controller is configured" while the test believes it is wired.
    Reach the app's own reference instead.
    """
    if not os.path.isdir(FB_DIR):
        pytest.skip("the SLOP checkout is not next to this repo")
    os.environ["SYSIBLE_FLASHBACK_DATA"] = str(tmp_path / "fbdata")
    os.environ["SYSIBLE_FLASHBACK_AGENT_TOKEN"] = TOKEN
    saved = {k: v for k, v in sys.modules.items() if k == "backend" or k.startswith("backend.")}
    for k in saved:
        del sys.modules[k]
    sys.path.insert(0, FB_DIR)
    try:
        ident = importlib.import_module("backend.identity")
        ident._AGENT_TOKEN = TOKEN
        store = importlib.import_module("backend.store")
        store.init_db()
        app_mod = importlib.import_module("backend.app")
        ctrl_mod = app_mod.controller          # the app's OWN reference
        from fastapi.testclient import TestClient
        client = TestClient(app_mod.app)
    finally:
        sys.path.remove(FB_DIR)
        for k in [k for k in list(sys.modules) if k == "backend" or k.startswith("backend.")]:
            del sys.modules[k]
        sys.modules.update(saved)
    return client, store, ctrl_mod


def _agent_module():
    sys.path.insert(0, AGENT_DIR)
    try:
        return importlib.import_module("agent")
    finally:
        sys.path.remove(AGENT_DIR)


GOOD_FB = {"X-Sysible-Auth": "sso-secret", "X-Sysible-User": "chris", "X-Sysible-Role": "superuser"}


# ---- the chain, end to end --------------------------------------------------
def test_back_up_now_in_the_console_ends_as_a_version_in_the_console(chain):
    """The whole point. Press the button, and after the host's next poll there is
    a stored version of a real file — with nothing else touched in between."""
    assert chain.fb_store.list_hosts() == [], "fixture: nothing captured yet"

    # 1. the operator presses "Back up now" in Flashback
    r = chain.fb.post(f"/api/hosts/{chain.host_id}/backup-now", headers=GOOD_FB)
    assert r.status_code == 200, r.text
    assert "next check-in" in r.json()["message"]

    # 2. the agent's next config-backup poll is told a capture was asked for
    agt = chain.agent_mod
    poll = chain.ctrl.get(f"/agents/{chain.host_id}/config-restores",
                          headers={"X-Agent-Secret": chain.state["agent_secret"]})
    assert poll.status_code == 200, poll.text
    assert poll.json()["capture_requested"] is True, "the request never reached the agent"

    # 3. the agent reads the real files and posts them (its own code path)
    agt._d3_send_snapshot(chain.state)

    # 4. the console now has this host, its files, and a version of each
    hosts = chain.fb_store.list_hosts()
    assert len(hosts) == 1 and hosts[0]["host_id"] == chain.host_id, hosts
    paths = {f["path"] for f in chain.fb_store.list_files(chain.host_id)}
    assert str(chain.etc / "hosts") in paths, paths
    assert str(chain.etc / "resolv.conf") in paths, paths


def test_the_request_is_handed_over_exactly_once(chain):
    """It is parked in memory and consumed on collection. A second poll must not
    make the host capture again — that is how an hourly agent turns one click
    into a capture every poll forever."""
    r = chain.fb.post(f"/api/hosts/{chain.host_id}/backup-now", headers=GOOD_FB)
    assert r.status_code == 200, r.text
    h = {"X-Agent-Secret": chain.state["agent_secret"]}
    first = chain.ctrl.get(f"/agents/{chain.host_id}/config-restores", headers=h).json()
    second = chain.ctrl.get(f"/agents/{chain.host_id}/config-restores", headers=h).json()
    assert first["capture_requested"] is True
    assert second["capture_requested"] is False


def test_a_changed_file_becomes_a_second_version_and_a_real_diff(chain):
    """The product is a TIME MACHINE — one stored copy is not the feature. Two
    captures around an edit must leave two versions and a diff that names the
    change."""
    agt = chain.agent_mod
    agt._d3_send_snapshot(chain.state)
    (chain.etc / "hosts").write_text("127.0.0.1 localhost\n10.0.0.5 db\n")
    agt._d3_send_snapshot(chain.state)

    path = str(chain.etc / "hosts")
    versions = chain.fb_store.list_versions(chain.host_id, path)
    assert len(versions) == 2, versions
    diff = chain.fb_store.diff_versions(chain.host_id, path,
                                        versions[1]["sha256"], versions[0]["sha256"])
    assert "10.0.0.5 db" in diff, diff


def test_an_unchanged_file_does_not_make_a_second_version(chain):
    """Capture is periodic. Without content addressing the history is 24
    identical entries a day and unreadable."""
    agt = chain.agent_mod
    agt._d3_send_snapshot(chain.state)
    agt._d3_send_snapshot(chain.state)
    path = str(chain.etc / "hosts")
    assert len(chain.fb_store.list_versions(chain.host_id, path)) == 1


def test_the_poll_itself_marks_the_agent_as_capture_capable(chain):
    """The signal the console uses to tell "this host will never capture until
    its agent is updated" from "its next check-in hasn't come round yet". An
    agent that reaches the poll at all is capable by definition."""
    before = chain.ctrl.get("/agents/config-poll-times",
                            headers={"X-API-Key": os.environ.get("SYSIBLE_API_KEY", "")})
    chain.ctrl.get(f"/agents/{chain.host_id}/config-restores",
                   headers={"X-Agent-Secret": chain.state["agent_secret"]})
    after = chain.ctrl.get("/agents/config-poll-times",
                           headers={"X-API-Key": os.environ.get("SYSIBLE_API_KEY", "")})
    if after.status_code != 200:
        pytest.skip("the poll-times endpoint needs the backend API key in this environment")
    hosts = after.json().get("hosts") or after.json()
    assert chain.host_id in str(hosts), (before.text, after.text)
