"""Turning a firewall on must not cut the connection it is being turned on over.

From a fleet that went dark: every agent's journal filled with

    ConnectTimeoutError('Connection to 192.168.8.139 timed out.')

after a firewall was enabled on the controller. Nothing was wrong with the
agents. 9000 — the agent + CLI API — had simply stopped being accepted.

install_sysible.sh has opened 9000/8800 since the day a fresh install came up
silently unreachable, and says so in its own comment. But it runs at install
time, and only when a firewall is ALREADY active. Enable one afterwards from the
console and nothing reopens them, so the button strands the fleet.

ufw is worse than firewalld here: its default-deny takes SSH with it, which is
precisely what the interactive "may disrupt existing ssh connections" prompt
warns about — and the builder passes --force to skip that prompt, because
otherwise it hangs. Skipping the warning means answering it ourselves.

So the enable actions keep open what is ALREADY serving: sshd, and Sysible's own
control plane where this host actually serves it. Nothing else — a firewall that
opens every listening port is not a firewall.
"""
import subprocess

import pytest

from client import api  # noqa: F401 - import FIRST; see test_power_actions_report_back
from client import _api_firewall as fw
from client._api_dispatch import _with_sbin_path


def _listener(port, proc=None):
    users = ' users:(("%s",pid=1,fd=3))' % proc if proc else ""
    return "LISTEN 0 128 0.0.0.0:%s 0.0.0.0:*%s" % (port, users)


CONTROLLER = [_listener(22, "sshd"), _listener(9000, "docker-proxy"),
              _listener(8800, "docker-proxy"), _listener(5432, "postgres")]


@pytest.fixture()
def box(tmp_path):
    """A host with a sandboxed PATH, whose listening sockets the test decides,
    and which records every rule the command asks its firewall for."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    rules = tmp_path / "rules"
    rules.write_text("")

    class Box:
        def _put(self, name, body):
            f = bindir / name
            f.write_text("#!/bin/sh\n" + body + "\n")
            f.chmod(0o755)

        def listening(self, lines, sshd_port=None, have_ss=True):
            if have_ss:
                withp = " ".join("'%s'" % l for l in lines)
                plain = " ".join("'%s'" % l.split(" users")[0] for l in lines)
                self._put("ss", 'case "$*" in *p*) printf "%%s\\n" %s;; '
                                '*) printf "%%s\\n" %s;; esac' % (withp, plain))
            self._put("sshd", 'echo "port %s"' % sshd_port if sshd_port else "exit 1")
            self._put("ufw", 'if [ "$1" = "allow" ]; then echo "$2" >> %s; fi; '
                             'echo "Firewall is active"; exit 0' % rules)
            self._put("firewall-cmd",
                      'case "$*" in *--add-port=*) echo "$*" '
                      '| sed "s/.*--add-port=//;s/ .*//" >> %s;; esac; exit 0' % rules)
            self._put("systemctl", "exit 0")

        def run(self, command):
            return subprocess.run(["/bin/bash", "-c", _with_sbin_path(command)],
                                  capture_output=True, text=True,
                                  # coreutils too: the discovery pipeline uses
                                  # awk/sed/grep/sort, which every real host has.
                                  # Only the firewall tools are faked.
                                  env={"PATH": f"{bindir}:/usr/bin:/bin"}, timeout=30)

        @property
        def kept_open(self):
            return sorted({l.strip() for l in rules.read_text().splitlines() if l.strip()})

    return Box()


# ---- the outage this exists to prevent -------------------------------------
@pytest.mark.parametrize("build", [fw.cmd_set_ufw_enabled, fw.cmd_set_firewalld_enabled])
def test_the_agent_api_survives_enabling_a_firewall_on_the_controller(box, build):
    """9000 is how every managed host reaches the controller. Enabling a firewall
    without it is a fleet-wide outage, and the agents cannot report it — they are
    the ones that just got cut off."""
    box.listening(CONTROLLER, sshd_port=22)

    p = box.run(build(True))

    assert p.returncode == 0, p.stderr
    assert "9000/tcp" in box.kept_open, "the whole fleet just went offline"


@pytest.mark.parametrize("build", [fw.cmd_set_ufw_enabled, fw.cmd_set_firewalld_enabled])
def test_the_console_survives_too(box, build):
    box.listening(CONTROLLER, sshd_port=22)
    box.run(build(True))
    assert "8800/tcp" in box.kept_open


@pytest.mark.parametrize("build", [fw.cmd_set_ufw_enabled, fw.cmd_set_firewalld_enabled])
def test_ssh_survives(box, build):
    """ufw --force skips the prompt that would have warned about this."""
    box.listening(CONTROLLER, sshd_port=22)
    box.run(build(True))
    assert "22/tcp" in box.kept_open


def test_a_non_default_ssh_port_is_found(box):
    """Asking sshd rather than assuming 22 — a host moved off 22 is exactly the
    host an admin cannot afford to lock themselves out of."""
    box.listening([_listener(2222, "sshd")], sshd_port=2222)
    box.run(fw.cmd_set_ufw_enabled(True))
    assert box.kept_open == ["2222/tcp"]


# ---- it must still be a firewall -------------------------------------------
def test_it_does_not_open_every_listening_port(box):
    """The point of the firewall is that most of these stay shut."""
    box.listening([_listener(22, "sshd"), _listener(80, "nginx"),
                   _listener(5432, "postgres"), _listener(6379, "redis")], sshd_port=22)

    box.run(fw.cmd_set_ufw_enabled(True))

    assert box.kept_open == ["22/tcp"], box.kept_open


def test_a_managed_host_gets_no_controller_ports(box):
    """9000/8800 are opened where they are SERVED, not everywhere. A managed host
    listens on neither, and must not end up with them open."""
    box.listening([_listener(22, "sshd")], sshd_port=22)
    box.run(fw.cmd_set_ufw_enabled(True))
    assert box.kept_open == ["22/tcp"]


# ---- and it must never be the reason the button fails ----------------------
def test_a_host_with_nothing_discoverable_still_enables(box):
    """No ss, no `sshd -T`. Opening nothing is the honest answer; refusing to
    enable the firewall because we could not introspect it would not be."""
    box.listening([], sshd_port=None, have_ss=False)

    p = box.run(fw.cmd_set_ufw_enabled(True))

    assert p.returncode == 0, p.stderr
    assert box.kept_open == []


def test_what_it_opened_is_reported(box):
    """An operator must be able to see which holes were left, without diffing
    the ruleset afterwards."""
    box.listening(CONTROLLER, sshd_port=22)

    p = box.run(fw.cmd_set_ufw_enabled(True))

    said = [l for l in p.stdout.splitlines() if "keeping" in l]
    assert len(said) == 3, p.stdout
    assert all("in use on this host" in l for l in said)


def test_ufw_stages_the_rules_before_it_enables(box):
    """Order is load-bearing for ufw: `ufw allow` on an inactive ufw stages the
    rule so it is in force the instant ufw comes up. Allowing afterwards means
    cutting SSH and then needing the host to repair it."""
    cmd = fw.cmd_set_ufw_enabled(True)
    assert cmd.index("_sysible_keep_open") < cmd.index("ufw --force enable"), \
        "ufw is enabled before the ports are staged"
