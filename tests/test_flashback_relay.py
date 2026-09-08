"""Config backup (Sysible Flashback): the agent -> controller -> Flashback path.

Flashback shipped with a complete server and console and NO producer: nothing
anywhere ever POSTed a snapshot, so every install sat on "No host has reported a
config backup yet". These tests cover the relay that fills that gap, and in
particular the reason it is a relay at all.

THE TRUST PROPERTY. Flashback's agent API is guarded by ONE bearer token and its
endpoints take host_id from the caller. Handing that token to every managed host
would let any single compromised host read every other host's stored config and
queue a restore that overwrites a file on it. So hosts talk to the CONTROLLER,
which authenticates them and stamps the host_id itself. test_a_host_cannot_
snapshot_as_another_host is the whole point of the design.

Most tests here run against a REAL Flashback app in-process (not a mock), so the
request shapes are pinned against the actual server rather than my belief about
it — the same reason the relay client was written from its routes.
"""
import base64
import importlib
import os
import sys

import pytest

FB_DIR = "/home/user/sysible-linux-operations-platform/flashback"
TOKEN = "test-flashback-agent-token"


def _real_flashback(tmp_path):
    """The actual Flashback app, on its own store, with an agent token set.

    NAME COLLISION, handled carefully: Flashback's package is ALSO called
    `backend`, exactly like this controller's. Importing it means displacing
    `backend.*` in sys.modules, and the first cut of this helper simply deleted
    those entries afterwards — so a later `from backend import db` in a test
    re-imported a FRESH controller backend.db pointing at a different database
    than the running app, and an assertion about the activity feed read an empty
    log while the app had written to the real one. The originals are saved and
    put back instead, so the controller's modules are exactly as they were.
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
        from fastapi.testclient import TestClient
        client = TestClient(app_mod.app)
    finally:
        sys.path.remove(FB_DIR)
        for k in [k for k in list(sys.modules) if k == "backend" or k.startswith("backend.")]:
            del sys.modules[k]
        sys.modules.update(saved)          # the controller's own backend, intact
    return client, store


@pytest.fixture
def flashback(tmp_path, monkeypatch):
    """Point the controller's relay client at a real in-process Flashback."""
    fb_client, store = _real_flashback(tmp_path)
    from backend import flashback as fbmod

    def fake_request(method, url, headers=None, timeout=None, **kw):
        assert (headers or {}).get("Authorization") == f"Bearer {TOKEN}", "token not forwarded"
        path = url.replace("http://flashback.test", "")
        return fb_client.request(method, path, headers=headers, **kw)

    monkeypatch.setattr(fbmod, "FLASHBACK_URL", "http://flashback.test")
    monkeypatch.setattr(fbmod, "AGENT_TOKEN", TOKEN)
    monkeypatch.setattr(fbmod.requests, "request", fake_request)
    return store


def hdr(secret):
    return {"X-Agent-Secret": secret}


def files(*pairs):
    return [{"path": p, "content_b64": base64.b64encode(c.encode()).decode()} for p, c in pairs]


# ---- the trust property ----------------------------------------------------
def test_a_host_cannot_snapshot_as_another_host(controller, agent, flashback):
    """The reason hosts never hold Flashback's token. host_id comes from the URL
    the controller just authenticated, so a body field naming another host is
    ignored rather than obeyed."""
    h1, s1 = agent("host-1", "secret-1", "web1")
    agent("host-2", "secret-2", "web2")
    r = controller.post(f"/agents/{h1}/config-snapshot", headers=hdr(s1),
                        json={"host_id": "host-2", "files": files(("/etc/hosts", "a"))})
    assert r.status_code == 200, r.text
    hosts = {h["host_id"] for h in flashback.list_hosts()}
    assert hosts == {"host-1"}, "a host wrote into another host's history"


def test_a_wrong_agent_secret_is_refused(controller, agent, flashback):
    h1, _ = agent("host-1", "secret-1", "web1")
    r = controller.post(f"/agents/{h1}/config-snapshot", headers=hdr("wrong"),
                        json={"files": files(("/etc/hosts", "a"))})
    assert r.status_code == 401
    assert flashback.list_hosts() == []


def test_an_unknown_host_is_refused(controller, flashback):
    r = controller.post("/agents/nope/config-snapshot", headers=hdr("x"),
                        json={"files": files(("/etc/hosts", "a"))})
    assert r.status_code == 404


