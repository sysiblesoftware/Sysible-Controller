"""A host carrying both firewalld and ufw, with one of them running.

Reported: "I think it's a problem between firewalld and ufw when both are
installed and only 1 is running." That is exactly it, and the symptom was a
console reading

    Backend: firewalld
    Running: not running
    Starts at boot: enabled

on a host that was firewalled the whole time — by ufw.

WHY. The two backends were asked whether they were running in two different
ways. firewalld got `firewall-cmd --state`, a real state query. ufw got its
status printed as text and grepped for "Status: active" — and `ufw status`
REQUIRES ROOT. When it cannot run it does not say "inactive"; it prints an
error to stderr, the grep matches nothing, and a running ufw reads as a stopped
one. Selection then fell through to "whichever is enabled at boot", which tries
firewalld first, so the dead one won. The port list printed underneath was
firewalld's — not the ruleset actually in force.

Any sudo configuration that does not cover ufw produces this, on every host
that has both installed.

These tests run the REAL generated shell against a simulated host: fake
firewall-cmd, ufw and systemctl on PATH, with the host's condition set by
environment variable. Reading the snippet cannot find this class of bug — the
snippet looks correct, and it is the interaction with a command that refuses
that is wrong.

Run: pytest tests/test_two_firewalls_one_host.py
"""
import os
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from client import _api_firewall as api            # noqa: E402

FIREWALL_CMD = """#!/bin/sh
case "$1" in
  --state) if [ "$FD_RUNNING" = 1 ]; then echo running; exit 0;
           else echo "not running" >&2; exit 252; fi ;;
  --list-ports) echo "443/tcp"; exit 0 ;;
  --list-services) echo "ssh"; exit 0 ;;
esac
exit 0
"""

# The whole point: real ufw refuses to report anything without root.
UFW = """#!/bin/sh
if [ "$UFW_NEEDS_ROOT" = 1 ]; then
  echo "ERROR: You need to be root to run this script" >&2; exit 1
fi
case "$1" in
  status) if [ "$UFW_RUNNING" = 1 ]; then echo "Status: active";
          else echo "Status: inactive"; fi; exit 0 ;;
esac
exit 0
"""

SYSTEMCTL = """#!/bin/sh
_verb="$1"; shift
for a in "$@"; do case "$a" in --*) ;; *) _unit="$a" ;; esac; done
case "$_verb" in
  is-enabled)
    case "$_unit" in
      firewalld) [ "$FD_ENABLED" = 1 ] && exit 0 ;;
      ufw) [ "$UFW_ENABLED" = 1 ] && exit 0 ;;
    esac; exit 1 ;;
  is-active)
    case "$_unit" in
      firewalld) [ "$FD_RUNNING" = 1 ] && exit 0 ;;
      ufw) [ "$UFW_RUNNING" = 1 ] && exit 0 ;;
    esac; exit 3 ;;
esac
exit 0
"""


@pytest.fixture
def sh(tmp_path):
    """Run a generated snippet on a host you describe."""
    binp = tmp_path / "bin"
    binp.mkdir()

    def run(snippet, firewalld=True, ufw=True, **state):
        for f in binp.iterdir():
            f.unlink()
        (binp / "systemctl").write_text(SYSTEMCTL)
        (binp / "systemctl").chmod(0o755)
        if firewalld:
            (binp / "firewall-cmd").write_text(FIREWALL_CMD)
            (binp / "firewall-cmd").chmod(0o755)
        if ufw:
            (binp / "ufw").write_text(UFW)
            (binp / "ufw").chmod(0o755)
        env = {"PATH": f"{binp}:/usr/bin:/bin", "FD_RUNNING": "0", "UFW_RUNNING": "0",
               "FD_ENABLED": "0", "UFW_ENABLED": "0", "UFW_NEEDS_ROOT": "0"}
        env.update({k: str(v) for k, v in state.items()})
        r = subprocess.run(["sh", "-c", snippet], capture_output=True, text=True,
                           env=env, timeout=60)
        return (r.stdout + r.stderr).strip()

    return run


def _pick(sh, **state):
    return sh(api._FW_PICK + "_fw_pick", **state).strip().splitlines()[-1]


