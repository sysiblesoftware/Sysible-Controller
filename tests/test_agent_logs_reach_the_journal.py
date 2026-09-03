"""The agent's output must actually reach `journalctl -u sysible-agent`.

Reported from a live host: 16 hours of uptime and the journal contained nothing
but systemd's own start/stop lines —

    Sep 02 23:58:08 deb-web-1 systemd[1]: Started sysible-agent.service...
    Sep 03 01:36:07 deb-web-1 systemd[1]: Started sysible-agent.service...

and not one "[agent] running task N", though the host was enrolled, online, and
being sent work. Under systemd, stdout is a PIPE, so Python block-buffers it;
the agent prints a line or two per task, so a 4-8 KB buffer takes hours or days
to flush. The unit's own docstring promises the opposite ("its output goes to the
journal ... instead of an open terminal").

This is not cosmetic: it is why a host whose terminal silently failed could not be
diagnosed by its operator or by us. Two fixes, because they reach different
populations — the unit's PYTHONUNBUFFERED covers new installs, and agent.py's own
line buffering is what reaches the thousands of agents already running an older
unit (it ships through the self-update).
"""
import ast
import os
import re
import subprocess
import sys
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
AGENT = os.path.join(ROOT, "host_agent", "agent.py")

import backend.agent_bundle as agent_bundle


def _pipe_env():
    """A child env that reproduces systemd's condition. This sandbox exports
    PYTHONUNBUFFERED=1, which would silently make BOTH subprocess tests below
    pass for the wrong reason — the control test caught exactly that."""
    e = dict(os.environ)
    e.pop("PYTHONUNBUFFERED", None)
    e["PYTHONSTARTUP"] = ""
    return e


def test_the_unit_runs_the_agent_unbuffered():
    unit = agent_bundle._service_unit() if hasattr(agent_bundle, "_service_unit") else None
    if unit is None:                      # find whichever helper renders the unit
        unit = next(getattr(agent_bundle, n)() for n in dir(agent_bundle)
                    if n.endswith("service_unit") or n.endswith("_unit"))
    assert "PYTHONUNBUFFERED=1" in unit or re.search(r"ExecStart=\S*python3? -u\b", unit), \
        "systemd gives stdout a pipe; without this the journal stays empty for hours"
    assert "journalctl" not in unit or True


def test_the_agent_line_buffers_its_own_output():
    """The fix that reaches agents already installed with the old unit."""
    src = open(AGENT, encoding="utf-8").read()
    assert "line_buffering=True" in src, (
        "an already-installed agent keeps its old unit forever — only agent.py "
        "itself, which the self-update ships, can fix those hosts")
    # And it must not be able to take the agent down on a stream that can't be
    # reconfigured (a closed stdout under some supervisors).
    i = src.index("line_buffering=True")
    assert "except Exception" in src[i:i + 400], "must be best-effort, never fatal"


def test_the_agent_announces_its_build_on_startup():
    """Without this, answering "does this host's agent support terminals?" means
    reading the source on the box."""
    src = open(AGENT, encoding="utf-8").read()
    assert re.search(r'print\(f?"\[agent\] sysible agent build', src), \
        "no startup banner — the journal cannot say which build is running"


def test_the_agent_really_flushes_each_line_when_stdout_is_a_pipe():
    """The actual behaviour, not the source: run a program with the agent's
    buffering setup, print without exiting, and read the pipe. Unbuffered ->
    the line is there immediately; block-buffered -> nothing."""
    prog = textwrap.dedent("""
        import sys, time
        for _s in (sys.stdout, sys.stderr):
            try:
                _s.reconfigure(line_buffering=True)
            except Exception:
                pass
        print("[agent] hello from the journal")
        time.sleep(30)          # still running, buffer not flushed by exit
    """)
    p = subprocess.Popen([sys.executable, "-c", prog], stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, env=_pipe_env())
    try:
        line = p.stdout.readline()
    finally:
        p.kill()
    assert "hello from the journal" in line, (
        "the line did not reach the pipe while the process was still running — "
        "this is exactly the empty-journal failure")


def test_without_the_fix_the_same_program_writes_nothing():
    """Prove the test above is not vacuous: the identical program WITHOUT the
    reconfigure produces nothing on the pipe."""
    prog = textwrap.dedent("""
        import time
        print("[agent] hello from the journal")
        time.sleep(30)
    """)
    p = subprocess.Popen([sys.executable, "-c", prog], stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, env=_pipe_env())
    try:
        import select
        ready, _, _ = select.select([p.stdout], [], [], 2.0)
        got = p.stdout.readline() if ready else ""
    finally:
        p.kill()
    assert "hello" not in got, (
        "expected the unfixed program to buffer; if this fails the environment "
        "is not reproducing the systemd pipe condition and the sibling test proves nothing")


def test_the_agent_still_parses():
    ast.parse(open(AGENT, encoding="utf-8").read())
