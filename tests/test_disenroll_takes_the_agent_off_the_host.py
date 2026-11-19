"""Disenrolling a host removes the agent from it, with nothing left behind.

Asked for plainly: "when you disenroll the agent should delete the service
from the host completely with no artifacts". Before this, Disenroll removed
the controller's record and the agent went on running -- and after the last
fix, stopped itself and sat there failed, with /opt/sysible-agent, the unit
file, the pinned certificate and the state file all still on the machine. The
console's own confirm admitted it: "Each agent keeps running on its host until
you stop/uninstall it there".

THE DANGEROUS PART IS WHICH SIGNAL IT ACTS ON. The agent used to learn it was
gone from a 404 "Unknown host_id" -- which is also exactly what a controller
whose database was reset, or restored from last week's backup, says to a
perfectly healthy fleet. An agent that uninstalled itself on that answer would
strip every host it manages, and no one would get those agents back without
walking to each machine.

So a deliberate removal now leaves a tombstone, and only a host_id with one is
ever answered 410 Gone. A fresh or restored database has no tombstones, every
agent gets the old cautious 404, and nothing is removed from anything.

Run: pytest tests/
"""
import importlib
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from backend import db                                     # noqa: E402
from backend import agent_bundle                           # noqa: E402

AGENT_SRC = (REPO / "host_agent" / "agent.py").read_text(encoding="utf-8")


def _enrolled(host_id="h-1", hostname="ubuntu-lab", secret="s3cret"):
    db.create_or_update_agent(host_id, hostname, "linux", "6.8", "online",
                              1700000000.0, secret, "192.168.155.135")
    return host_id, secret


# ---- the controller tells the two cases apart ------------------------------
def test_disenrolling_leaves_a_record_that_we_let_it_go():
    host_id, _ = _enrolled()
    db.delete_agent(host_id)
    rec = db.disenrollment_record(host_id)
    assert rec and rec["host_id"] == host_id, rec
    assert rec["at"], "no time recorded, so nothing can age it out later"


def test_the_tombstone_keeps_the_name_for_whoever_reads_it_later():
    host_id, _ = _enrolled(hostname="ubuntu-lab")
    db.delete_agent(host_id)
    assert db.disenrollment_record(host_id)["hostname"] == "ubuntu-lab"


def test_a_host_we_never_knew_has_no_record():
    assert db.disenrollment_record("never-heard-of-it") is None


def test_re_enrolling_forgets_it(controller):
    """The host_id is machine-derived, so a re-enroll lands on the SAME one.
    Left behind, the tombstone would hand the host its own death warrant on
    the first heartbeat, seconds after an operator put it back."""
    host_id, _ = _enrolled()
    db.delete_agent(host_id)
    assert db.disenrollment_record(host_id)
    db.clear_disenrollment(host_id)
    assert db.disenrollment_record(host_id) is None


def test_a_disenrolled_agent_is_told_410_not_404(controller):
    import backend.app as app_module
    host_id, secret = _enrolled()
    db.delete_agent(host_id)
    with pytest.raises(Exception) as caught:
        app_module.verify_agent(host_id, secret)
    assert getattr(caught.value, "status_code", None) == 410, caught.value
    assert str(caught.value.detail).startswith("Disenrolled"), caught.value.detail


def test_a_reset_database_still_says_404(controller):
    """THE one that matters. Every agent in a healthy fleet asks a controller
    whose database was just restored from backup, and every one of them must
    be told to sit still."""
    import backend.app as app_module
    with pytest.raises(Exception) as caught:
        app_module.verify_agent("some-host-id", "whatever")
    assert getattr(caught.value, "status_code", None) == 404, caught.value
    assert caught.value.detail == "Unknown host_id"


# ---- the agent acts on one of them and not the other -----------------------
def test_the_agent_has_a_separate_error_for_being_let_go():
    assert "class DisenrolledError" in AGENT_SRC
    i = AGENT_SRC.index("class DisenrolledError")
    j = AGENT_SRC.index("class UnknownHostError")
    assert "410" in AGENT_SRC[i:j], "nothing explains which status this is"


def test_410_raises_the_one_that_uninstalls_and_404_does_not():
    m = re.search(r"if r\.status_code == 410.*?raise DisenrolledError", AGENT_SRC, re.S)
    assert m, "a 410 is not turned into DisenrolledError"
    assert "Disenrolled" in m.group(0), "any 410 would do, which is too broad"
    m404 = re.search(r"if r\.status_code == 404.*?raise UnknownHostError", AGENT_SRC, re.S)
    assert m404, "a 404 no longer raises UnknownHostError"


def test_only_the_disenrolled_path_uninstalls():
    """The 404 path must keep doing what it did: stop, explain, change
    nothing on the host."""
    i = AGENT_SRC.index("except UnknownHostError:")
    body = AGENT_SRC[i:i + 1200]
    assert "self_uninstall" not in body, \
        "a controller that merely forgot this host now wipes the agent off it"


