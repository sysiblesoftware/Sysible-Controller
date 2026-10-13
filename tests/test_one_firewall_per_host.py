"""ufw and firewalld must not be enabled on the same host.

Both own the kernel's netfilter tables. With both enabled they overwrite each
other, and the failure is not a clean error — it is a host that degrades.
Observed on an openSUSE box where firewalld is the native firewall and ufw was
installed and enabled from this console beside it: firewalld went from a
one-second start to 48s, then 62s, then a 90-second `start operation timed out`,
and the host stopped passing traffic — it could not even ping its own address.

The console offered both on every host and said nothing about it. Each enable
action now refuses while the other is in charge. "In charge" includes
enabled-at-boot and not currently running, because otherwise the collision waits
for the next reboot, which is a much worse time to discover it.
"""
import subprocess

import pytest

from client import api  # noqa: F401 - import FIRST; see test_power_actions_report_back
from client import _api_firewall as fw
from client._api_dispatch import _with_sbin_path
from host_agent.agent import _PRIV_ERROR_HINTS


@pytest.fixture()
def box(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()

    class Box:
        def tool(self, name, body):
            f = bindir / name
            f.write_text("#!/bin/sh\n" + body + "\n")
            f.chmod(0o755)

        def remove(self, name):
            (bindir / name).unlink()

        def systemd(self, active=(), enabled=()):
            """A systemctl that answers for the named units and succeeds at the
            verbs that actually do something. NOTE the argument scan: the real
            calls are `systemctl is-active --quiet firewalld`, so the unit is not
            $2 — reading it as $2 is how the first version of this fixture tested
            the string "--quiet" and quietly passed on nothing."""
            # Stateful, because the builders enable a unit and then ask whether
            # it came up: a fake that always answered "not active" failed the
            # enable's own success check, which looked like the guard misfiring.
            state = tmp_path / "running"
            state.write_text(" ".join(active))
            self.tool("systemctl", f"""
unit=""
for a in "$@"; do case "$a" in -*|is-*|enable|disable|start|stop) ;; *) unit="$a";; esac; done
case "$1" in
  is-active)  case " $(cat {state}) "   in *" $unit "*) exit 0;; esac; exit 3 ;;
  is-enabled) case " {' '.join(enabled)} " in *" $unit "*) exit 0;; esac; exit 1 ;;
  enable)     printf '%s %s' "$(cat {state})" "$unit" > {state}; exit 0 ;;
  disable)    exit 0 ;;
esac
exit 0""")

        def run(self, command):
            return subprocess.run(["/bin/bash", "-c", _with_sbin_path(command)],
                                  capture_output=True, text=True,
                                  env={"PATH": f"{bindir}:/usr/bin:/bin"}, timeout=30)

    # Both installed; neither running nor enabled.
    b = Box()
    b.tool("firewall-cmd", "exit 0")
    b.tool("ufw", 'echo "Status: inactive"; exit 0')
    b.tool("ss", "exit 1")
    b.tool("sshd", "exit 1")
    b.systemd()
    return b


# ---- the refusal -----------------------------------------------------------
def test_ufw_refuses_while_firewalld_is_running(box):
    box.systemd(active=["firewalld"])

    p = box.run(fw.cmd_set_ufw_enabled(True))

    assert p.returncode != 0, "both firewalls were just enabled on one host"
    assert "firewalld is already managing" in p.stderr
    assert "Disable firewalld" in p.stderr, "it does not say how to proceed"


def test_ufw_refuses_while_firewalld_is_merely_enabled_at_boot(box):
    """Not running right now, but it comes back on the next reboot — and then
    both are up, which is the state that broke the host."""
    box.systemd(enabled=["firewalld"])

    p = box.run(fw.cmd_set_ufw_enabled(True))

    assert p.returncode != 0
    assert "start at boot" in p.stderr


def test_firewalld_refuses_while_ufw_is_active(box):
    box.tool("ufw", 'echo "Status: active"; exit 0')

    p = box.run(fw.cmd_set_firewalld_enabled(True))

    assert p.returncode != 0, "both firewalls were just enabled on one host"
    assert "ufw is already managing" in p.stderr
    assert "Disable ufw" in p.stderr


def test_firewalld_refuses_while_ufw_is_merely_enabled_at_boot(box):
    box.systemd(enabled=["ufw"])

    p = box.run(fw.cmd_set_firewalld_enabled(True))

    assert p.returncode != 0
    assert "ufw is already managing" in p.stderr


# ---- and it must not refuse when there is no conflict ----------------------
def test_ufw_enables_freely_when_firewalld_is_not_in_charge(box):
    """firewalld installed but neither running nor enabled is not a conflict —
    plenty of hosts ship the package without using it."""
    p = box.run(fw.cmd_set_ufw_enabled(True))
    assert p.returncode == 0, p.stderr


def test_firewalld_enables_freely_when_ufw_is_not_in_charge(box):
    p = box.run(fw.cmd_set_firewalld_enabled(True))
    assert p.returncode == 0, p.stderr


def test_firewalld_does_not_refuse_over_a_ufw_that_is_not_installed(box):
    """`systemctl is-enabled ufw` can answer oddly on a host with no ufw at all,
    so the binary's absence is the authority."""
    box.remove("ufw")
    box.systemd(enabled=["ufw"])        # a leftover unit file, no ufw installed

    p = box.run(fw.cmd_set_firewalld_enabled(True))

    assert p.returncode == 0, p.stderr


# ---- the refusal must not be mistaken for a privilege problem --------------
@pytest.mark.parametrize("build", [fw.cmd_set_ufw_enabled, fw.cmd_set_firewalld_enabled])
def test_the_refusal_does_not_look_like_a_privilege_error(build):
    """The agent retries under sudo on a privilege-looking failure. Retrying this
    one changes nothing and buries the real message."""
    cmd = build(True)
    ours = " ".join(part.split("'")[0] for part in cmd.split("echo '")[1:]).lower()
    hits = [h for h in _PRIV_ERROR_HINTS if h in ours]
    assert not hits, f"the refusal would be retried under sudo: {hits}"


# ---- disabling is never blocked -------------------------------------------
@pytest.mark.parametrize("build", [fw.cmd_set_ufw_enabled, fw.cmd_set_firewalld_enabled])
def test_turning_one_off_is_never_blocked_by_the_other(box, build):
    """Disabling is the documented way out of a conflict. Gating it on the same
    check would leave an operator with no way to resolve one."""
    box.systemd(active=["firewalld", "ufw"], enabled=["firewalld", "ufw"])
    box.tool("ufw", 'echo "Status: active"; exit 0')

    cmd = build(False)

    assert "already managing" not in cmd, "the way out of a conflict is blocked"
