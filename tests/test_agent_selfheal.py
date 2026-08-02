"""
Agent self-heal: a self-update rewrites host_agent/agent.py on disk and asks
systemd to restart the agent. When that external restart doesn't take, the
running process keeps reporting the OLD build forever and the console shows the
host stuck "updating". The agent must detect that its on-disk source changed and
re-exec into it on its own — no dependence on the restart.

These load agent.py by path (no enrollment/network) and stub os.execv so the
re-exec is observed, not performed.
"""
import importlib.util
import os
import shutil
import tempfile

import pytest

_AGENT_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "host_agent", "agent.py")


def _load_agent_copy():
    d = tempfile.mkdtemp()
    copy = os.path.join(d, "agent.py")
    shutil.copy(_AGENT_SRC, copy)
    spec = importlib.util.spec_from_file_location("sysagent_under_test", copy)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m, copy


@pytest.fixture()
def agent():
    m, copy = _load_agent_copy()
    calls = []
    m.os.execv = lambda *a: calls.append(a)   # observe, don't actually exec
    m._last_reexec_check = 0.0
    return m, copy, calls


def test_no_reexec_when_source_unchanged(agent):
    m, _copy, calls = agent
    m._reexec_if_source_changed()
    assert not calls, "re-exec fired with no on-disk change"


def test_reexec_when_source_changed_and_compiles(agent):
    m, copy, calls = agent
    old = m.AGENT_VERSION
    with open(copy, "a", encoding="utf-8") as f:
        f.write("\n# self-update bumped this build\n")
    assert m._source_version(copy) != old
    m._last_reexec_check = 0.0
    m._reexec_if_source_changed()
    assert calls, "agent did not re-exec after its source changed on disk"
    # re-execs into the same file path with the interpreter
    argv = calls[0][1]
    assert argv[1] == copy


def test_no_reexec_when_new_source_is_corrupt(agent):
    m, copy, calls = agent
    with open(copy, "a", encoding="utf-8") as f:
        f.write("\ndef (:  # not valid python\n")
    m._last_reexec_check = 0.0
    m._reexec_if_source_changed()
    assert not calls, "re-exec fired on a non-compiling file (would crash-loop)"


def test_reexec_check_is_throttled(agent):
    m, copy, calls = agent
    with open(copy, "a", encoding="utf-8") as f:
        f.write("\n# changed\n")
    # First call primes the throttle timestamp but the throttle window blocks a
    # second immediate call — so a busy loop can't hammer the filesystem.
    m._last_reexec_check = m.time.time()   # pretend we just checked
    m._reexec_if_source_changed()
    assert not calls, "throttle did not suppress a rapid re-check"
