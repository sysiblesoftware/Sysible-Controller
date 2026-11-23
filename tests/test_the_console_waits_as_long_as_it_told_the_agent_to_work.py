"""The console must not give up before the host-side budget it granted.

Reported as "the agent is timing out on almost every host", with a patch screen
where 9 of 16 rows read "timed out waiting for agent" — and the hosts that
answered were the fast package managers (pacman, apt) while the ones that did
not were the slow ones (dnf, zypper). That is not an agent fault.

The console's patience (`poll_timeout`) and the host's time budget
(`exec_timeout`) were two independent decisions, and in four of five call sites
the console gave up first:

    fleet health     waits  8s   grants 60s
    fleet query      waits 20s   grants 60s
    posture          waits 30s   grants 60s
    patch sweep      waits 60s   grants the agent's 1800s DEFAULT (no bound)

The mislabelling is the small half. _host_bounded's own docstring has the large
half: the agent's task loop is SERIAL, so a probe the console walked away from
keeps running and blocks every task queued behind it on that host. The next
sweep then times out too, and the one after that — a cascade that spreads across
the fleet and does not clear on its own.

So the invariant: whenever a call bounds host-side work, the agent's own
`timeout` must fire BEFORE the console stops listening. Then the task ends, the
result is a true statement about the host, and the queue is freed.
"""
import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
SRC = (REPO / "webgui" / "server.py").read_text(encoding="utf-8")


def _reconcile():
    from webgui.server import _reconcile_timeouts
    return _reconcile_timeouts


@pytest.mark.parametrize("wait,grant", [(8, 60), (20, 60), (30, 60), (60, 1800), (900, 900)])
def test_the_agent_always_gives_up_first(wait, grant):
    poll, exec_ = _reconcile()(wait, grant)
    assert exec_ < poll, f"the console stops listening at {poll}s with {exec_}s still granted"
    from webgui.server import _DISPATCH_MARGIN_S
    assert poll - exec_ >= _DISPATCH_MARGIN_S, (
        "no room for the agent to pick the task up and upload a result before "
        "the console walks away")


@pytest.mark.parametrize("wait,grant", [(20, 60), (30, 60), (60, 1800), (900, 900)])
def test_the_budget_shrinks_rather_than_the_wait_stretching(wait, grant):
    """WHICH ONE GIVES. Satisfying the invariant by stretching the console's
    patience to 65s also satisfies "the agent gives up first" -- and leaves a
    dashboard health sweep waiting a minute on one bad host, plus the serial
    queue occupied for the whole of it. The point is to free the queue.

    Found by exactly that mutation: replacing the shrink with the raw budget
    left every other test in this file green.
    """
    from webgui.server import _DISPATCH_EXEC_FLOOR_S, _DISPATCH_MARGIN_S
    poll, exec_ = _reconcile()(wait, grant)
    assert poll <= max(wait, _DISPATCH_EXEC_FLOOR_S + _DISPATCH_MARGIN_S), (
        f"the wait was stretched from {wait}s to {poll}s instead of shrinking "
        f"the host's budget")
    assert exec_ < grant, "the budget was not shrunk at all"


def test_a_mutating_action_is_never_bounded_here():
    """Actions that may legitimately run long leave exec_timeout unset on
    purpose. Bounding them here would cut off real work — an update install
    killed at 55s is far worse than a probe mislabelled."""
    poll, exec_ = _reconcile()(60, None)
    assert exec_ is None, "a mutating action was given a host-side bound"
    assert poll == 60, "its wait was changed"


def test_an_impatient_caller_does_not_get_an_unusable_probe():
    """When the wait is so short that even the floor will not fit, the WAIT is
    what gives. Shrinking the budget to nothing would dispatch a probe that
    cannot finish and call the host broken for it."""
    from webgui.server import _DISPATCH_EXEC_FLOOR_S, _DISPATCH_MARGIN_S
    poll, exec_ = _reconcile()(2, 60)
    assert exec_ >= _DISPATCH_EXEC_FLOOR_S, exec_
    assert poll >= exec_ + _DISPATCH_MARGIN_S, (poll, exec_)


def test_the_budget_is_never_stretched_beyond_what_was_asked():
    """It only ever shrinks. A caller asking for 5s of host time must not find
    the host given 55s because the wait happened to be long."""
    poll, exec_ = _reconcile()(900, 5)
    assert exec_ == 5, exec_


def test_dispatch_applies_the_reconciliation_before_bounding():
    """Order matters: _host_bounded stamps the number into the shell command, so
    reconciling afterwards would wrap the command with the UNRECONCILED budget
    and leave the whole thing exactly as broken."""
    block = SRC[SRC.index("    label = entry[\"label\"]\n    env = entry.get(\"environment\")"):]
    block = block[:block.index("    try:")]
    assert "_reconcile_timeouts" in block, "_dispatch_one does not reconcile at all"
    assert block.index("_reconcile_timeouts") < block.index("_host_bounded"), \
        "the command is bounded with the unreconciled budget"


def test_the_patch_sweep_bounds_the_host():
    """The sweep that caused the report. It passed no exec_timeout, so an
    overrun kept running for up to 1800s on a serial queue — which is what turns
    one slow host into a fleet of timeouts."""
    block = SRC[SRC.index("def _probe_updates"):]
    block = block[:block.index("\n@app.get(\"/api/fleet-updates\")")]
    assert "exec_timeout=exec_timeout" in block, \
        "the patch probe still leaves host-side work unbounded"
    call = SRC[SRC.index("ptmo = int(os.getenv(\"SYSIBLE_UPDATE_PROBE_TIMEOUT\""):]
    call = call[:call.index("_UPDATES_CACHE[\"hosts\"] = hosts")]
    assert re.search(r"_probe_updates\(e, cmd, last_seen, now, ptmo, ptmo\)", call), \
        "the sweep does not hand the probe a host-side budget"


def test_no_call_site_waits_less_than_it_grants():
    """A source scan over every literal pair, so a NEW call site with the old
    shape fails here rather than becoming another fleet of false timeouts.

    The runtime reconciliation would silently rescue such a pair, which is
    exactly why this is checked separately: silently rescued is still wrong, and
    the next reader needs the numbers to mean what they say.
    """
    bad = []
    for m in re.finditer(r"poll_timeout=([^,\n]+),\s*\n?\s*exec_timeout=([^,)\n]+)", SRC):
        poll, ex = m.group(1), m.group(2)
        nums = [re.search(r'"(\d+)"\)|^(\d+)$', t.strip()) for t in (poll, ex)]
        if not all(nums):
            continue
        p_v, e_v = (int(n.group(1) or n.group(2)) for n in nums)
        if e_v > p_v:
            bad.append(f"line {SRC[:m.start()].count(chr(10)) + 1}: waits {p_v}s, grants {e_v}s")
    assert not bad, "call sites that give up before the work they authorized:\n" + "\n".join(bad)
