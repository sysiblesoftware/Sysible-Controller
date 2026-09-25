"""Selecting the hosts that need a reboot.

The Update Hosts page could bulk-select "all with updates" and nothing else. That
selector keys on pending updates, so on a fleet that has just finished patching —
every host at 0 pending, ten of them flagged REBOOT yes — it selects NOTHING. The
one moment you most want to reboot a set of hosts is the one moment the page
offers no way to pick them, leaving sixteen checkboxes to tick by hand.

Driven in a real browser against a fleet shaped like that one: the button reads
"Select all needing reboot (10)", clicking it selects exactly the ten rows whose
REBOOT column says yes and none of the six that say no, and on a fleet with no
reboots pending it renders disabled with no count rather than as a control that
silently does nothing.

There is no JS test runner in this repo, so what these tests hold is the RULE —
that the selection keys on the reboot flag and not on pending updates, and that
it stays the same rule the header's "N need reboot" count uses, so the button and
the number beside it can never disagree.
"""
import re
from pathlib import Path

import pytest

VIEW = Path(__file__).resolve().parents[1] / "webgui" / "frontend" / "src" / "views" / "Updates.jsx"


@pytest.fixture(scope="module")
def src():
    return VIEW.read_text()


def _rebootable_expr(src):
    m = re.search(r"const rebootable = (.+?);\n", src, re.S)
    assert m, "the reboot selection is gone from Updates.jsx"
    return m.group(1)


def test_the_selection_keys_on_the_reboot_flag(src):
    expr = _rebootable_expr(src)
    assert "h.reboot" in expr, "the reboot selection no longer reads the reboot flag"


def test_the_selection_does_not_depend_on_pending_updates(src):
    """The entire reason this exists: after a patch run every host is at 0
    pending and still needs its reboot."""
    expr = _rebootable_expr(src)
    assert "total" not in expr, (
        "the reboot selection is gated on pending updates again — it will select "
        "nothing on a freshly-patched fleet, which is exactly when it is needed")


def test_offline_hosts_are_not_selected(src):
    """Nothing can be dispatched to a host that is not checking in; selecting it
    only produces a failure row."""
    expr = _rebootable_expr(src)
    assert "online !== false" in expr


def test_the_button_exists_and_is_wired_to_that_selection(src):
    m = re.search(r"Select all needing reboot", src)
    assert m, "the button is gone"
    # The button's own JSX block, from the preceding <button to its close.
    start = src.rfind("<button", 0, m.start())
    block = src[start:m.end() + 200]
    assert "setChecked(rebootable)" in block, \
        "the button no longer selects the reboot set"
    assert "disabled={!rebootable.length}" in block, \
        "with nothing to select the button must be disabled, not a control that does nothing"


def test_the_button_and_the_header_count_use_the_same_rule(src):
    """The header says 'N need reboot' right above it. If these two ever key on
    different things, the button selects a number that contradicts the label
    beside it — and the operator cannot tell which one is lying."""
    summary = re.search(r"const summary = useMemo\(\(\) => \{(.+?)\}, \[hosts\]\)", src, re.S)
    assert summary, "the header summary is gone"
    body = summary.group(1)
    assert "if (h.reboot) reboot++" in body, "the header's reboot count changed shape"
    assert "h.online === false" in body, "the header no longer excludes offline hosts"
    # Both sides: reboot flag + online, and no dependence on pending updates.
    expr = _rebootable_expr(src)
    assert "h.reboot" in expr and "online !== false" in expr
