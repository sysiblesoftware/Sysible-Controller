"""Online and Offline / stale, as one tile.

Asked for directly: "combine these 2 panels", with a screenshot of the two sitting
side by side reading 16 and 0.

They were two cards for one fact. Every enrolled host is in exactly one of them,
so the pair always summed to "Hosts enrolled" immediately to their left — two
panels and three numbers carrying one piece of information, and the panel that
mattered (offline) was the one that read 0 almost all the time.

Combined, the big number stays "how many are up" and offline becomes a suffix:
it is the exception, and on this strip 0 is the good answer, so it should read
like a footnote until it is not. Driven in a browser with one host down:

    Online | 3 | · 1 offline        ("1 offline" in red; muted grey at 0)

The drill-down now holds both groups, which is the part that needs care — a list
of hosts with no way to tell which are down is worse than the two lists it
replaced. Offline hosts come first, and every row carries its own state dot.
"""
import re
from pathlib import Path

import pytest

VIEW = (Path(__file__).resolve().parents[1]
        / "webgui" / "frontend" / "src" / "views" / "Dashboard.jsx")


@pytest.fixture(scope="module")
def src():
    return VIEW.read_text(encoding="utf-8")


def _card(src, label):
    """The whole <MetricCard label="..."> element.

    Sliced, not matched with a non-greedy regex: the card contains self-closing
    <span ... /> elements, so `(.+?)/>` stops inside it and the assertions then
    read a fragment that never contains what they are looking for.
    """
    i = src.index(f'<MetricCard label="{label}"')
    j = src.index("onOpenHost={openHost} />", i)
    return src[i:j + len("onOpenHost={openHost} />")]


@pytest.fixture(scope="module")
def strip(src):
    m = re.search(r'<div className="metric-row">(.+?)\n      </div>', src, re.S)
    assert m, "the metric strip is gone"
    return m.group(1)


# ---- the ask ---------------------------------------------------------------
def test_there_is_no_longer_a_separate_offline_card(strip):
    assert 'label="Offline / stale"' not in strip, \
        "Online and Offline are two cards again"


def test_the_online_card_carries_the_offline_count(strip):
    card = _card(strip, "Online")
    assert "m.offline" in card, "the combined tile does not report offline hosts"
    assert "offline" in card


def test_the_offline_count_is_quiet_until_it_is_not(strip):
    """0 offline is the normal state on a healthy fleet. Painting it red all the
    time is how a strip stops being read."""
    card = _card(strip, "Online")
    assert re.search(r"m\.offline > 0 \? VERDICT_COLOR\.CRITICAL : \"var\(--text-dim\)\"", card), \
        "the offline count is coloured the same whether or not anything is offline"


def test_the_strip_still_shows_the_total_beside_it(strip):
    """Online alone is not a fleet size. 'Hosts enrolled' is what makes '3' mean
    something, and the two must keep coming from the same sweep."""
    assert 'label="Hosts enrolled"' in strip


# ---- the part that combining could have broken -----------------------------
def test_the_drill_down_covers_both_groups(src):
    assert "hostLists.combined" in _card(src, "Online"), \
        "the combined tile still drills into only one of the two groups"


def test_offline_hosts_come_first_in_the_drill_down(src):
    """The big number is how many are up; the reason anyone opens the list is to
    find the ones that are not."""
    m = re.search(r"const combined = (.+?);\n", src, re.S)
    assert m, "the combined host list is gone"
    expr = m.group(1)
    assert expr.index("offline") < expr.index("online"), \
        "online hosts are listed before the offline ones the list is opened for"
    assert "off: true" in expr and "off: false" in expr, \
        "the rows do not carry which group they came from"


def test_every_row_says_which_state_it_is(src):
    """Two labelled lists became one. Without a per-row marker it is a list of
    hosts with no way to tell the down ones from the up ones."""
    assert "h.off !== undefined" in src, \
        "the drill-down rows no longer show online/offline state"
    assert re.search(r'`dot \$\{h\.off \? "bad" : "ok"\}`', src)


def test_the_marker_is_only_added_where_it_means_something(src):
    """Hosts enrolled drills into the same component with rows that have no `off`
    flag; a dot there would be asserting a state nobody computed."""
    assert "h.off !== undefined &&" in src, \
        "a state dot is rendered for rows that carry no state"


# ---- and the counts still agree with each other ----------------------------
def test_online_and_offline_still_come_from_one_source(src):
    """They summed to Hosts enrolled because all three read the same inventory.
    Combining the cards must not quietly give the suffix a different source."""
    m = re.search(r"const hostLists = useMemo\(\(\) => \{(.+?)\n  \}, \[inventory\]\)", src, re.S)
    assert m, "hostLists changed shape"
    body = m.group(1)
    assert "const online = [], offline = []" in body
    assert "return { all: inv.map(mk), online, offline, combined };" in body
