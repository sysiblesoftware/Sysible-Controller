""""Shell script — runs as root on each checked host." It did not.

Reported with a transcript: an operator pasted a firewall audit into the
console's Run-a-script box, supplied a sudo password, ran it on 16 hosts, and
every host came back `whoami: admin` with firewall-cmd refusing on polkit, nft
"Operation not permitted" and iptables "you must be root". Exit 0 on every host.
The output looked like a finding about the fleet. It was a finding about the
dispatch.

The cause is the escalation rule in the agent:

    if first["returncode"] == 0 or not _looks_like_privilege_error(combined):
        return first

which is a good rule for a single command the controller composed — `systemctl
status foo` succeeding means there was nothing to elevate for — and the wrong
rule for a script somebody else wrote. An operator's script captures its own
failures into variables, prints them, and exits 0; it satisfies neither half of
that test while every privileged line inside it was refused.

The fix is not to loosen the heuristic. Re-running an arbitrary script because
its output mentioned "permission denied" would execute it TWICE, and scripts
are not idempotent. The fix is that when root is PROMISED, we go to root first —
still via the run-as user's own sudo, so the host's policy decides exactly as
before, and with no second execution.

The fallback matters as much as the escalation. A read-only script from an
operator with no sudo used to work, and still does; it just says so, loudly, in
the output. The one outcome that must never happen is unprivileged results
presented as though they were root's.
"""
import importlib
import os
import re
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


@pytest.fixture()
def agent(monkeypatch):
    import host_agent.agent as a
    a = importlib.reload(a)
    monkeypatch.setattr(a, "_local_user_exists", lambda u: True)
    monkeypatch.setattr(a.os, "geteuid", lambda: 0)      # agent runs as root
    return a


def _record(agent, monkeypatch, outcomes):
    """Replace _exec with a recorder. `outcomes` maps a substring of the argv to
    the result that invocation should produce."""
    calls = []

    def fake_exec(cmd, input_data=None, **kw):
        argv = cmd if isinstance(cmd, str) else " ".join(cmd)
        calls.append({"argv": argv, "stdin": input_data})
        for needle, res in outcomes.items():
            if needle in argv:
                return dict(res)
        return {"stdout": "", "stderr": "", "returncode": 0}

    monkeypatch.setattr(agent, "_exec", fake_exec)
    return calls


ELEVATED = "sudo"            # the elevated form always goes through sudo
PLAIN = "runuser -u admin -- bash -c"


# ---- the report -----------------------------------------------------------

def test_a_root_script_goes_to_root_first(agent, monkeypatch):
    """Not 'tries unprivileged, escalates if it looks like it failed'."""
    calls = _record(agent, monkeypatch, {ELEVATED: {"stdout": "uid=0(root)", "stderr": "", "returncode": 0}})
    res = agent._run_as_user("admin", "id -u", want_root=True)
    assert res["returncode"] == 0 and "root" in res["stdout"]
    assert len(calls) == 1, "the script was run more than once"
    assert "sudo" in calls[0]["argv"], "the first attempt was not elevated"


def test_the_exit_zero_script_that_started_this(agent, monkeypatch):
    """The exact shape from the report: unprivileged run succeeds (exit 0) with
    permission errors in its OUTPUT. The old rule returned that untouched."""
    denied = {"stdout": "whoami: admin\nNot Authorized(uid)\nyou must be root",
              "stderr": "", "returncode": 0}
    calls = _record(agent, monkeypatch, {PLAIN: denied,
                                         ELEVATED: {"stdout": "whoami: root", "stderr": "", "returncode": 0}})
    res = agent._run_as_user("admin", "whoami", want_root=True)
    assert "root" in res["stdout"] and "admin" not in res["stdout"], \
        "the script still ran as the operator despite root being promised"
    assert not any(PLAIN in c["argv"] and "sudo" not in c["argv"] for c in calls), \
        "the unprivileged form was run at all"


def test_a_script_is_never_executed_twice(agent, monkeypatch):
    """Scripts are not idempotent. Whatever else this does, it must not run an
    operator's script a second time to see if it works better as root."""
    calls = _record(agent, monkeypatch, {ELEVATED: {"stdout": "", "stderr": "boom", "returncode": 3}})
    agent._run_as_user("admin", "rm -rf /srv/thing", want_root=True)
    assert len(calls) == 1, f"ran {len(calls)} times: {[c['argv'] for c in calls]}"


def test_a_script_that_fails_on_its_own_merits_reports_its_own_failure(agent, monkeypatch):
    """A non-zero exit that is NOT a sudo refusal is the script's own result and
    must come back untouched — not be re-run with fewer privileges."""
    calls = _record(agent, monkeypatch,
                    {ELEVATED: {"stdout": "", "stderr": "cp: no such file", "returncode": 1}})
    res = agent._run_as_user("admin", "cp a b", want_root=True)
    assert res["returncode"] == 1 and "no such file" in res["stderr"]
    assert len(calls) == 1
    assert "NOT RUN AS ROOT" not in res["stderr"]


# ---- the fallback ---------------------------------------------------------

