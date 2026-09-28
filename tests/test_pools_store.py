"""What a pool is allowed to be.

A pool says which hosts must not all go down together. Every rule the rolling
engine relies on is only as good as the pool it is handed, and a pool can arrive
from a PATCH as easily as from the form — so the refusals live here, at the point
the pool is written down, rather than at 02:00 when the run finds out.

The one that matters most is the arithmetic: a pool where max_unavailable leaves
nothing serving is an outage someone has scheduled without noticing. It is
rejected when it is saved, with the numbers in the message.
"""
import json

import pytest


@pytest.fixture()
def pools(tmp_path, monkeypatch):
    """A fresh store per test — the module keeps its file path at import time."""
    monkeypatch.setenv("SYSIBLE_RUN_DIR", str(tmp_path))
    import importlib

    from webgui import pools as mod
    mod = importlib.reload(mod)
    return mod


def _pool(**over):
    p = {"name": "web", "members": ["web-1", "web-2", "web-3"], "provider": "none",
         "health": {"kind": "agent"}, "max_unavailable": 1, "min_healthy": 1}
    p.update(over)
    return p


# ---- the arithmetic that keeps a pool serving ------------------------------
def test_a_pool_that_cannot_keep_anything_serving_is_refused(pools):
    with pytest.raises(ValueError) as e:
        pools.create_pool(_pool(members=["web-1", "web-2"], max_unavailable=2), "admin")
    msg = str(e.value)
    assert "2-member pool" in msg and "below the 1" in msg, msg
    assert "Lower max_unavailable" in msg, "the refusal does not say what to do about it"


def test_the_arithmetic_is_rechecked_on_an_edit(pools):
    """The form can be trusted; a PATCH cannot. Raising max_unavailable on an
    existing pool is the easy way to schedule an outage."""
    p = pools.create_pool(_pool(), "admin")
    with pytest.raises(ValueError):
        pools.update_pool(p["id"], {"max_unavailable": 3})
    assert pools.get_pool(p["id"])["max_unavailable"] == 1, \
        "a rejected edit was stored anyway"


def test_a_rejected_edit_leaves_the_working_pool_alone(pools):
    """Validated before it replaces the stored one, so a bad edit cannot leave a
    half-applied pool behind."""
    p = pools.create_pool(_pool(), "admin")
    with pytest.raises(ValueError):
        pools.update_pool(p["id"], {"members": ["web-1", "web-1"], "name": "renamed"})
    still = pools.get_pool(p["id"])
    assert still["name"] == "web" and still["members"] == ["web-1", "web-2", "web-3"]


def test_a_bigger_pool_may_take_more_out(pools):
    p = pools.create_pool(_pool(members=[f"web-{i}" for i in range(6)],
                                max_unavailable=3), "admin")
    assert p["max_unavailable"] == 3


def test_min_healthy_is_respected_not_just_the_last_member(pools):
    """A pool told to keep two serving cannot have four of six taken out."""
    with pytest.raises(ValueError):
        pools.create_pool(_pool(members=[f"web-{i}" for i in range(6)],
                                max_unavailable=5, min_healthy=2), "admin")


# ---- the roll order --------------------------------------------------------
def test_a_host_listed_twice_is_refused(pools):
    """It would be drained twice in one run, and counted as two members' worth of
    capacity that is really one machine."""
    with pytest.raises(ValueError) as e:
        pools.create_pool(_pool(members=["web-1", "web-2", "web-1"]), "admin")
    assert "more than once" in str(e.value) and "web-1" in str(e.value)


def test_a_one_member_pool_is_refused(pools):
    with pytest.raises(ValueError) as e:
        pools.create_pool(_pool(members=["only-1"]), "admin")
    assert "at least two members" in str(e.value)


def test_the_member_order_is_kept(pools):
    """It is the roll order, not a set — an operator who puts the standby last
    means it."""
    order = ["web-3", "web-1", "web-2"]
    p = pools.create_pool(_pool(members=order), "admin")
    assert pools.get_pool(p["id"])["members"] == order


