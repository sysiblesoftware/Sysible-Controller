"""An agent nobody can fix by restarting must stop, not restart forever.

Seen on a host that had just been disenrolled:

    sysible-agent.service - Sysible host agent
     Active: activating (auto-restart) (Result: exit-code) ... 591ms ago
    Process: ExecStart=/usr/bin/python3 /opt/sysible-agent/agent.py
             (code=exited, status=1/FAILURE)
        CPU: 154ms

The agent was behaving correctly. It asked the controller for work, was told
the host_id is unknown, printed the paragraph that says exactly what to do --
re-enroll with a FRESH bundle, the old token is spent -- cleared its state and
exited. The unit's Restart=always then launched it again, where it found no
token, printed the other paragraph, and exited. Five seconds later, again.

Exit 1 is what a crash looks like, and crashes SHOULD be retried: a heartbeat
timing out under load is exactly what Restart=always is for. These two are not
crashes. The state is already cleared and the token already spent, so no
number of restarts can change the outcome -- only an operator with a new
bundle can. They exit 78 (EX_CONFIG from sysexits(3)) now, and the unit names
78 in RestartPreventExitStatus, so systemd stops and leaves the unit failed
with the reason on screen instead of a stopwatch.

Run: pytest tests/
"""
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
AGENT = REPO / "host_agent" / "agent.py"
SRC = AGENT.read_text(encoding="utf-8")


def _unit() -> str:
    """The unit as it is really written onto a host -- generated, not a copy
    of it kept here, so this cannot pass against a unit nobody installs."""
    sys.path.insert(0, str(REPO))
    from backend import agent_bundle
    unit = agent_bundle._systemd_unit()
    assert "[Service]" in unit and "ExecStart" in unit, unit[:200]
    return unit


UNIT = _unit()


# ---- the two deliberate exits --------------------------------------------
def test_the_agent_has_one_code_for_needing_an_operator():
    assert "EXIT_NEEDS_OPERATOR = 78" in SRC, \
        "no distinct exit code, so systemd cannot tell this from a crash"


@pytest.mark.parametrize("why", [
    "no enrollment token found",
    "controller no longer recognizes host_id",
])
def test_each_unfixable_state_exits_with_that_code(why):
    """Both of them, by the text the operator actually sees -- the point is
    that the message and the code travel together."""
    i = SRC.index(why)
    after = SRC[i:i + 1500]
    m = re.search(r"sys\.exit\(([^)]*)\)", after)
    assert m, f"no exit after {why!r}"
    assert m.group(1).strip() == "EXIT_NEEDS_OPERATOR", (
        f"{why!r} still exits {m.group(1)}, which systemd will retry forever")


def test_an_ordinary_failure_is_still_retried():
    """The guard must not turn every failure terminal. A heartbeat that times
    out under load is exactly what Restart=always is for, and a host that goes
    quiet because its agent gave up on one bad request is worse than the loop
    this fixes."""
    assert "sys.exit(EXIT_NEEDS_OPERATOR)" in SRC
    others = [m for m in re.findall(r"sys\.exit\(([^)]*)\)", SRC)
              if m.strip() not in ("EXIT_NEEDS_OPERATOR", "0")]
    assert not others, (
        f"other hard exits that systemd WILL retry: {others} -- if one of them "
        f"is also unfixable it needs the same code, and if it is not it should "
        f"not be a hard exit")


def test_the_agent_says_it_is_stopping_rather_than_restarting():
    """"no enrollment token found" with the unit relaunching every five
    seconds reads as a bug in the agent. It is a decision."""
    i = SRC.index("no enrollment token found")
    assert "stopping rather than restarting" in SRC[i:i + 400], SRC[i:i + 400]


# ---- the unit has to agree ------------------------------------------------
def test_the_unit_stops_on_that_code():
    m = re.search(r"^RestartPreventExitStatus=(.*)$", UNIT, re.M)
    assert m, "the unit restarts the agent whatever it exits with"
    assert "78" in m.group(1).split(), m.group(1)


def test_the_unit_still_restarts_everything_else():
    assert re.search(r"^Restart=always$", UNIT, re.M), \
        "the agent no longer comes back from an ordinary crash"


def test_the_two_halves_name_the_same_number():
    """A code in the agent and a different one in the unit is the same loop
    with more words in it."""
    code = re.search(r"EXIT_NEEDS_OPERATOR = (\d+)", SRC).group(1)
    prevent = re.search(r"^RestartPreventExitStatus=(.*)$", UNIT, re.M).group(1)
    assert code in prevent.split(), f"agent exits {code}, unit prevents {prevent}"


# ---- and it really exits with it ------------------------------------------
def test_running_the_agent_with_no_token_exits_78(tmp_path):
    """Not asserted from the source: run it.

    Stubbed down to the one decision -- no token, no state -- so this is the
    real register() path and the real exit, with nothing on the network."""
    stub = tmp_path / "run.py"
    stub.write_text(textwrap.dedent(f"""
        import sys, types
        try:                      # the real one where it is installed
            import requests       # noqa: F401
        except ImportError:       # ...and enough of one where it is not
            m = types.ModuleType("requests")
            m.Session = lambda *a, **k: None
            m.exceptions = types.SimpleNamespace(RequestException=Exception)
            sys.modules["requests"] = m
        sys.path.insert(0, {str(AGENT.parent)!r})
        import agent
        agent.load_state = lambda: None
        agent.get_enroll_token = lambda: None
        try:
            agent.main()
        except SystemExit as e:
            raise SystemExit(e.code)
    """), encoding="utf-8")
    r = subprocess.run([sys.executable, str(stub)], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 78, (
        f"exited {r.returncode}, so systemd restarts it forever\n"
        f"{r.stdout}\n{r.stderr}")
    assert "no enrollment token" in r.stdout, r.stdout
