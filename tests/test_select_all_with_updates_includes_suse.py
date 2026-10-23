""""Select all with updates" skipped every SUSE host that had a security patch.

Reported from a live fleet: three openSUSE hosts each showing a red 1 in the
SECURITY column, the header reading "6 hosts with updates", and the button
selecting six — none of them the three with outstanding security work.

The cause is that PENDING and SECURITY come from two DIFFERENT zypper commands,
and only one of them feeds `total`:

    PENDING   <- zypper list-updates                     -> `zypper update`
    SECURITY  <- zypper list-patches --category security  -> `zypper patch`

A Leap host with an unapplied security patch and no package updates reports
total=0, security=1 — which is accurate, and which the selection read as
"nothing to do here". On dnf and apt the bug is invisible: there a security
update is also a package update, so security>0 implies total>0 and keying on
total alone is accidentally right. It is wrong only on the family where the
SECURITY column is the one that matters.

These RUN the shipping rule under node rather than pattern-matching the source,
because what went wrong was behaviour on one shape of data, and a regex over
`const actionable = ...` would have been perfectly happy with the old line.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "webgui" / "frontend" / "src"
MODULE = SRC / "updates.js"
node = shutil.which("node") or "/opt/node22/bin/node"
needs_node = pytest.mark.skipif(not Path(node).exists(), reason="node is not available")

# The fleet from the report, exactly as the page showed it.
FLEET = [
    {"id": "arch-01", "total": 0, "security": 0},
    {"id": "arch-02", "total": 0, "security": 0},
    {"id": "arch-03", "total": 0, "security": 0},
    {"id": "deb-web-1", "total": 0, "security": 0},
    {"id": "opensuse-01", "total": 0, "security": 1},
    {"id": "opensuse-02", "total": 0, "security": 1},
    {"id": "opensuse-03", "total": 0, "security": 1},
    {"id": "rocky-01", "total": 3, "security": 0},
    {"id": "rocky-02", "total": 3, "security": 0},
    {"id": "rocky-03", "total": 1, "security": 0},
    {"id": "rocky-web-1", "total": 8, "security": 0},
    {"id": "sysible-virt", "total": 3, "security": 3},
    {"id": "ubuntu-01", "total": 0, "security": 0},
    {"id": "ubuntu-02", "total": 3, "security": 1},
    {"id": "ubuntu-03", "total": 0, "security": 0},
    {"id": "ubuntu-web-1", "total": 0, "security": 0},
]


def _run(fleet):
    """The real module, loaded by node, against this fleet."""
    script = (
        f'import {{ hasUpdates, hostsWithUpdates, countWithUpdates }} '
        f'from "{MODULE.as_posix()}";\n'
        f"const fleet = {json.dumps(fleet)};\n"
        'console.log(JSON.stringify({'
        '  selected: hostsWithUpdates(fleet).map(h => h.id),'
        '  count: countWithUpdates(fleet),'
        '  each: fleet.map(h => [h.id, hasUpdates(h)]),'
        '}));\n'
    )
    r = subprocess.run([node, "--input-type=module", "-e", script],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@needs_node
def test_the_suse_hosts_are_selected():
    """The report, in one assertion."""
    out = _run(FLEET)
    for host in ("opensuse-01", "opensuse-02", "opensuse-03"):
        assert host in out["selected"], (
            f"{host} has a pending SECURITY update and was not selected — the "
            f"three hosts with security work are the three the button skips")


@needs_node
def test_the_count_beside_the_button_agrees_with_it():
    """The header said 6 while the fleet had 9. A count and a selection that
    disagree teach an operator to distrust both."""
    out = _run(FLEET)
    assert out["count"] == len(out["selected"]) == 9


@needs_node
def test_hosts_with_nothing_to_do_are_still_not_selected():
    """The fix must not become "select everything". Eight hosts here are clean
    and must stay unselected, or the button is just Select All."""
    out = _run(FLEET)
    for host in ("arch-01", "arch-02", "arch-03", "deb-web-1",
                 "ubuntu-01", "ubuntu-03", "ubuntu-web-1"):
        assert host not in out["selected"], f"{host} has nothing pending"


@needs_node
@pytest.mark.parametrize("host,expected,why", [
    ({"total": 0, "security": 0}, False, "nothing pending"),
    ({"total": 3, "security": 0}, True, "package updates only (dnf/apt/pacman)"),
    ({"total": 0, "security": 1}, True, "a security patch only (zypper)"),
    ({"total": 3, "security": 1}, True, "both"),
    ({}, False, "a host that has never been scanned reports neither"),
    ({"total": None, "security": None}, False, "nulls are not updates"),
])
def test_the_rule_itself(host, expected, why):
    out = _run([dict(host, id="h")])
    assert out["each"][0][1] is expected, why


@needs_node
def test_an_offline_host_is_never_selected():
    """Nothing can be dispatched to a host that is not checking in; selecting it
    only produces a failure row. Same rule the reboot selection uses."""
    out = _run([{"id": "down", "total": 9, "security": 9, "online": False},
                {"id": "up", "total": 1, "security": 0, "online": True}])
    assert out["selected"] == ["up"]


def test_both_views_use_the_one_rule():
    """The answer was written out three times before this and all three were
    wrong together. Keeping them on one import is the only reason a fix in one
    place fixes the Dashboard tile too."""
    for view in ("Updates.jsx", "Dashboard.jsx"):
        src = (SRC / "views" / view).read_text()
        assert 'from "../updates.js"' in src, f"{view} does not use the shared rule"
        assert "(h.total || 0) > 0" not in src, (
            f"{view} still decides 'has updates' from the pending count alone — "
            f"that is the line that hid the SUSE hosts")