def test_the_disenrolled_path_clears_state_before_it_removes_anything():
    i = AGENT_SRC.index("except DisenrolledError:")
    body = AGENT_SRC[i:i + 800]
    assert body.index("clear_state()") < body.index("self_uninstall()"), \
        "a teardown that fails half way would leave a credentialed agent behind"
    assert "sys.exit(EXIT_NEEDS_OPERATOR)" in body


def test_the_uninstall_is_detached_and_does_not_notify():
    """`systemctl disable --now` stops the process that launched it. Run in
    this process's session, the teardown would be killed by its own second
    step."""
    i = AGENT_SRC.index("def self_uninstall")
    body = AGENT_SRC[i:i + 2000]
    assert "start_new_session=True" in body, "the teardown dies with the agent"
    assert '"--local-only"' in body, \
        "it would spend ten seconds telling a controller that already knows"
    assert "stdout" not in body.split("Popen")[1][:400], \
        "stdout is redirected away from the journal, so a failed removal is silent"


def test_a_missing_uninstaller_is_reported_not_pretended():
    i = AGENT_SRC.index("def self_uninstall")
    body = AGENT_SRC[i:i + 2000]
    assert "return False" in body and "disenroll_agent.sh" in body


# ---- the uninstaller is on the host, and does what it says ------------------
def test_run_agent_installs_the_uninstaller():
    """Every instruction for removing the agent named a file that was never
    installed, so "run disenroll_agent.sh" meant "find the zip you used
    months ago"."""
    run = agent_bundle._run_script(False)
    assert re.search(r"cp -f \./disenroll_agent\.sh\s+\S+/disenroll_agent\.sh", run), run[-1500:]
    assert re.search(r"chmod 755 \S+/disenroll_agent\.sh", run)


def test_the_bundle_still_ships_it():
    src = (REPO / "backend" / "agent_bundle.py").read_text(encoding="utf-8")
    assert 'zf.writestr("disenroll_agent.sh"' in src


@pytest.fixture()
def fake_host(tmp_path):
    """A PATH where every destructive command only writes down what it was
    asked to do, so the real script can be run and read."""
    b = tmp_path / "bin"
    b.mkdir()
    log = tmp_path / "did.log"
    for name in ("rm", "rmdir", "systemctl", "userdel", "id", "sed", "hostname",
                 "python3"):
        f = b / name
        f.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit 0\n')
        f.chmod(0o755)
    script = tmp_path / "disenroll_agent.sh"
    script.write_text(agent_bundle._disenroll_script(), encoding="utf-8")
    script.chmod(0o755)
    return {"script": script, "log": log,
            "env": {"PATH": f"{b}:{os.environ.get('PATH','/usr/bin:/bin')}",
                    "EUID": "0", "HOME": "/root"}}


def _run(fake_host, *args):
    r = subprocess.run(["bash", str(fake_host["script"]), *args],
                       capture_output=True, text=True, env=fake_host["env"],
                       timeout=60)
    did = Path(fake_host["log"]).read_text() if Path(fake_host["log"]).exists() else ""
    return r, did


@pytest.fixture()
def host_state():
    """A state file where the script looks for one.

    Without it the notify step is skipped anyway -- which is how the first
    version of the --local-only test passed with the flag ignored: it asserted
    the absence of a line that was never going to be printed. The flag can
    only be shown to work when the thing it suppresses would otherwise fire.
    """
    f = Path("/var/lib/sysible/agent_state.json")
    f.parent.mkdir(parents=True, exist_ok=True)
    had = f.exists()
    if not had:
        f.write_text('{"host_id":"h","agent_secret":"s"}')
    yield f
    if not had and f.exists():
        f.unlink()


@pytest.mark.skipif(os.geteuid() != 0, reason="the script refuses to run as non-root")
def test_without_the_flag_it_notifies_the_controller(fake_host, host_state):
    """The operator-run path is unchanged: it tells the controller first,
    while it still has the credentials to prove who it is."""
    r, _did = _run(fake_host)
    assert "Notifying controller" in r.stdout, r.stdout


@pytest.mark.skipif(os.geteuid() != 0, reason="the script refuses to run as non-root")
def test_local_only_tells_nobody_even_with_credentials_to_hand(fake_host, host_state):
    r, _did = _run(fake_host, "--local-only")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Notifying controller" not in r.stdout, r.stdout
    assert "controller already knows" in r.stdout, r.stdout


@pytest.mark.skipif(os.geteuid() != 0, reason="the script refuses to run as non-root")
def test_local_only_still_removes_everything(fake_host, host_state):
    r, did = _run(fake_host, "--local-only")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "systemctl disable --now" in did, did
    for gone in ("/opt/sysible-agent", "agent_state.json"):
        assert gone in did, f"{gone} is left on the host:\n{did}"
    assert "daemon-reload" in did, "the unit file is removed without telling systemd"


@pytest.mark.skipif(os.geteuid() != 0, reason="the script refuses to run as non-root")
def test_an_unknown_flag_is_refused_rather_than_ignored(fake_host):
    r, _ = _run(fake_host, "--definitely-not-a-flag")
    assert r.returncode == 2, r.stdout + r.stderr
