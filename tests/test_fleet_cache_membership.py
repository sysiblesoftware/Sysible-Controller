"""A sweep cache has to describe the fleet it is being read about.

Reported: "Hosts not showing up in the updates hosts menu" — the dashboard said
16 hosts enrolled, 16 online, while Update Hosts listed 3 and declared "All 3
hosts up to date". Then: "they eventually all showed up but it took a very long
time."

Nothing was dropping hosts. The fleet sweeps cache their result and judged that
cache FRESH on age alone, so a host enrolled after the last sweep was missing
from the page for the whole TTL — 15 minutes for updates, 5 for posture — while
the dashboard beside it reads the instant inventory and had the host
immediately. Two screens in the same process, disagreeing, with nothing to
suggest the newer one was merely stale; "eventually" is the TTL expiring.

So freshness now means young AND covering exactly the hosts enrolled now. The
tests that matter most here are the two that keep the cure from being worse than
the disease: a cache that still covers the fleet must still be served (or every
page load re-sweeps the fleet), and a controller that cannot be asked must not
invalidate anything (a stale list beats a 502).
"""
import time

import pytest

import webgui.server as srv


HOSTS_3 = ["hid-00", "hid-01", "hid-02"]
HOSTS_16 = [f"hid-{i:02d}" for i in range(16)]


def _rows(ids):
    return [{"id": i, "label": i, "environment": "Dev", "online": True,
             "total": 0, "security": 0, "reboot": False} for i in ids]


@pytest.fixture()
def fleet(monkeypatch):
    """Control what the controller says is enrolled, without a controller."""
    state = {"ids": list(HOSTS_16), "fail": False, "swept": 0}

    def _list_merged_hosts(agent_only=True):
        if state["fail"]:
            raise RuntimeError("controller unreachable")
        return [{"kind": "agent", "id": i, "label": i, "type_text": "Agent",
                 "address": i, "environment": "Dev"} for i in state["ids"]]

    monkeypatch.setattr(srv.dispatch, "list_merged_hosts", _list_merged_hosts)
    monkeypatch.setattr(srv.api, "get_agents",
                        lambda: [{"host_id": i, "last_seen": time.time()} for i in state["ids"]])

    def _probe(e, *a, **k):
        state["swept"] += 1
        return {"id": e["id"], "label": e["label"], "environment": "Dev",
                "online": True, "total": 0, "security": 0, "reboot": False}

    monkeypatch.setattr(srv, "_probe_updates", _probe)
    monkeypatch.setattr(srv, "_probe_posture", _probe)
    monkeypatch.setattr(srv.api, "cmd_update_status", lambda refresh=False: "true")
    monkeypatch.setattr(srv, "_posture_command", lambda: "true")
    return state


@pytest.fixture(autouse=True)
def _clear_caches():
    for c in (srv._UPDATES_CACHE, srv._POSTURE_CACHE):
        c["hosts"] = None
        c["ts"] = 0.0
    # The fleet-id memo is module state too. It is short-lived in production (a
    # few seconds), but across tests it would carry one test's fleet into the
    # next and make a membership change look already-covered.
    srv._FLEET_IDS_CACHE["ids"] = None
    srv._FLEET_IDS_CACHE["ts"] = 0.0
    yield


# ---- the reported bug ------------------------------------------------------
def test_a_cache_from_before_an_enrollment_is_not_fresh(fleet):
    """The exact state behind the screenshot: the last sweep saw 3 hosts, 13
    more have enrolled since, and the cache is only a minute old."""
    srv._UPDATES_CACHE["hosts"] = _rows(HOSTS_3)
    srv._UPDATES_CACHE["ts"] = time.time() - 60      # well inside the 900s TTL

    r = srv.fleet_updates(refresh=0, live=0, user="admin")

    assert len(r["hosts"]) == 16, \
        "the page still showed only the hosts the last sweep happened to cover"
    assert r["cached"] is False, "a cache that does not cover the fleet was served as fresh"


def test_posture_has_the_same_guard(fleet):
    srv._POSTURE_CACHE["hosts"] = _rows(HOSTS_3)
    srv._POSTURE_CACHE["ts"] = time.time() - 60      # inside the 300s TTL
    r = srv.fleet_posture(refresh=0, user="admin")
    assert len(r["hosts"]) == 16 and r["cached"] is False


def test_a_removed_host_also_invalidates(fleet):
    """Coverage is equality, not 'contains' — a decommissioned host must stop
    being reported as part of the fleet just as promptly."""
    srv._UPDATES_CACHE["hosts"] = _rows(HOSTS_16)
    srv._UPDATES_CACHE["ts"] = time.time() - 60
    fleet["ids"] = list(HOSTS_3)

    r = srv.fleet_updates(refresh=0, live=0, user="admin")

    assert len(r["hosts"]) == 3 and r["cached"] is False