@pytest.mark.parametrize("refusal", [
    "sudo: a password is required",
    "sudo: no tty present and no askpass program specified",
    "admin is not in the sudoers file.  This incident will be reported.",
    "Sorry, try again.",
    "sudo: a terminal is required to read the password",
])
def test_an_operator_without_sudo_still_gets_their_script_run(agent, monkeypatch, refusal):
    """This used to work and must keep working: a read-only script from someone
    with no sudo rights. Refusing outright would be a regression for every
    auditor-shaped user."""
    calls = _record(agent, monkeypatch, {
        ELEVATED: {"stdout": "", "stderr": refusal, "returncode": 1},
        PLAIN: {"stdout": "some read-only output", "stderr": "", "returncode": 0},
    })
    res = agent._run_as_user("admin", "cat /etc/os-release", want_root=True)
    assert "some read-only output" in res["stdout"], "the fallback did not run"
    assert len(calls) == 2


@pytest.mark.parametrize("refusal", [
    "sudo: a password is required",
    "admin is not in the sudoers file.",
])
def test_the_fallback_says_it_was_not_root(agent, monkeypatch, refusal):
    """The whole point. Unprivileged output that is not LABELLED unprivileged is
    how a probe's refusal gets read as a finding about the host."""
    _record(agent, monkeypatch, {
        ELEVATED: {"stdout": "", "stderr": refusal, "returncode": 1},
        PLAIN: {"stdout": "whoami: admin", "stderr": "", "returncode": 0},
    })
    res = agent._run_as_user("admin", "whoami", want_root=True)
    assert "NOT RUN AS ROOT" in res["stderr"], \
        "unprivileged output is being returned with nothing to say so"
    assert "admin" in res["stderr"], "the message does not name the user that cannot sudo"


def test_the_password_is_fed_on_stdin_not_argv(agent, monkeypatch):
    """Unchanged from before, and worth keeping pinned: a sudo password on argv
    is visible in `ps` to every user on the host."""
    calls = _record(agent, monkeypatch, {ELEVATED: {"stdout": "", "stderr": "", "returncode": 0}})
    agent._run_as_user("admin", "whoami", become_password="hunter2", want_root=True)
    assert all("hunter2" not in c["argv"] for c in calls), "the password reached argv"
    assert any((c["stdin"] or "").strip() == "hunter2" for c in calls), \
        "the password was not supplied on stdin"
    assert any("sudo -S" in c["argv"] for c in calls), "sudo -S is not used with a password"


# ---- everything else is untouched -----------------------------------------

def test_without_want_root_the_old_behaviour_is_exactly_as_before(agent, monkeypatch):
    """want_root is opt-in, set only by callers whose UI promises root. Every
    other RBAC action keeps running unprivileged first, which is what lets a
    user with no sudo read things."""
    calls = _record(agent, monkeypatch, {PLAIN: {"stdout": "ok", "stderr": "", "returncode": 0}})
    res = agent._run_as_user("admin", "cat /etc/hostname")
    assert res["stdout"] == "ok"
    assert len(calls) == 1 and "sudo" not in calls[0]["argv"], \
        "a plain RBAC command is now elevating first"


def test_the_old_escalate_on_privilege_error_path_still_works(agent, monkeypatch):
    calls = _record(agent, monkeypatch, {
        PLAIN: {"stdout": "", "stderr": "Permission denied", "returncode": 1},
        ELEVATED: {"stdout": "elevated", "stderr": "", "returncode": 0},
    })
    res = agent._run_as_user("admin", "systemctl restart nginx")
    assert res["stdout"] == "elevated"
    assert len(calls) == 2


# ---- the wiring -----------------------------------------------------------

def test_the_console_script_action_asks_for_root():
    """The flag has to actually be SET by the thing whose label promises root.

    Searching for the string "want_root" is not enough — `want_root=False`
    contains it, and this test passed with exactly that substituted in. It has
    to check the VALUE, and that the value is tied to the script action."""
    src = (REPO / "webgui" / "server.py").read_text()
    i = src.index('results.append(_dispatch_one(entry, command, "command", become, token, desc')
    call = src[i:i + 400]
    m = re.search(r"want_root\s*=\s*([^,)\n]+)", call)
    assert m, "the fleet script dispatch does not pass want_root at all"
    value = m.group(1).strip()
    assert value not in ("False", "None", "0"), \
        f"the fleet script dispatch passes want_root={value} — it will run as " \
        f"the operator while the box says it runs as root"
    assert "script" in value, \
        f"want_root={value} is not tied to the script action"


def test_want_root_survives_the_whole_dispatch_chain():
    """Controller -> task -> agent. A break anywhere in here is silent: the
    script simply runs unprivileged again, exits 0, and looks fine."""
    chain = {
        "webgui/server.py": "want_root=want_root",
        "client/_api_dispatch.py": "want_root=want_root",
        "client/_api_users.py": 'body_base["want_root"] = True',
        "backend/models/agent_models.py": "want_root: bool = False",
        "backend/app.py": '_store_want_root(task_id)',
        "host_agent/agent.py": 'want_root=bool(task.get("want_root"))',
    }
    for rel, needle in chain.items():
        assert needle in (REPO / rel).read_text(), f"{rel} drops want_root"


def test_an_agent_that_predates_the_flag_is_unaffected():
    """Agents update on their own heartbeat and some are powered off for weeks.
    An old agent sees an unknown key in the task dict and ignores it."""
    src = (REPO / "host_agent" / "agent.py").read_text()
    assert 'task.get("want_root")' in src, \
        "the agent reads the flag in a way that would KeyError on an older task"
