"""An inventory the controller could not return is "I don't know", not "all down".

Reported as sixteen of sixteen hosts reading "not scanned — the host was
unreachable when this ran", directly beneath a strip saying 16/16 agents
online, and a dashboard tile saying 0 offline. Three places on one screen
disagreeing about the same fleet.

Six callers read the liveness inventory as

    try:    last_seen = {... for a in api.get_agents()}
    except: last_seen = {}

and every gate reads a missing entry as offline. So one failed call to the
controller -- a restart, a blip, a timeout -- did not degrade a sweep, it
INVERTED it: every host marked unreachable, every probe skipped. For the patch
sweep the result is then cached for fifteen minutes, so a momentary hiccup
became a quarter of an hour of a fleet that looked unscannable while it was
up and idle.

Probing a host that turns out to be down costs one timeout. Skipping a fleet
that is up fills the screen with confident wrong answers and caches them.
"""
import pathlib
import re
import sys

import pytest
from fastapi.testclient import TestClient

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
SRC = (REPO / "webgui" / "server.py").read_text(encoding="utf-8")


@pytest.fixture
def srv():
    import webgui.server as s
    return s


def test_an_unreadable_inventory_reads_as_unknown(srv, monkeypatch):
    def boom():
        raise RuntimeError("controller restarting")
    monkeypatch.setattr(srv.api, "get_agents", boom)
    assert srv._agent_last_seen() is None, \
        "a failed inventory still reads as an empty fleet, i.e. everyone offline"


def test_a_readable_inventory_is_still_a_mapping(srv, monkeypatch):
    monkeypatch.setattr(srv.api, "get_agents",
                        lambda: [{"host_id": "h1", "last_seen": 123.0}])
    assert srv._agent_last_seen() == {"h1": 123.0}


def test_the_patch_sweep_probes_instead_of_skipping_the_fleet(srv, monkeypatch):
    """The reported screen. With the inventory unavailable, every host must be
    ASKED -- not written off and cached as unreachable."""
    def boom():
        raise RuntimeError("controller restarting")
    monkeypatch.setattr(srv.api, "get_agents", boom)
    monkeypatch.setattr(srv.dispatch, "list_merged_hosts",
                        lambda **kw: [{"id": "h1", "kind": "agent", "label": "rocky-01"},
                                      {"id": "h2", "kind": "agent", "label": "opensuse-01"}])
    asked = []

    def fake_dispatch(entry, command, kind, *a, **kw):
        asked.append(entry["label"])
        return {"host": entry["label"], "ok": True, "code": 0,
                "stdout": "SYSUPDATES|mgr=zypper|total=3|security=1|reboot=0", "stderr": ""}
    monkeypatch.setattr(srv, "_dispatch_one", fake_dispatch)
    monkeypatch.setattr(srv, "_tokenless_entry", lambda e: e)
    srv._UPDATES_CACHE["hosts"] = None
    srv._UPDATES_CACHE["ts"] = 0.0

    rows = [srv._probe_updates(e, "cmd", None, 0.0)
            for e in srv.dispatch.list_merged_hosts(agent_only=True)]
    assert sorted(asked) == ["opensuse-01", "rocky-01"], \
        f"the sweep skipped hosts it could not confirm were up: asked {asked}"
    assert all(r.get("error") != "offline" for r in rows), rows
    assert [r["total"] for r in rows] == [3, 3], rows


def test_a_host_the_inventory_says_is_stale_is_still_skipped(srv, monkeypatch):
    """The gate must keep working when the inventory DOES answer -- otherwise
    this fix trades a false outage for a sweep that waits out every dead host."""
    monkeypatch.setattr(srv, "_dispatch_one",
                        lambda *a, **kw: pytest.fail("a known-offline host was probed"))
    row = srv._probe_updates({"id": "h1", "kind": "agent", "label": "deb-web-1"},
                             "cmd", {"h1": 0.0}, 10_000_000.0)
    assert row["error"] == "offline" and row["online"] is False


def test_every_reader_goes_through_the_helper():
    """Six copies is how one of them keeps the old behaviour."""
    leftover = re.findall(r"except Exception:\s*\n\s*last_seen = \{\}", SRC)
    assert not leftover, f"{len(leftover)} caller(s) still read a failure as an empty fleet"
    assert SRC.count("_agent_last_seen()") >= 6, \
        "not every sweep reads the inventory through the helper"


def test_every_gate_checks_that_it_knows_before_it_judges():
    """A gate that fires on `not online` alone cannot tell unknown from down."""
    bad = [m.start() for m in re.finditer(r"if aid is not None and not online:", SRC)]
    assert not bad, "a liveness gate still treats unknown as offline"
    for m in re.finditer(r"if last_seen is not None and aid is not None", SRC):
        pass
    assert SRC.count("if last_seen is not None and aid is not None") >= 5, \
        "not every gate was updated"
