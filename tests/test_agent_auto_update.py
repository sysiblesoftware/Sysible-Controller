"""Agents update themselves — automatically, and without stampeding.

A fleet drifts onto mixed agent builds whenever nobody remembers to press
"Update agents". The heartbeat already reports each host's build on every
check-in, so that is where the gap is closed: a host reporting an older build
gets the same self-update the manual push sends, queued for it alone.

The whole risk lives in the RATE. Agents heartbeat roughly every 1.5 seconds, so
"version differs -> queue a task" would enqueue tens of thousands of tasks a day
for any host that can't finish the update — filling the queue, the activity feed
and the disk. These tests pin the guard: at most ONE queued update per host per
target build, a cooldown before retrying that same build, and nothing at all for
a healthy or revoked host.
"""
import time

import pytest

import backend.app as app_mod
import backend.db as db


def _agent(host_id, version, secret=None):
    secret = secret or ("sec-" + host_id)
    db.create_or_update_agent(host_id, host_id, "linux", "6.1", "online",
                              time.time(), secret, "10.0.0.1")
    conn = db._connect()
    cur = conn.cursor()
    cur.execute("UPDATE agents SET agent_version=? WHERE host_id=?", (version, host_id))
    conn.commit()
    conn.close()
    return secret


def _update_tasks(host_id):
    conn = db._connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM agent_tasks WHERE host_id=? AND kind='agent-update'",
                (host_id,))
    n = cur.fetchone()[0]
    conn.close()
    return n


@pytest.fixture(autouse=True)
def _fresh_memo():
    """Each test starts with an empty per-host memo (it is process state)."""
    app_mod._auto_update_sent.clear()
    yield
    app_mod._auto_update_sent.clear()


def _beat(controller, host_id, secret, version):
    return controller.post("/agents/heartbeat", json={
        "host_id": host_id, "agent_secret": secret,
        "hostname": host_id, "ip": "10.0.0.1", "agent_version": version,
    })


# ---- the happy path --------------------------------------------------------
def test_an_outdated_agent_is_updated_on_its_next_heartbeat(controller, monkeypatch):
    monkeypatch.setattr(app_mod, "_AGENT_AUTO_UPDATE", True)
    monkeypatch.setattr(app_mod, "_current_agent_version", lambda: "newbuild123")
    secret = _agent("h1", "oldbuild000")

    assert _beat(controller, "h1", secret, "oldbuild000").status_code == 200
    assert _update_tasks("h1") == 1


def test_a_current_agent_is_left_alone(controller, monkeypatch):
    monkeypatch.setattr(app_mod, "_AGENT_AUTO_UPDATE", True)
    monkeypatch.setattr(app_mod, "_current_agent_version", lambda: "newbuild123")
    secret = _agent("h1", "newbuild123")

    assert _beat(controller, "h1", secret, "newbuild123").status_code == 200
    assert _update_tasks("h1") == 0


# ---- the rate guard (the part that matters) --------------------------------
def test_repeated_heartbeats_queue_exactly_one_update(controller, monkeypatch):
    """The failure this prevents: an agent that never applies the update keeps
    reporting the old build, once every ~1.5s, forever."""
    monkeypatch.setattr(app_mod, "_AGENT_AUTO_UPDATE", True)
    monkeypatch.setattr(app_mod, "_current_agent_version", lambda: "newbuild123")
    secret = _agent("h1", "oldbuild000")

    for _ in range(25):
        assert _beat(controller, "h1", secret, "oldbuild000").status_code == 200
    assert _update_tasks("h1") == 1