# ---- capture ---------------------------------------------------------------
def test_a_snapshot_reaches_flashback_and_shows_up_as_a_host(controller, agent, flashback):
    """The end the operator sees: after this, the console is no longer empty."""
    h1, s1 = agent("host-1", "secret-1", "web1")
    r = controller.post(f"/agents/{h1}/config-snapshot", headers=hdr(s1),
                        json={"files": files(("/etc/hosts", "127.0.0.1 x"),
                                             ("/etc/hostname", "web1"))})
    assert r.status_code == 200, r.text
    assert r.json()["changed"] == 2
    hosts = flashback.list_hosts()
    assert len(hosts) == 1 and hosts[0]["host_id"] == "host-1"
    assert hosts[0]["files"] == 2
    # The console labels the host by its hostname, not its opaque id.
    assert hosts[0]["label"] == "web1"


def test_unchanged_files_do_not_pile_up_versions(controller, agent, flashback):
    """Capture runs hourly; without content-addressing that is 24 identical
    versions a day and the real history is unreadable."""
    h1, s1 = agent("host-1", "secret-1", "web1")
    body = {"files": files(("/etc/hosts", "same"))}
    assert controller.post(f"/agents/{h1}/config-snapshot", headers=hdr(s1), json=body).json()["changed"] == 1
    assert controller.post(f"/agents/{h1}/config-snapshot", headers=hdr(s1), json=body).json()["changed"] == 0
    body2 = {"files": files(("/etc/hosts", "different"))}
    assert controller.post(f"/agents/{h1}/config-snapshot", headers=hdr(s1), json=body2).json()["changed"] == 1
    assert len(flashback.list_versions("host-1", "/etc/hosts")) == 2


def test_the_relay_cap_sits_below_the_global_request_ceiling(controller):
    """Ordering, not just size. The controller's body-limit middleware rejects an
    over-large body BEFORE any route sees it, with a flat "Request body too
    large." When the two caps were equal that generic message always won, and an
    operator whose /etc had outgrown the cap got no hint which knob to turn."""
    from backend.app import _max_request_bytes
    from backend import flashback as fbmod
    assert fbmod.MAX_SNAPSHOT_BYTES < _max_request_bytes()


def test_an_oversized_snapshot_is_refused_with_a_usable_message(controller, agent, flashback):
    from backend import flashback as fbmod
    h1, s1 = agent("host-1", "secret-1", "web1")
    # Over the relay cap but under the global ceiling, so the useful one answers.
    payload = "x" * (fbmod.MAX_SNAPSHOT_BYTES + 1000)
    r = controller.post(f"/agents/{h1}/config-snapshot", headers=hdr(s1),
                        json={"files": [{"path": "/etc/big", "content_b64": payload}]})
    assert r.status_code == 413
    assert "SYSIBLE_D3LOREAN_PATHS" in r.json()["detail"]     # says what to do


# ---- restore ---------------------------------------------------------------
def test_a_queued_restore_is_offered_to_that_host_only(controller, agent, flashback):
    h1, s1 = agent("host-1", "secret-1", "web1")
    h2, s2 = agent("host-2", "secret-2", "web2")
    controller.post(f"/agents/{h1}/config-snapshot", headers=hdr(s1),
                    json={"files": files(("/etc/hosts", "v1"))})
    sha = flashback.list_versions("host-1", "/etc/hosts")[0]["sha256"]
    flashback.queue_restore("host-1", "/etc/hosts", sha, "alice")

    mine = controller.get(f"/agents/{h1}/config-restores", headers=hdr(s1)).json()["restores"]
    theirs = controller.get(f"/agents/{h2}/config-restores", headers=hdr(s2)).json()["restores"]
    assert len(mine) == 1 and mine[0]["path"] == "/etc/hosts"
    assert theirs == [], "a restore leaked to a host it was not queued for"


