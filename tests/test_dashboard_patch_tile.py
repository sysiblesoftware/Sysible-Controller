"""The dashboard's "Needs patching" tile.

Asked for as "a small thing that shows if hosts need patching on the main
dashboard" — with a screenshot of the dashboard that has no such tile on it.

The tile existed. It was rendered behind `patch.loaded`, and `loaded` was
`updates.length > 0`, so it was hidden whenever the fleet-updates sweep had
returned nothing. That is not a rare state: the sweep runs off a 15-minute cache,
a cold cache makes the call a full fleet probe that can take minutes, and the
`.catch(() => {})` around it turned a failure into the same empty list. So the
indicator was missing in exactly the three cases it is for — never scanned, still
scanning, and the scan failed — and indistinguishable from "nothing to patch".

It now always renders, in four states, each driven in a real browser against the
shipping Dashboard with a stubbed API:

    scanning   Needs patching | scanning…
    pending    Needs patching | 2 | · 4 sec | · 1 reboot
    clear      Needs patching | 0                        (green)
    error      Needs patching | — | couldn't read patch status

Measured at 2000/1600/1280px: five cards, one row. The strip was a fixed
four-column grid, which left the fifth card alone on a full-width row of its own.

There is no JS test runner in this repo, so these hold the rules.
"""
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "webgui" / "frontend" / "src"
VIEW = SRC / "views" / "Dashboard.jsx"
CSS = SRC / "styles.css"


@pytest.fixture(scope="module")
def src():
    return VIEW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def card(src):
    m = re.search(r"function PatchCard\(\{(.+?)\n\}\n", src, re.S)
    assert m, "the patch tile is gone"
    return m.group(1)


# ---- the reported gap: it has to be on the page ----------------------------
def test_the_tile_is_not_hidden_behind_having_data(src):
    """`{patch.loaded && (...)}` is why it was not on the dashboard at all."""
    assert "patch.loaded" not in src, \
        "the patch tile is conditional on the sweep having returned rows again"
    assert re.search(r"<PatchCard\b", src), "the patch tile is not rendered"


def test_having_no_rows_is_not_the_same_as_having_nothing_to_patch(src):
    """A fully-patched fleet reports every host with total 0 — rows, all clear.
    A sweep that has not run reports no rows at all. Reading the second as the
    first is what made the tile silently disappear."""
    m = re.search(r"scanned: (.+?),", src)
    assert m, "the tile no longer distinguishes scanned from empty"
    assert "updState" in m.group(1), \
        f"'scanned' is derived from the row count again: {m.group(1)}"
    assert "updates.length" not in m.group(1)


@pytest.mark.parametrize("state", ["scanning", "ready", "error"])
def test_every_state_is_represented(card, state):
    assert state in card, f"the tile cannot show the {state} state"


def test_a_clear_fleet_reads_as_clear_not_as_absent(card):
    """Zero is an answer, and a useful one — it means the sweep ran."""
    assert re.search(r"clear = state === \"ready\" && withUpd === 0", card)
    assert "VERDICT_COLOR.OK" in card, "a fully-patched fleet is not marked as such"


# ---- a failure must not read as "nothing to patch" -------------------------
def test_a_failed_sweep_is_not_swallowed(src):
    """`.catch(() => {})` left the tile showing the last good number — or
    nothing — with no sign that the fleet had not actually been asked."""
    m = re.search(r"api\.fleetUpdates\(0, 0\)(.+?)\.finally", src, re.S)
    assert m, "the patch fetch changed shape"
    body = m.group(1)
    assert re.search(r"\.catch\(\(e\) =>", body), "the sweep's failure is discarded again"
    assert 'setUpdState("error")' in body


def test_the_failure_says_what_went_wrong(card, src):
    assert "couldn’t read patch status" in card or "couldn't read patch status" in card
    assert "${err}" in card, "the error text never reaches the operator"


# ---- and it must not pile sweeps on top of each other ----------------------
def test_two_sweeps_cannot_run_at_once(src):
    """A cold cache makes this a full fleet probe that can outlast the 30s poll.
    Without a guard the next poll starts another one on top of it, and on a
    sixteen-host fleet they stack up faster than they finish — which is what
    "the controller is timing out a lot" looks like from the inside."""
    assert "updInFlight" in src, "overlapping fleet sweeps are possible again"
    m = re.search(r"const loadUpdates = useCallback\(\(\) => \{(.+?)\n  \}, \[\]\)", src, re.S)
    assert m, "the patch fetch is gone"
    body = m.group(1)
    assert "if (updInFlight.current) return;" in body, "the in-flight guard does not short-circuit"
    assert ".finally(" in body and "updInFlight.current = false" in body, \
        "a failed sweep would leave the guard stuck on, so the tile never updates again"


def test_the_dashboard_does_not_force_a_rescan_on_every_poll(src):
    """refresh=1 here would re-probe the whole fleet every 30 seconds, from every
    open dashboard. The tile reads what the sweep cache has."""
    assert "api.fleetUpdates(0, 0)" in src, \
        "the dashboard is asking for a forced refresh on its poll"


# ---- what the number actually counts ---------------------------------------
def test_it_counts_hosts_with_updates_not_updates(src):
    m = re.search(r"const patch = useMemo\(\(\) => \{(.+?)\n  \}, ", src, re.S)
    assert m, "the patch summary is gone"
    body = m.group(1)
    assert "if ((h.total || 0) > 0) withUpd++" in body
    assert "sec += h.security || 0" in body, "the security count changed shape"
    assert "if (h.reboot) reboot++" in body, \
        "hosts waiting on a reboot are no longer counted"


def test_security_and_reboot_are_shown_as_their_own_thing(card):
    """'12 hosts need patching' and 'one of them needs a reboot to finish' are
    different jobs, and the second is the one that gets forgotten."""
    assert "VERDICT_COLOR.CRITICAL" in card and "sec" in card
    assert "VERDICT_COLOR.WARNING" in card and "reboot" in card


def test_the_tile_opens_the_page_that_can_act_on_it(src, card):
    assert 'onOpen={() => onOpen("updates")}' in src, \
        "the tile no longer leads anywhere"
    assert "onClick={onOpen}" in card


# ---- the strip has to hold five cards --------------------------------------
def test_the_metric_strip_does_not_orphan_the_fifth_card():
    """A fixed four-column grid put the fifth card alone on a full-width row:
    one number, on its own line, under four that share one."""
    css = CSS.read_text(encoding="utf-8")
    m = re.search(r"\.metric-row \{([^}]*)\}", css)
    assert m, ".metric-row is gone"
    rule = m.group(1)
    assert "auto-fit" in rule, \
        f"the metric strip is a fixed column count again: {rule.strip()}"
    assert "repeat(4, 1fr)" not in rule