def test_the_same_build_is_retried_only_after_the_cooldown(controller, monkeypatch):
    monkeypatch.setattr(app_mod, "_AGENT_AUTO_UPDATE", True)
    monkeypatch.setattr(app_mod, "_current_agent_version", lambda: "newbuild123")
    monkeypatch.setattr(app_mod, "_AGENT_AUTO_UPDATE_RETRY_S", 1800)
    secret = _agent("h1", "oldbuild000")

    _beat(controller, "h1", secret, "oldbuild000")
    assert _update_tasks("h1") == 1
    # Pretend the cooldown has elapsed with the host still on the old build.
    ver, when = app_mod._auto_update_sent["h1"]
    app_mod._auto_update_sent["h1"] = (ver, when - 1801)
    _beat(controller, "h1", secret, "oldbuild000")
    assert _update_tasks("h1") == 2, "a stuck host should be retried once per cooldown"


def test_a_new_controller_build_re_arms_immediately(controller, monkeypatch):
    """The memo is keyed by TARGET build, so shipping a newer agent must not wait
    out the cooldown left over from the previous one."""
    monkeypatch.setattr(app_mod, "_AGENT_AUTO_UPDATE", True)
    monkeypatch.setattr(app_mod, "_current_agent_version", lambda: "build-A")
    secret = _agent("h1", "oldbuild000")
    _beat(controller, "h1", secret, "oldbuild000")
    assert _update_tasks("h1") == 1

    monkeypatch.setattr(app_mod, "_current_agent_version", lambda: "build-B")
    _beat(controller, "h1", secret, "oldbuild000")
    assert _update_tasks("h1") == 2


# ---- who is excluded -------------------------------------------------------
def test_a_revoked_host_is_never_auto_updated(controller, monkeypatch):
    monkeypatch.setattr(app_mod, "_AGENT_AUTO_UPDATE", True)
    monkeypatch.setattr(app_mod, "_current_agent_version", lambda: "newbuild123")
    secret = _agent("gone", "oldbuild000")
    db.revoke_agent("gone")

    # The heartbeat itself is rejected for a revoked agent; either way, no task.
    _beat(controller, "gone", secret, "oldbuild000")
    assert _update_tasks("gone") == 0


def test_an_agent_that_reports_no_version_is_left_alone(controller, monkeypatch):
    # Nothing to compare against — queueing blind would push an update at a host
    # on every single heartbeat, since the condition could never clear.
    monkeypatch.setattr(app_mod, "_AGENT_AUTO_UPDATE", True)
    monkeypatch.setattr(app_mod, "_current_agent_version", lambda: "newbuild123")
    secret = _agent("h1", "oldbuild000")

    _beat(controller, "h1", secret, None)
    assert _update_tasks("h1") == 0


def test_it_can_be_turned_off(controller, monkeypatch):
    monkeypatch.setattr(app_mod, "_AGENT_AUTO_UPDATE", False)
    monkeypatch.setattr(app_mod, "_current_agent_version", lambda: "newbuild123")
    secret = _agent("h1", "oldbuild000")

    _beat(controller, "h1", secret, "oldbuild000")
    assert _update_tasks("h1") == 0


# ---- it must never break the heartbeat -------------------------------------
def test_a_failure_while_queueing_does_not_fail_the_heartbeat(controller, monkeypatch):
    """The heartbeat is the most frequent request in the system and decides
    whether a host reads as online. An auto-update problem must cost an update,
    never a host."""
    monkeypatch.setattr(app_mod, "_AGENT_AUTO_UPDATE", True)
    monkeypatch.setattr(app_mod, "_current_agent_version", lambda: "newbuild123")

    def boom(*a, **k):
        raise RuntimeError("cannot build the update command")

    monkeypatch.setattr(app_mod, "_build_agent_update_command", boom)
    secret = _agent("h1", "oldbuild000")

    assert _beat(controller, "h1", secret, "oldbuild000").status_code == 200
    assert _update_tasks("h1") == 0


# ---- what the console is told ----------------------------------------------
def test_update_status_reports_that_auto_update_is_on(controller, superuser_headers,
                                                      monkeypatch):
    monkeypatch.setattr(app_mod, "_AGENT_AUTO_UPDATE", True)
    r = controller.get("/update-status", headers=superuser_headers)
    assert r.status_code == 200
    assert r.json()["agents"]["auto_update"] is True
