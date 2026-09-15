"""A reboot the console can actually report.

Clicking "Reboot host" (Quick System Actions, or the posture page's one-click
fix) spun for the full agent timeout and then went red with "timed out waiting
for agent" — on a host that had in fact rebooted. The command was
`shutdown -r +0 || systemctl reboot`: +0 is IMMEDIATE, so init started tearing
the host down while the agent was still inside the task, and the result POST
that would have turned the button green never happened. Power-off was the same,
and an SSH host fared no better (the connection died mid-command).

The fix is the one cmd_restart_agent already uses for the identical problem —
hand the work to init detached and return right away — so these pin the
properties that make the result reportable:

  * the power action is SCHEDULED with a grace period, not run inline;
  * the command returns in milliseconds, long before the host goes down;
  * a host that may NOT reboot (no privilege, init refuses) says so on stderr
    in the wording the agent escalates on, and never prints a false success.

The shell is exercised for real against a sandboxed PATH — these would pass
against a command that merely *looked* right, so they run it instead.
"""
import os
import shutil
import subprocess
import time

import pytest

from client import api  # noqa: F401 - import FIRST: client.api re-exports the
                        # cmd_* builders by wildcard, and importing a sibling
                        # _api_* module ahead of it leaves that copy partial.
from client import _api_dispatch as dispatch
from host_agent.agent import _looks_like_privilege_error

SUCCESS = "requested"          # substring of the "…requested - this host goes down…" line
GRACE = 2                      # seconds; the real default is longer, see _POWER_GRACE


@pytest.fixture(autouse=True)
def _short_grace(monkeypatch):
    """Keep the detached-branch tests to a couple of seconds, not the real grace."""
    monkeypatch.setattr(dispatch, "_POWER_GRACE", GRACE, raising=False)


@pytest.fixture()
def sandbox(tmp_path):
    """A PATH containing ONLY what the test puts there, so `command -v systemd-run`
    answers what the test wants rather than whatever this machine happens to have."""
    bindir = tmp_path / "bin"
    bindir.mkdir()

    class Sandbox:
        path = bindir
        tmp = tmp_path

        def fake(self, name, body):
            f = bindir / name
            f.write_text("#!/bin/sh\n" + body + "\n")
            f.chmod(0o755)
            return f

        def real(self, name):
            """Expose a REAL system binary (sleep, setsid, id) inside the sandbox."""
            src = shutil.which(name)
            assert src, f"{name} not available on this machine"
            (bindir / name).symlink_to(src)

        def run(self, command, timeout=30):
            t0 = time.monotonic()
            p = subprocess.run(["/bin/bash", "-c", command], capture_output=True,
                               text=True, env={"PATH": str(bindir)}, timeout=timeout)
            return p, time.monotonic() - t0

    return Sandbox()


@pytest.mark.parametrize("build,verb", [(dispatch.cmd_reboot_host, "reboot"),
                                        (dispatch.cmd_poweroff_host, "poweroff")])
class TestScheduledWithInit:
    def test_it_is_handed_to_init_on_a_timer_not_run_inline(self, sandbox, build, verb):
        """systemd-run must be asked to run `systemctl <verb>` LATER. Run it inline
        and the agent is killed before it can report — the original bug."""
        argv = sandbox.tmp / "argv"
        sandbox.fake("systemd-run", f'printf "%s\\n" "$@" > {argv}; exit 0')

        p, elapsed = sandbox.run(build())

        assert p.returncode == 0, p.stderr
        assert SUCCESS in p.stdout
        # Back in the operator's hands immediately — the agent has the whole grace
        # period to POST the result before the host goes away.
        assert elapsed < GRACE, f"took {elapsed:.1f}s; the host may go down first"
        args = argv.read_text().split()
        assert "systemctl" in args and verb in args, args
        assert any(a.startswith("--on-active=") for a in args), args

    def test_a_refused_schedule_is_never_dressed_up_as_success(self, sandbox, build, verb):
        """polkit's refusal must reach the agent verbatim: it is what makes the
        agent retry the whole command under the host's sudo."""
        sandbox.fake("systemd-run",
                     'echo "Failed to start transient timer unit: '
                     'Interactive authentication required." >&2; exit 1')

        p, _ = sandbox.run(build())

        assert p.returncode != 0
        assert SUCCESS not in p.stdout
        assert "Interactive authentication required" in p.stderr
        assert _looks_like_privilege_error(p.stderr), "the agent would not escalate"


@pytest.mark.parametrize("build,flag", [(dispatch.cmd_reboot_host, "-r"),
                                        (dispatch.cmd_poweroff_host, "-P")])