def test_the_payload_carries_the_digest_the_agent_verifies(controller, agent, flashback):
    """The agent refuses to write bytes whose sha256 does not match, so the
    digest has to survive the relay."""
    import hashlib
    h1, s1 = agent("host-1", "secret-1", "web1")
    controller.post(f"/agents/{h1}/config-snapshot", headers=hdr(s1),
                    json={"files": files(("/etc/hosts", "v1-content"))})
    sha = flashback.list_versions("host-1", "/etc/hosts")[0]["sha256"]
    rid = flashback.queue_restore("host-1", "/etc/hosts", sha, "alice")["id"]

    r = controller.get(f"/agents/{h1}/config-restores/{rid}/payload", headers=hdr(s1))
    assert r.status_code == 200
    assert r.content == b"v1-content"
    assert r.headers["X-Flashback-Path"] == "/etc/hosts"
    assert r.headers["X-Flashback-Sha256"] == sha
    assert hashlib.sha256(r.content).hexdigest() == sha


def test_acking_clears_the_restore_and_is_recorded_in_the_activity_feed(controller, agent, flashback):
    """A restore CHANGED a file on a host — unlike a snapshot, it is attributed."""
    from backend import db
    h1, s1 = agent("host-1", "secret-1", "web1")
    controller.post(f"/agents/{h1}/config-snapshot", headers=hdr(s1),
                    json={"files": files(("/etc/hosts", "v1"))})
    sha = flashback.list_versions("host-1", "/etc/hosts")[0]["sha256"]
    rid = flashback.queue_restore("host-1", "/etc/hosts", sha, "alice")["id"]

    r = controller.post(f"/agents/{h1}/config-restores/{rid}/ack", headers=hdr(s1),
                        json={"ok": True, "path": "/etc/hosts"})
    assert r.status_code == 200
    assert controller.get(f"/agents/{h1}/config-restores", headers=hdr(s1)).json()["restores"] == []
    descs = [e["description"] for e in db.get_activity_log(limit=20)]
    assert any("restored a config file" in d for d in descs), descs


def test_a_failed_restore_is_recorded_as_a_failure(controller, agent, flashback):
    from backend import db
    h1, s1 = agent("host-1", "secret-1", "web1")
    controller.post(f"/agents/{h1}/config-snapshot", headers=hdr(s1),
                    json={"files": files(("/etc/hosts", "v1"))})
    sha = flashback.list_versions("host-1", "/etc/hosts")[0]["sha256"]
    rid = flashback.queue_restore("host-1", "/etc/hosts", sha, "alice")["id"]
    controller.post(f"/agents/{h1}/config-restores/{rid}/ack", headers=hdr(s1),
                    json={"ok": False, "path": "/etc/hosts"})
    descs = [e["description"] for e in db.get_activity_log(limit=20)]
    assert any("FAILED to restore" in d for d in descs), descs


# ---- degradation -----------------------------------------------------------
def test_an_unconfigured_controller_says_so_instead_of_failing_obscurely(controller, agent, monkeypatch):
    """A standalone Controller has no Flashback. The agent must get a reason it
    can log, not a 500 — and config backup being off must never break check-ins."""
    from backend import flashback as fbmod
    monkeypatch.setattr(fbmod, "FLASHBACK_URL", "")
    monkeypatch.setattr(fbmod, "AGENT_TOKEN", "")
    h1, s1 = agent("host-1", "secret-1", "web1")
    r = controller.post(f"/agents/{h1}/config-snapshot", headers=hdr(s1),
                        json={"files": files(("/etc/hosts", "a"))})
    assert r.status_code == 503
    d = r.json()["detail"]
    assert "SYSIBLE_FLASHBACK_URL" in d and "SYSIBLE_FLASHBACK_AGENT_TOKEN" in d


def test_an_unreachable_flashback_is_reported_without_leaking_the_token(controller, agent, monkeypatch):
    import requests as _rq
    from backend import flashback as fbmod
    monkeypatch.setattr(fbmod, "FLASHBACK_URL", "http://flashback.invalid")
    monkeypatch.setattr(fbmod, "AGENT_TOKEN", "super-secret-token")

    def boom(*a, **kw):
        raise _rq.ConnectionError("nope")
    monkeypatch.setattr(fbmod.requests, "request", boom)
    h1, s1 = agent("host-1", "secret-1", "web1")
    r = controller.post(f"/agents/{h1}/config-snapshot", headers=hdr(s1),
                        json={"files": files(("/etc/hosts", "a"))})
    assert r.status_code == 503
    assert "unreachable" in r.json()["detail"]
    assert "super-secret-token" not in r.text
