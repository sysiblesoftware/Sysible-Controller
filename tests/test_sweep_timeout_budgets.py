"""The browser has to wait at least as long as the server is allowed to take.

Reported: "I'm getting a request timeout" while scanning the fleet.

The console aborts any JSON call after 60 seconds. The fleet sweeps budget their
probes PER HOST — 60s for a rescan, 180s for "Refresh metadata & rescan" — and
run them in waves. So the live refresh was DESIGNED to outlast the browser's own
limit: on any fleet with a slow host it could not succeed, and the operator got
"Request timed out — the controller didn't respond in time", which blames the
controller for a scan that was working and would have finished. Measured in a
real browser against a 75s sweep: aborted at exactly 60s; with the corrected
budget, returned at 75s with the data.

These two numbers live in different languages in different files, which is
exactly how they drifted apart. This test reads both and holds them together.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
API_JS = REPO / "webgui" / "frontend" / "src" / "api.js"
SERVER = REPO / "webgui" / "server.py"


def _js_const(name):
    m = re.search(rf"^const {name}\s*=\s*(\d+)\s*;", API_JS.read_text(), re.M)
    assert m, f"{name} is gone from api.js"
    return int(m.group(1))


def _server_live_probe_budget():
    """The per-host budget the updates sweep gives itself on a live refresh."""
    m = re.search(r'ptmo = int\(os\.getenv\("SYSIBLE_UPDATE_PROBE_TIMEOUT",\s*"(\d+)"\)\)'
                  r'\s*if live else\s*(\d+)', SERVER.read_text())
    assert m, "the updates sweep no longer states its per-host probe budget"
    return int(m.group(1)), int(m.group(2))     # (live, plain) seconds


def test_the_live_sweep_budget_outlasts_the_server():
    live_s, _ = _server_live_probe_budget()
    client_ms = _js_const("LIVE_SWEEP_TIMEOUT_MS")
    assert client_ms > live_s * 1000, (
        f"'Refresh metadata & rescan' lets the server take {live_s}s per host but the "
        f"browser gives up after {client_ms // 1000}s — a working scan is reported to the "
        f"operator as a controller timeout")


def test_the_plain_sweep_budget_outlasts_the_server():
    _, plain_s = _server_live_probe_budget()
    client_ms = _js_const("SWEEP_TIMEOUT_MS")
    assert client_ms > plain_s * 1000, (
        f"a rescan lets the server take {plain_s}s per host but the browser gives up after "
        f"{client_ms // 1000}s")


def test_a_sweep_is_never_left_on_the_ordinary_request_budget():
    """The ordinary limit is for ordinary calls. A sweep that forgets to say so
    silently inherits 60s and starts timing out again."""
    src = API_JS.read_text()
    for call in ("fleetUpdates", "fleetPosture", "fleetHealth"):
        # From this entry's name to the start of the next one — the call can span
        # several lines, and a non-greedy match to the first ")," lands mid-call.
        m = re.search(rf"\n  {call}:(.*?)(?=\n  [A-Za-z_][A-Za-z0-9_]*:)", src, re.S)
        assert m, f"{call} is gone from api.js"
        assert "timeout" in m.group(1), (
            f"{call} passes no timeout, so it inherits the 60s default while the server "
            f"is allowed to take minutes")


def test_the_default_stays_short_for_everything_else():
    """The fix must not become 'make every request wait ten minutes'. An ordinary
    call to a wedged controller still has to fail quickly enough to say so."""
    assert _js_const("DEFAULT_TIMEOUT_MS") <= 60000