# ---- the reported bug -------------------------------------------------------
def test_a_running_ufw_is_not_lost_when_its_status_needs_root(sh):
    """The whole report. ufw is enforcing, firewalld is enabled but dead, and
    `ufw status` is refused — which used to hand the host to firewalld."""
    got = _pick(sh, UFW_RUNNING=1, UFW_ENABLED=1, FD_ENABLED=1, UFW_NEEDS_ROOT=1)
    assert got == "ufw", (
        f"picked {got!r}: a running ufw was treated as stopped because its "
        f"status command was refused, and the host was handed to a firewalld "
        f"that is not running")


def test_the_status_screen_does_not_call_a_firewalled_host_unprotected(sh):
    out = sh(api.cmd_fw_status(), UFW_RUNNING=1, UFW_ENABLED=1, FD_ENABLED=1,
             UFW_NEEDS_ROOT=1)
    assert "Backend: ufw" in out, out
    assert "Running: running" in out, out
    # And it says why the rules are missing rather than printing a root error
    # where a rule list should be.
    assert "needs root" in out, out
    assert "443/tcp" not in out, \
        "it printed firewalld's ports for a host ufw is enforcing"


def test_running_state_does_not_depend_on_being_root(sh):
    """systemctl answers it, and systemctl does not need root."""
    rooted = sh(api.cmd_fw_status(), UFW_RUNNING=1, UFW_ENABLED=1)
    refused = sh(api.cmd_fw_status(), UFW_RUNNING=1, UFW_ENABLED=1, UFW_NEEDS_ROOT=1)
    for out in (rooted, refused):
        assert "Backend: ufw" in out and "Running: running" in out, out


# ---- which backend owns the host -------------------------------------------
@pytest.mark.parametrize("label,state,want", [
    ("firewalld running, ufw idle", dict(FD_RUNNING=1), "firewalld"),
    ("ufw running, firewalld idle", dict(UFW_RUNNING=1), "ufw"),
    ("ufw running, firewalld enabled but stopped",
     dict(UFW_RUNNING=1, FD_ENABLED=1), "ufw"),
    ("firewalld running, ufw enabled but stopped",
     dict(FD_RUNNING=1, UFW_ENABLED=1), "firewalld"),
    ("neither running, firewalld enabled", dict(FD_ENABLED=1), "firewalld"),
    ("neither running, ufw enabled", dict(UFW_ENABLED=1), "ufw"),
    ("neither running nor enabled", dict(), "firewalld"),
])
def test_the_backend_in_charge_is_the_one_picked(sh, label, state, want):
    """What is ENFORCING wins, then what is set to start, then what is
    installed — and a rule has to go where it will actually take effect."""
    assert _pick(sh, **state) == want, label


def test_only_one_installed_is_the_one_used(sh):
    assert sh(api._FW_PICK + "_fw_pick", firewalld=False,
              UFW_RUNNING=1).strip() == "ufw"
    assert sh(api._FW_PICK + "_fw_pick", ufw=False,
              FD_RUNNING=1).strip() == "firewalld"


def test_no_firewall_at_all_says_so(sh):
    assert sh(api._FW_PICK + "_fw_pick", firewalld=False,
              ufw=False).strip() == "none"


# ---- the other one on the host ---------------------------------------------
def test_both_running_is_called_out_as_dangerous(sh):
    """The condition the enable guards exist to prevent, but nothing reported
    once a host was already in it — and it is the one where rules come and go."""
    out = sh(api.cmd_fw_status(), FD_RUNNING=1, UFW_RUNNING=1,
             FD_ENABLED=1, UFW_ENABLED=1)
    assert "BOTH running" in out, out
    assert "overwrite each other" in out, out


def test_the_idle_one_is_named_so_not_running_is_not_alarming(sh):
    """"Running: not running" on the other backend is the normal, correct state
    of a host with both installed. Saying which one is in charge is what stops
    it reading as a fault."""
    out = sh(api.cmd_fw_status(), FD_RUNNING=1, FD_ENABLED=1, UFW_ENABLED=1)
    assert "ufw is also installed" in out, out
    assert "firewalld is the one in charge" in out, out
    assert "BOTH running" not in out, out


def test_a_host_with_one_firewall_is_not_told_about_the_other(sh):
    out = sh(api.cmd_fw_status(), ufw=False, FD_RUNNING=1, FD_ENABLED=1)
    assert "also installed" not in out, out