class TestWithoutSystemdRun:
    """No systemd-run (a non-systemd or minimal host): the classic `shutdown` is
    detached into its own session instead, and must behave the same way."""

    def test_the_command_returns_first_and_the_host_goes_down_after(self, sandbox, build, flag):
        marker = sandbox.tmp / "went-down"
        sandbox.fake("shutdown", f'echo "shutdown $*" > {marker}')
        sandbox.real("sh")
        sandbox.real("sleep")
        sandbox.real("setsid")
        sandbox.real("id")

        p, elapsed = sandbox.run(build())

        assert p.returncode == 0, p.stderr
        assert SUCCESS in p.stdout
        assert elapsed < GRACE, f"took {elapsed:.1f}s"
        assert not marker.exists(), "the host went down before the result could be sent"
        time.sleep(GRACE + 2)
        assert marker.exists(), "the detached power action never ran"
        assert flag in marker.read_text()

    def test_an_unprivileged_host_says_so_instead_of_faking_success(self, sandbox, build, flag):
        """Detaching hides the exit code, so privilege is proven BEFORE detaching —
        otherwise an operator with no sudo gets a green button and a host that
        never reboots."""
        sandbox.fake("id", "echo 1000")
        sandbox.fake("shutdown", 'echo "SHOULD NOT RUN" >&2')
        sandbox.real("sh")
        sandbox.real("sleep")
        sandbox.real("setsid")

        p, _ = sandbox.run(build())

        assert p.returncode != 0
        assert SUCCESS not in p.stdout
        assert _looks_like_privilege_error(p.stderr), p.stderr


class TestThroughTheConsole:
    """End to end: the operator clicks the button and gets a green result.

    The agent here runs the command for real against a host that goes down the
    moment init is told to — modelled as a `shutdown` that never returns. If the
    command blocks there, the agent dies mid-task and reports nothing, which is
    exactly the "timed out waiting for agent" the operator was seeing.
    """

    @pytest.fixture()
    def console(self, monkeypatch, sandbox):
        import webgui.server as w

        entry = {"id": "h1", "label": "web1", "kind": "agent", "environment": "Dev"}
        # The host's init: told to go down, it never gives the shell back.
        sandbox.fake("shutdown", "sleep 600")
        sandbox.fake("systemctl", "sleep 600")
        sandbox.fake("systemd-run", "exit 0")
        sandbox.real("sh")
        sandbox.real("sleep")
        sandbox.real("setsid")
        sandbox.real("id")

        results = {}

        def run_on_entry(e, command, **kw):
            try:
                p, _ = sandbox.run(command, timeout=4)
            except subprocess.TimeoutExpired:
                return {"sync": False, "task_id": "t1", "error": None}   # host died mid-task
            results["t1"] = {"stdout": p.stdout, "stderr": p.stderr, "code": p.returncode}
            return {"sync": False, "task_id": "t1", "error": None}

        monkeypatch.setattr(w.dispatch, "list_merged_hosts", lambda **k: [dict(entry)])
        monkeypatch.setattr(w.dispatch, "run_on_entry", run_on_entry)
        monkeypatch.setattr(w.dispatch, "poll_entry_result", lambda e, t: results.get(t))
        monkeypatch.setattr(w.api, "log_action", lambda *a, **k: None)
        monkeypatch.setattr(w.api, "admin_login", lambda u, p: {
            "role": "superuser", "token": "tok", "must_change_password": False,
            "sudo_connect": False})
        monkeypatch.setattr(w.api, "whoami", lambda: {"username": "op", "role": "superuser"})
        monkeypatch.setenv("SYSIBLE_WEBGUI_TASK_TIMEOUT", "3")

        from fastapi.testclient import TestClient
        c = TestClient(w.app, base_url="https://testserver")
        origin = {"origin": "https://testserver", "host": "testserver"}
        assert c.post("/api/login", json={"username": "op", "password": "pw"},
                      headers=origin).status_code == 200
        c.headers.update(origin)
        return c

    @pytest.mark.parametrize("action", ["qsa_reboot", "qsa_poweroff"])
    def test_quick_system_actions_reports_success(self, console, action):
        r = console.post(f"/api/tool/{action}", json={"targets": ["h1"], "params": {}})
        assert r.status_code == 200, r.text
        result = r.json()["results"][0]
        assert result["error"] != "timed out waiting for agent"
        assert result["ok"] is True, result
        assert SUCCESS in result["stdout"]

    @pytest.mark.parametrize("action", ["reboot", "poweroff"])
    def test_the_fleet_power_buttons_report_success(self, console, action):
        r = console.post("/api/fleet", json={"action": action, "targets": ["h1"]})
        assert r.status_code == 200, r.text
        result = r.json()["results"][0]
        assert result["error"] != "timed out waiting for agent"
        assert result["ok"] is True, result
        assert SUCCESS in result["stdout"]
