"""Whether agents are reporting in is on screen everywhere, from one source.

Asked for as "some kind of controller health bar at the top of controller to
monitor agent health". Until now the only place that said so was the Dashboard,
so the answer was a navigation away from anyone in the middle of doing
something to the fleet.

The more interesting half is where the number comes from. /api/agents ALREADY
attaches an aged `online` flag, computed from _AGENT_ONLINE_SECONDS -- the
window an operator can tune. The Dashboard ignored it and re-derived liveness
twice from its own `const STALE = 20`. Adding a third copy for the new strip
would have been the straightforward thing to do and exactly the bug this
session keeps finding: two surfaces, one fleet, answers that can drift apart.
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
SRC = REPO / "webgui" / "frontend" / "src"
FLEET_JS = SRC / "fleet.js"
PULSE = (SRC / "components" / "FleetPulse.jsx").read_text(encoding="utf-8")
DASH = (SRC / "views" / "Dashboard.jsx").read_text(encoding="utf-8")
APP = (SRC / "App.jsx").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _ask(expr, inventory):
    """Run the real module rather than pattern-match it."""
    probe = REPO / "tests" / ".fleet_probe.mjs"
    probe.write_text(
        f'import {{ fleetCounts, isOnline, agentHealth, agentHealthCounts }} from {json.dumps(str(FLEET_JS))};\n'
        f'const inv = {json.dumps(inventory)};\n'
        f'console.log(JSON.stringify({expr}));\n')
    try:
        out = subprocess.run([NODE, str(probe)], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout.strip())
    finally:
        probe.unlink(missing_ok=True)


@pytest.mark.skipif(not NODE, reason="node is not on this machine")
def test_it_believes_the_server_over_its_own_clock():
    """The server said offline. A stale-but-recent last_seen must not override
    it: the window belongs to the controller, which an operator can retune."""
    inv = [{"host_id": "a", "online": False, "last_seen": 9e9}]
    assert _ask("fleetCounts(inv, 9e9)", inv) == {"total": 1, "online": 0, "offline": 1}


@pytest.mark.skipif(not NODE, reason="node is not on this machine")
def test_it_falls_back_only_when_the_server_did_not_say():
    """A controller old enough not to send the flag still gets counted."""
    inv = [{"host_id": "a", "last_seen": 1000}, {"host_id": "b", "last_seen": 900}]
    assert _ask("fleetCounts(inv, 1005)", inv) == {"total": 2, "online": 1, "offline": 1}


@pytest.mark.skipif(not NODE, reason="node is not on this machine")
def test_a_revoked_host_is_not_part_of_the_fleet():
    inv = [{"host_id": "a", "online": True}, {"host_id": "b", "online": True, "revoked": True}]
    assert _ask("fleetCounts(inv)", inv) == {"total": 1, "online": 1, "offline": 0}


@pytest.mark.skipif(not NODE, reason="node is not on this machine")
def test_nothing_enrolled_is_not_a_fleet_that_is_down():
    assert _ask("fleetCounts(inv)", []) == {"total": 0, "online": 0, "offline": 0}


def test_the_fallback_window_matches_the_server_default():
    """Two numbers in two languages in two files -- the shape that drifts. The
    fallback only fires for an old controller, but when it fires it must agree."""
    js = re.search(r"export const STALE_SECONDS\s*=\s*(\d+)", FLEET_JS.read_text(encoding="utf-8"))
    py = re.search(r'_AGENT_ONLINE_SECONDS\s*=\s*float\(os\.getenv\(\s*"SYSIBLE_AGENT_ONLINE_SECONDS",\s*"(\d+)"\s*\)\)',
                   (REPO / "webgui" / "server.py").read_text(encoding="utf-8"))
    assert js and py, "one of the two windows moved or was renamed"
    assert int(js.group(1)) == int(py.group(1)), \
        f"the console falls back to {js.group(1)}s, the controller uses {py.group(1)}s"


def test_the_dashboard_no_longer_keeps_its_own_copy():
    assert 'from "../fleet.js"' in DASH, "the Dashboard does not use the shared source"
    assert "const STALE" not in DASH, "the Dashboard still carries its own window"
    assert "last_seen) <=" not in DASH, "the Dashboard still ages last_seen itself"


def test_the_strip_is_on_every_view_and_for_every_role():
    """Read-only fleet state. Gating it behind a view or a role puts it back
    where it was."""
    assert "<FleetPulse" in APP, "the strip is not mounted"
    # The WHOLE LINE, not from the tag onward: a `{view === null && ...}` guard
    # sits BEFORE the tag, so slicing from it reads past the very thing this
    # checks. Found by mutation -- the first version of this passed while the
    # strip was gated to the Dashboard.
    line = next(l for l in APP.splitlines() if "<FleetPulse" in l)
    assert "view" not in line, f"the strip only appears on some views: {line.strip()}"
    assert "isSuper" not in line and "isAuditor" not in line, \
        f"the strip is gated by role: {line.strip()}"


def test_a_failed_poll_says_so_rather_than_showing_zero():
    """"0 online" and "I could not ask" are different facts, and a strip that
    shows the first for the second is the thing being fixed all over this
    product."""
    assert '"err"' in PULSE and "unavailable" in PULSE
    block = PULSE[PULSE.index('if (state.kind === "err")'):]
    block = block[:block.index("}")]
    assert "counts" not in block, "the error state still renders counts"


def test_every_agent_silent_is_its_own_message():
    """Sixteen hosts do not go down together; the controller's address moved."""
    assert "silent === total" in PULSE, "the all-silent case is no longer singled out"
    assert "address" in PULSE, "it does not suggest what that usually means"


