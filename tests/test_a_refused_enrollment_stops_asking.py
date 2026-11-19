"""An enrollment the controller refuses is not retried forever.

From a host that had restarted 164 times in fourteen minutes:

    [agent] sysible agent build 28a0e771103c starting
    Traceback (most recent call last):
      File "/opt/sysible-agent/agent.py", line 2577, in main
        state = register()
      ...
    requests.exceptions.HTTPError: 403 Invalid or expired token
    sysible-agent.service: Main process exited, code=exited, status=1/FAILURE
    sysible-agent.service: Scheduled restart job, restart counter is at 164.

An enrollment token is SINGLE USE. Once spent, no amount of asking again
revives it -- and register() handled no refusal at all, so the HTTPError
escaped main(), printed twenty lines of stack, exited 1, and systemd started
the whole thing over five seconds later.

The rule is the controller's own choice of status code rather than a list of
message strings that would drift from it: 429 (busy) and 503 (an administrator
paused enrollment) both mean come back later, and both will answer differently
with nobody touching this host. Every other refusal -- token spent, host
revoked, already enrolled, network not permitted -- is a decision about THIS
host that only an operator can change, so the agent says what to do and stops.

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


def _drive(tmp_path, status, detail):
    """Run the REAL register()/main() against a controller that refuses."""
    stub = tmp_path / "run.py"
    stub.write_text(textwrap.dedent(f"""
        import sys, types
        sys.path.insert(0, {str(AGENT.parent)!r})
        import requests
        import agent

        class R:
            status_code = {status}
            reason = "refused"
            ok = False
            def json(self): return {{"detail": {detail!r}}}

        agent.load_state = lambda: None
        agent.get_enroll_token = lambda: "a-token"
        agent._stable_host_id = lambda: "host-1"
        agent._request = lambda *a, **k: R()
        agent.loop = lambda state: None
        agent.main()
    """), encoding="utf-8")
    r = subprocess.run([sys.executable, str(stub)], capture_output=True, text=True,
                       timeout=60)
    return r


# ---- the one that cost 164 restarts ----------------------------------------
def test_a_spent_token_stops_the_agent(tmp_path):
    r = _drive(tmp_path, 403, "Invalid or expired token")
    assert r.returncode == 78, (
        f"exited {r.returncode}, so systemd restarts it every five seconds "
        f"forever\n{r.stdout}\n{r.stderr}")


def test_a_spent_token_says_what_to_do_about_it(tmp_path):
    r = _drive(tmp_path, 403, "Invalid or expired token")
    out = r.stdout + r.stderr
    assert "used ONCE" in out, out
    assert "Download" in out and "run_agent.sh" in out, out


@pytest.mark.parametrize("status,detail", [
    # The terminal path never re-raises, so it cannot produce one...
    (403, "Invalid or expired token"),
    # ...and THIS is the one that can: a retryable refusal re-raises so the
    # process exits non-78 and systemd's restart becomes the retry, which
    # means main() has to catch it. Testing only the 403 passed whether or
    # not main() handled anything at all.
    (503, "Enrollment is paused by an administrator."),
])
def test_it_does_not_print_a_traceback(tmp_path, status, detail):
    """Twenty lines of stack, every five seconds, is how the one line that
    mattered became the hardest thing in the journal to find."""
    r = _drive(tmp_path, status, detail)
    out = r.stdout + r.stderr
    assert "Traceback (most recent call last)" not in out, out
    assert "requests.exceptions.HTTPError" not in out, out


# ---- what the controller means by each refusal -----------------------------
@pytest.mark.parametrize("status,detail", [
    (403, "Invalid or expired token"),
    (403, "This host was revoked by an administrator."),
    (403, "This host_id is already enrolled."),
    (403, "Enrollment from this network is not permitted."),
    (409, "This enrollment token was just claimed by another host."),
    (409, "A live agent is already enrolled for this host."),
    (400, "Invalid host_id."),
])
def test_a_decision_about_this_host_is_terminal(tmp_path, status, detail):
    r = _drive(tmp_path, status, detail)
    assert r.returncode == 78, f"{status} {detail!r} exited {r.returncode}"


@pytest.mark.parametrize("status,detail", [
    (503, "Enrollment is paused by an administrator."),
    (429, "Too many enrollment attempts from this source; retry shortly."),
])
def test_come_back_later_really_does_come_back(tmp_path, status, detail):
    """These two will answer differently with nobody touching this host, so
    stopping the unit for good would need a visit to un-pause a pause."""
    r = _drive(tmp_path, status, detail)
    assert r.returncode == 1, f"{status} {detail!r} exited {r.returncode} (78 = stopped)"
    assert "Trying again shortly" in (r.stdout + r.stderr)


def test_the_retryable_set_is_the_controllers_own_status_codes():
    """Matching on message text would drift from the controller the first time
    anyone reworded a sentence."""
    m = re.search(r"RETRYABLE_ENROLL_STATUS = \(([^)]*)\)", SRC)
    assert m, "the rule is no longer one list"
    assert {int(x) for x in re.findall(r"\d+", m.group(1))} == {429, 503}


def test_the_refusal_is_reported_before_the_decision_to_stop():
    i = SRC.index("def _enrollment_refused")
    body = SRC[i:i + 2500]
    assert body.index("refused this enrollment") < body.index("sys.exit(EXIT_NEEDS_OPERATOR)")