# ---- providers -------------------------------------------------------------
def test_a_custom_provider_needs_both_halves(pools):
    """A drain with no undrain removes a node from the pool permanently."""
    with pytest.raises(ValueError) as e:
        pools.create_pool(_pool(provider="command", drain_cmd="drain.sh"), "admin")
    assert "drain and an undrain" in str(e.value)


def test_an_unknown_provider_is_refused(pools):
    for bad in ("f5", "haproxy", "keepalived"):
        with pytest.raises(ValueError):
            pools.create_pool(_pool(provider=bad), "admin")


def test_only_the_providers_that_are_wired_are_offered(pools):
    """A provider in this list is one an operator can pick in the form. Listing
    haproxy before the drain actually reaches every balancer would mean a node
    that reads as drained and is still taking traffic from the two nobody told."""
    assert set(pools.PROVIDERS) == {"none", "command"}


def test_no_balancer_is_a_valid_pool(pools):
    """Most of the value — one host at a time, with a health gate — needs no
    integration at all, and a pool nobody can define is a pool nobody uses."""
    p = pools.create_pool(_pool(provider="none"), "admin")
    assert p["provider"] == "none"


# ---- free-form configuration is root, and is marked as such ----------------
def test_a_command_provider_is_flagged_as_superuser_only(pools):
    """Its drain runs as root on every member, which sidesteps the sudo policy
    that constrains a sysadmin everywhere else — the same reason run_command is
    restricted."""
    p = _pool(provider="command", drain_cmd="a", undrain_cmd="b")
    assert pools.needs_superuser(p) is True


def test_a_command_health_check_is_flagged_too(pools):
    p = _pool(health={"kind": "command", "target": "systemctl is-active nginx"})
    assert pools.needs_superuser(p) is True


def test_an_ordinary_pool_is_not_superuser_only(pools):
    assert pools.needs_superuser(_pool()) is False


# ---- health checks ---------------------------------------------------------
def test_a_health_check_that_needs_a_target_must_have_one(pools):
    for kind in ("tcp", "http", "command"):
        with pytest.raises(ValueError):
            pools.create_pool(_pool(health={"kind": kind}), "admin")


def test_a_tcp_check_wants_a_port_number(pools):
    with pytest.raises(ValueError) as e:
        pools.create_pool(_pool(health={"kind": "tcp", "target": "https"}), "admin")
    assert "port number" in str(e.value)
    with pytest.raises(ValueError):
        pools.create_pool(_pool(health={"kind": "tcp", "target": "70000"}), "admin")
    assert pools.create_pool(_pool(health={"kind": "tcp", "target": "443"}), "admin")


def test_an_unknown_health_check_is_refused(pools):
    with pytest.raises(ValueError):
        pools.create_pool(_pool(health={"kind": "vibes"}), "admin")


# ---- storage ---------------------------------------------------------------
def test_pools_round_trip_through_the_file(pools, tmp_path):
    a = pools.create_pool(_pool(name="web"), "admin")
    b = pools.create_pool(_pool(name="db", members=["db-1", "db-2"]), "admin")
    assert {p["name"] for p in pools.list_pools()} == {"web", "db"}
    assert a["id"] != b["id"]
    on_disk = json.loads((tmp_path / "webgui_pools.json").read_text())
    assert len(on_disk) == 2


def test_deleting_one_leaves_the_others(pools):
    a = pools.create_pool(_pool(name="web"), "admin")
    pools.create_pool(_pool(name="db", members=["db-1", "db-2"]), "admin")
    assert pools.delete_pool(a["id"]) is True
    assert [p["name"] for p in pools.list_pools()] == ["db"]
    assert pools.delete_pool(a["id"]) is False


def test_a_missing_pool_reads_as_none(pools):
    assert pools.get_pool("nope") is None
    assert pools.update_pool("nope", {"name": "x"}) is None


def test_who_made_it_is_recorded(pools):
    """These run root commands unattended; the activity trail needs a name."""
    p = pools.create_pool(_pool(), "alice")
    assert p["created_by"] == "alice" and p["created_ts"] > 0