# ---- ...without making the cure worse than the disease ---------------------
def test_a_cache_that_still_covers_the_fleet_is_still_served(fleet):
    """If this breaks, every single page load re-sweeps the whole fleet — which
    is the slow thing the operator was complaining about in the first place."""
    srv._UPDATES_CACHE["hosts"] = _rows(HOSTS_16)
    srv._UPDATES_CACHE["ts"] = time.time() - 60
    before = fleet["swept"]

    r = srv.fleet_updates(refresh=0, live=0, user="admin")

    assert r["cached"] is True, "a cache covering the whole fleet was thrown away"
    assert fleet["swept"] == before, "the fleet was re-probed for nothing"


def test_an_unreachable_controller_does_not_discard_the_cache(fleet):
    """We cannot tell what is enrolled, so we cannot claim the cache is wrong.
    A stale list beats a 502 on a page whose whole job is showing host state."""
    srv._UPDATES_CACHE["hosts"] = _rows(HOSTS_16)
    srv._UPDATES_CACHE["ts"] = time.time() - 60
    fleet["fail"] = True

    r = srv.fleet_updates(refresh=0, live=0, user="admin")

    assert r["cached"] is True and len(r["hosts"]) == 16


def test_an_expired_cache_still_re_sweeps(fleet):
    """The age rule has to survive the new one."""
    srv._UPDATES_CACHE["hosts"] = _rows(HOSTS_16)
    srv._UPDATES_CACHE["ts"] = time.time() - (srv._UPDATES_TTL + 5)

    r = srv.fleet_updates(refresh=0, live=0, user="admin")

    assert r["cached"] is False and len(r["hosts"]) == 16


def test_refresh_still_bypasses_the_cache(fleet):
    srv._UPDATES_CACHE["hosts"] = _rows(HOSTS_16)
    srv._UPDATES_CACHE["ts"] = time.time()
    r = srv.fleet_updates(refresh=1, live=0, user="admin")
    assert r["cached"] is False


# ---- ...and the membership check must not become its own load ---------------
def test_cached_reads_do_not_hammer_the_controller(fleet, monkeypatch):
    """The dashboard polls fleet-health every 10 seconds, from every open tab.

    A cached read used to cost ZERO controller calls. Checking fleet membership
    on every read turned each of those polls into an inventory round-trip —
    measured at 20 calls for 20 cached polls — which is a steady stream of work
    the controller does not need while it is also serving every agent's poll.
    The lookup is memoized for a few seconds so a burst of pollers costs one.
    """
    srv._FLEET_IDS_CACHE["ids"] = None
    srv._FLEET_IDS_CACHE["ts"] = 0.0
    calls = {"n": 0}
    inner = srv.dispatch.list_merged_hosts

    def counted(agent_only=True):
        calls["n"] += 1
        return inner(agent_only=agent_only)

    monkeypatch.setattr(srv.dispatch, "list_merged_hosts", counted)

    srv.fleet_updates(refresh=0, live=0, user="admin")   # prime the sweep cache
    calls["n"] = 0
    for _ in range(20):
        r = srv.fleet_updates(refresh=0, live=0, user="admin")
        assert r["cached"] is True
    assert calls["n"] <= 1, (
        f"{calls['n']} controller inventory reads for 20 cached polls — the "
        f"membership check is not memoized")


def test_a_refresh_does_not_bother_asking(fleet, monkeypatch):
    """When we are re-sweeping regardless, the membership answer changes
    nothing — so it is not worth a round-trip."""
    srv._FLEET_IDS_CACHE["ids"] = None
    srv._FLEET_IDS_CACHE["ts"] = 0.0
    seen = {"n": 0}
    monkeypatch.setattr(srv, "_fleet_ids_or_none",
                        lambda: (seen.__setitem__("n", seen["n"] + 1), {"x"})[1])
    srv.fleet_updates(refresh=1, live=0, user="admin")
    assert seen["n"] == 0


def test_the_memo_never_pins_an_unreachable_controller(fleet, monkeypatch):
    """A blip returns None (unknown), and unknown must not be remembered — the
    next read has to be free to find out the truth."""
    srv._FLEET_IDS_CACHE["ids"] = None
    srv._FLEET_IDS_CACHE["ts"] = 0.0
    fleet["fail"] = True
    assert srv._fleet_ids_or_none() is None
    assert srv._FLEET_IDS_CACHE["ids"] is None, "a failed lookup was memoized"
    fleet["fail"] = False
    assert srv._fleet_ids_or_none() == set(HOSTS_16)
