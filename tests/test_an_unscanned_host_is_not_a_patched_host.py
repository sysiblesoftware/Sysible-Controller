"""A host nobody could scan must never be counted as a host with nothing to install.

Found by scanning for the class, then confirmed against two screenshots of the
same fleet:

    Update Hosts      "0 hosts with updates  0 security  1 need reboot  2 offline"
    Dashboard         "Needs patching 0 · 1 reboot"

Nine of those sixteen hosts read "timed out waiting for agent". A failed scan
returns total: null and security: null, and every reader asked `(h.total || 0)
> 0` — false, which is the same answer a fully patched host gives. There was no
third state, so there was no way to say "we do not know".

The counts were the smaller half. "All N hosts up to date" fires on
withUpd === 0 && sec === 0 && reboot === 0, and the Dashboard tile paints its
zero in OK green on withUpd === 0 alone. In the screenshot a single stray reboot
flag on one unrelated host was the only thing standing between the operator and
a green all-clear over a fleet two thirds of which was never scanned. On a patch
compliance screen the worst failure is not a missing number; it is a green one.
"""
import pathlib
import re
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SRC = REPO / "webgui" / "frontend" / "src"
UPDATES_JS = (SRC / "updates.js").read_text(encoding="utf-8")
UPDATES_VIEW = (SRC / "views" / "Updates.jsx").read_text(encoding="utf-8")
DASH = (SRC / "views" / "Dashboard.jsx").read_text(encoding="utf-8")
NODE = __import__("shutil").which("node")


@pytest.mark.skipif(not NODE, reason="node is not on this machine")
@pytest.mark.parametrize("host,want,why", [
    ({"online": True, "total": None, "security": None}, True,
     "a timed-out scan is the case this exists for"),
    ({"online": True, "total": 0, "security": 0}, False,
     "a host that answered zero is KNOWN to be clean"),
    ({"online": True, "total": 3, "security": 1}, False, "it answered"),
    ({"online": True, "total": None, "security": 2}, False,
     "a partial answer is still an answer — SUSE reports security without total"),
    ({"online": False, "total": None, "security": None}, False,
     "offline has its own count; folding it in here double-reports it"),
    (None, True, "no row at all is not a clean host"),
])
def test_what_counts_as_unknown(host, want, why, tmp_path):
    """Driven in node against the real module, not pattern-matched."""
    import json
    probe = tmp_path / "probe.mjs"
    probe.write_text(
        f"import {{ isUnknown }} from {json.dumps(str(SRC / 'updates.js'))};\n"
        f"console.log(JSON.stringify(isUnknown({json.dumps(host)})));\n")
    out = subprocess.run([NODE, str(probe)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip()) is want, why


def test_the_all_up_to_date_banner_requires_every_host_to_have_answered():
    block = UPDATES_VIEW[UPDATES_VIEW.index("✓ All {summary.total") - 900:]
    block = block[:block.index("✓ All {summary.total")]
    assert "summary.unknown === 0" in block, (
        "the all-clear banner can still appear over hosts that never reported")


def test_the_summary_counts_unknown_separately():
    block = UPDATES_VIEW[UPDATES_VIEW.index("const summary = useMemo"):]
    block = block[:block.index("const groups = useMemo")]
    assert "isUnknown(h)" in block, "the summary has no notion of an unscanned host"
    # It must SKIP, not also add to the other tallies.
    assert re.search(r"if \(isUnknown\(h\)\) \{ unknown\+\+; continue; \}", block), \
        "an unknown host still falls through into the withUpd/sec/reboot counts"
    assert "unknown" in UPDATES_VIEW[UPDATES_VIEW.index("return { withUpd"):][:120]


def test_the_dashboard_tile_does_not_paint_an_unscanned_fleet_green():
    block = DASH[DASH.index("function PatchCard"):]
    block = block[:block.index("return (")]
    assert re.search(r"const clear = state === \"ready\" && withUpd === 0 && unknown === 0",
                     block), "a fleet nobody could scan still shows a green zero"
    assert "unknown > 0" in block, "the tooltip still claims every host reported"


def test_the_dashboard_loop_skips_unknown_hosts():
    block = DASH[DASH.index("const patch = useMemo"):]
    block = block[:block.index("}, [updates, updState]);")]
    assert "isUnknown(h)" in block and "continue" in block, \
        "unscanned hosts still count toward the tile's zero"


def test_both_screens_ask_the_same_module():
    """The reason updates.js exists: this question was written out three times
    and all three got it wrong together. A second private copy of 'unknown'
    would put it straight back."""
    for name, text in (("Updates.jsx", UPDATES_VIEW), ("Dashboard.jsx", DASH)):
        assert 'from "../updates.js"' in text and "isUnknown" in text, name
        assert "total == null" not in text, \
            f"{name} re-implements the unknown test instead of importing it"