def test_it_uses_the_instant_inventory_not_a_sweep():
    """A strip on every screen must not start a fleet-wide probe every few
    seconds. /api/agents is heartbeat ages the controller already holds."""
    assert "api.agents()" in PULSE
    for heavy in ("fleetHealth", "fleetPosture", "fleetUpdates", "runTool"):
        assert heavy not in PULSE, f"the strip polls {heavy}"

# ---- and it says what it is -------------------------------------------------
def test_the_number_has_a_noun():
    """It was a dot, a number and "/ 16 agents" in 12px beside the sign-in
    line. Reported as not looking like anything and as not saying what the
    number was -- "16 of what, measured how" is not a status display."""
    assert "fleet-chip-label" in PULSE, "the count carries no label"
    assert ">Agents<" in PULSE, "the label does not name what is being counted"
    assert "of ${total} healthy" in PULSE, \
        "the value does not read as a sentence (e.g. '14 of 16 healthy')"


def test_it_is_a_control_not_a_run_of_text():
    """A chip, so it separates from the 'Signed in as ...' text it sits beside
    -- which was two runs of small grey with nothing to tell them apart."""
    assert '"fleet-chip ' in PULSE, "there is no container"
    assert "<button" in PULSE, "a clickable status is not a button"
    css = (REPO / "webgui" / "frontend" / "src" / "styles.css").read_text(encoding="utf-8")
    assert ".fleet-chip {" in css, "the chip has no styling"
    assert "border-radius: var(--r-pill)" in css[css.index(".fleet-chip {"):][:400]


def test_the_state_is_not_carried_by_colour_alone():
    """A red dot and a green dot are the same dot to a good share of
    operators. The VALUE says it too: '0 of 16 online' reads without colour."""
    assert "status unavailable" in PULSE and "none enrolled" in PULSE
    assert "aria-label" in PULSE, "the state is not exposed to a screen reader"


def test_an_unreadable_inventory_is_not_zero_online():
    """The distinction this whole session keeps turning on."""
    err = PULSE[PULSE.index('if (state.kind === "err")'):]
    err = err[:err.index("const { total")]
    # Comments are not code. The comment in that block says Not "0 online",
    # which is exactly the string this looks for -- a guard tripped by its own
    # explanation, for the third time in this session.
    code = "\n".join(l for l in err.splitlines() if not l.strip().startswith("//"))
    assert "status unavailable" in code
    assert "online" not in code, "the error state claims something about the hosts"

# ---- health is wider than reachability --------------------------------------
@pytest.mark.skipif(not NODE, reason="node is not on this machine")
def test_a_quarantined_agent_is_not_healthy():
    """THE CASE THE WIDENING IS FOR. The controller hands a quarantined agent no
    tasks at all -- _probe_updates and _probe_posture both fail it fast rather
    than wait out the deadline -- so it heartbeats on schedule, counted as
    online, and runs nothing anyone asks of it."""
    inv = [{"host_id": "a", "online": True},
           {"host_id": "b", "online": True, "integrity_quarantined": True}]
    assert _ask("agentHealthCounts(inv, 0)", inv) == {
        "total": 2, "healthy": 1, "silent": 0, "quarantined": 1}


@pytest.mark.skipif(not NODE, reason="node is not on this machine")
def test_a_silent_agent_that_is_also_quarantined_is_counted_once():
    """Counting it twice would put the breakdown above the fleet size."""
    inv = [{"host_id": "a", "online": False, "integrity_quarantined": True}]
    got = _ask("agentHealthCounts(inv, 0)", inv)
    assert got == {"total": 1, "healthy": 0, "silent": 1, "quarantined": 0}, got
    assert got["silent"] + got["quarantined"] + got["healthy"] == got["total"]


@pytest.mark.skipif(not NODE, reason="node is not on this machine")
def test_a_revoked_agent_is_in_no_count_at_all():
    inv = [{"host_id": "a", "online": True},
           {"host_id": "b", "online": True, "revoked": True}]
    assert _ask("agentHealthCounts(inv, 0)", inv)["total"] == 1


def test_the_chip_says_why_rather_than_only_how_many():
    """A number that has gone amber is not actionable on its own. The chip stays
    short; the tooltip has room to say what is wrong and why it matters."""
    assert "quarantined" in PULSE and "silent" in PULSE
    assert "hands them no work" in PULSE, \
        "it does not say what being quarantined costs the operator"


def test_version_is_not_guessed():
    """The console only learns the expected agent version from the response to
    an update-agents POST, so "out of date" cannot be answered from the
    inventory. Inventing it would be worse than leaving it out."""
    fleet = (SRC / "fleet.js").read_text(encoding="utf-8")
    code = "\n".join(l for l in fleet.splitlines() if not l.strip().startswith("//"))
    assert "agent_version" not in code, \
        "health is being derived from a version the payload cannot settle"
