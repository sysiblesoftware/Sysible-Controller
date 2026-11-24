"""The patch screen and the dashboard must not give opposite answers.

Reported: the patch list said "2 offline" and named deb-web-1, greyed out and
unselectable — while the dashboard, at the same moment, said "Online 16 · 0
offline" and listed deb-web-1 as merely needing attention.

Both are reading the same fleet, and both apply the same rule: heard from within
_AGENT_ONLINE_SECONDS. The difference was WHEN. Counting packages is heavy, so
/api/fleet-updates is cached for 15 minutes — and the reachability verdict was
cached with it. A host briefly stale during one scan stayed labelled offline for
a quarter of an hour, next to a dashboard polling the live inventory.

Package counts may age; that is what the scan timestamp is for. Reachability is
one instant inventory call, so it is re-judged on every return.
"""
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
SRC = (REPO / "webgui" / "server.py").read_text(encoding="utf-8")


@pytest.fixture
def srv(monkeypatch):
    from webgui import server
    return server


def _agents(monkeypatch, srv, ages):
    """Pin the live inventory: {host_id: seconds since last heartbeat}."""
    import time
    now = time.time()
    monkeypatch.setattr(srv.api, "get_agents",
                        lambda: [{"host_id": k, "last_seen": now - v}
                                 for k, v in ages.items()])


def test_a_host_that_is_back_is_not_still_reported_offline(srv, monkeypatch):
    """The exact report. The scan skipped it; it is answering now."""
    _agents(monkeypatch, srv, {"h1": 2})
    stale = [{"id": "h1", "host": "deb-web-1", "online": False, "error": "offline",
              "total": None, "security": None}]
    row = srv._with_live_reachability(stale)[0]
    assert row["online"] is True, "a host heartbeating two seconds ago is reported offline"
    assert row["error"] != "offline", \
        "it still claims the host is offline while the host is answering"
    assert "not scanned" in row["error"], \
        "it drops the reason entirely, so the blank counts look like a real zero"


def test_a_host_that_really_is_gone_still_reads_offline(srv, monkeypatch):
    """The other half. Refreshing reachability must not whitewash a host that is
    genuinely down into looking fine."""
    _agents(monkeypatch, srv, {"h1": 9999})
    rows = srv._with_live_reachability(
        [{"id": "h1", "host": "deb-web-1", "online": True, "error": None}])
    assert rows[0]["online"] is False


def test_counts_are_never_rewritten(srv, monkeypatch):
    """Only reachability is cheap enough to refresh. Package counts come from the
    scan, and silently changing them here would make the timestamp a lie."""
    _agents(monkeypatch, srv, {"h1": 2})
    row = srv._with_live_reachability(
        [{"id": "h1", "host": "x", "online": False, "error": "offline",
          "total": 7, "security": 3, "mgr": "apt", "reboot": True}])[0]
    assert (row["total"], row["security"], row["mgr"], row["reboot"]) == (7, 3, "apt", True)


def test_an_ssh_only_host_is_left_alone(srv, monkeypatch):
    """No agent, so there is no heartbeat to judge it by. Marking it offline
    because the inventory has never heard of it would invent a fact."""
    _agents(monkeypatch, srv, {"h1": 2})
    rows = srv._with_live_reachability(
        [{"id": None, "host": "ssh-box", "online": None, "error": None}])
    assert rows[0]["online"] is None


def test_a_lost_inventory_does_not_blank_the_screen(srv, monkeypatch):
    """If the inventory call itself fails, the scan's own verdict is the best
    answer available — better than reporting the whole fleet as offline."""
    def boom():
        raise RuntimeError("controller unreachable")
    monkeypatch.setattr(srv.api, "get_agents", boom)
    given = [{"id": "h1", "host": "x", "online": True, "error": None}]
    assert srv._with_live_reachability(given) == given


def test_both_return_paths_go_through_it():
    """The cached path is the one that was wrong, but routing only that one
    through leaves two code paths to drift apart again."""
    block = SRC[SRC.index('@app.get("/api/fleet-updates")'):]
    block = block[:block.index('# ----------------------------------------------------------------------\n# Scheduled fleet jobs')]
    # A fixed COUNT breaks every time a return path is added, which says
    # nothing about whether the new one is correct -- and a stale-while-
    # revalidate path was added for exactly that reason. Check the property
    # instead: every return that carries hosts re-judges them.
    import re as _re
    returns = _re.findall(r"return \{[^}]*?\"hosts\":[^}]*?\}", block, _re.S)
    assert returns, "no returns found -- the slice is wrong"
    missing = [r for r in returns if "_with_live_reachability" not in r]
    assert not missing, (
        "a return hands back cached reachability:\n" + "\n".join(missing))


def test_the_posture_sweep_does_not_cache_reachability_either():
    """Found by scanning for the same shape, not by a second report.

    _POSTURE_CACHE holds rows for five minutes and each one carries `online`,
    so a host briefly stale during one sweep read as offline -- with no posture
    at all -- for the next five minutes. Worse than the patch screen: the
    dashboard grades remediation ELIGIBILITY from this flag, so a host that was
    answering again could not be acted on until the cache turned over.

    Posture findings are slow-moving and belong in a cache. Reachability does
    not.
    """
    block = SRC[SRC.index('@app.get("/api/fleet-posture")'):]
    block = block[:block.index("def _probe_metrics") if "def _probe_metrics" in block
                  else block.index("\n@app.")]
    assert block.count("_with_live_reachability") == 3, (
        "not every return of the posture sweep re-judges reachability: "
        f"found {block.count('_with_live_reachability')} of 3")


def test_the_online_window_is_defined_once():
    """It was a bare `20` repeated at six sites. Six copies of the rule that
    decides who is offline is how two screens come to disagree in the first
    place."""
    import re
    bare = re.findall(r"\(now - ls\)\s*<=\s*\d+", SRC)
    assert not bare, f"hardcoded heartbeat windows still in the server: {bare}"
    assert "_AGENT_ONLINE_SECONDS" in SRC
