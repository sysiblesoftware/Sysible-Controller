"""Enabling a firewall must not cut the fleet off from the controller.

Two cases, and they need OPPOSITE rules — which is why this reads each host's
configuration instead of opening 9000 everywhere:

  * A MANAGED host dials OUT to the controller on 9000 and nothing ever dials
    in. ufw can be set to deny outgoing, and when it is, enabling it cuts every
    agent at once. Giving those hosts an INBOUND 9000 rule would open a port on
    fifteen machines that need none — a step backwards, not a fix.
  * The CONTROLLER needs 9000 INBOUND. The listening-port scan catches that
    whenever the service is up; it does not if the container happened to be down
    at the moment the firewall was enabled, which locks the fleet out until
    somebody notices.

The address comes from the agent's own env file, because guessing one would be
worse than doing nothing.
"""
import subprocess

import pytest

from client import api  # noqa: F401 - import FIRST; see test_power_actions_report_back
from client import _api_firewall as fw
from client._api_dispatch import _with_sbin_path

CONTROLLER = "https://192.168.8.139:9000"


@pytest.fixture()
def box(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    rules = tmp_path / "rules"
    rules.write_text("")

    class Box:
        def tool(self, name, body):
            f = bindir / name
            f.write_text("#!/bin/sh\n" + body + "\n")
            f.chmod(0o755)

        def agent_points_at(self, value):
            """Stand in for /opt/sysible-agent/sysible_agent.env. The builder
            reads an absolute path, so the command is rewritten to read the
            test's copy — the parsing under test is unchanged."""
            env = tmp_path / "agent.env"
            env.write_text(f"SYSIBLE_CONTROLLER={value}\nSYSIBLE_ENROLL_TOKEN=x\n")
            self._env = str(env)

        def no_agent(self):
            self._env = str(tmp_path / "absent.env")

        def listening(self, ports, local_addrs=("10.0.0.9",)):
            lines = " ".join(
                "'LISTEN 0 128 0.0.0.0:%s 0.0.0.0:*'" % p for p in ports) or "''"
            self.tool("ss", 'printf "%%s\\n" %s' % lines)
            self.tool("ip", 'printf "%s\\n" ' + " ".join(
                "'1: eth0    inet %s/24 scope global eth0'" % a for a in local_addrs))

        def run(self, command):
            command = command.replace("/opt/sysible-agent/sysible_agent.env", self._env)
            return subprocess.run(["/bin/bash", "-c", _with_sbin_path(command)],
                                  capture_output=True, text=True,
                                  env={"PATH": f"{bindir}:/usr/bin:/bin"}, timeout=30)

        @property
        def rules(self):
            return rules.read_text()

    b = Box()
    b.tool("ufw", 'echo "$*" >> %s; echo "Firewall is active"; exit 0' % rules)
    b.tool("firewall-cmd", 'echo "$*" >> %s; exit 0' % rules)
    b.tool("sshd", 'echo "port 22"')
    b.tool("systemctl", 'case "$1" in is-active|is-enabled) exit 1;; esac; exit 0')
    b.agent_points_at(CONTROLLER)
    b.listening([22])
    return b


# ---- a managed host: the way OUT must survive -------------------------------
def test_a_managed_host_keeps_its_outbound_path_to_the_controller(box):
    """The failure this prevents is fleet-wide and silent: every agent stops
    checking in at once, and the hosts that could report it are the ones cut."""
    box.run(fw.cmd_set_ufw_enabled(True))
    assert "allow out to 192.168.8.139 port 9000 proto tcp" in box.rules


def test_a_managed_host_is_given_no_inbound_agent_port(box):
    """Nothing ever dials into a managed host on 9000. Opening it there would
    add a listening-port rule to every host in the fleet for no reason."""
    box.run(fw.cmd_set_ufw_enabled(True))
    assert "allow 9000/tcp" not in box.rules, box.rules


def test_a_bare_host_with_no_port_in_the_url_uses_the_agent_api_default(box):
    box.agent_points_at("https://192.168.8.139")
    box.run(fw.cmd_set_ufw_enabled(True))
    assert "port 9000" in box.rules


def test_every_candidate_in_the_list_is_kept_reachable(box):
    """SYSIBLE_CONTROLLER takes a comma-separated candidate list and the agent
    fails over between them, so one allowed address is not enough."""
    box.agent_points_at("https://192.168.8.139:9000,https://10.0.0.5:9443")
    box.run(fw.cmd_set_ufw_enabled(True))
    assert "allow out to 192.168.8.139 port 9000" in box.rules
    assert "allow out to 10.0.0.5 port 9443" in box.rules


def test_a_host_with_no_agent_installed_is_unaffected(box):
    """Not every host runs the agent, and a missing env file must not fail the
    enable or invent an address."""
    box.no_agent()
    p = box.run(fw.cmd_set_ufw_enabled(True))
    assert p.returncode == 0, p.stderr
    assert "allow out" not in box.rules


# ---- the controller: the way IN must survive --------------------------------
def test_the_controller_keeps_9000_open_even_with_nothing_listening(box):
    """The container was down when the firewall went on. Without this the fleet
    has no way back in and no way to say so."""
    box.agent_points_at("https://10.0.0.9:9000")      # that address is ours
    box.listening([22], local_addrs=("10.0.0.9",))    # 9000 NOT listening

    box.run(fw.cmd_set_ufw_enabled(True))

    assert "allow 9000/tcp" in box.rules, box.rules


def test_a_host_that_is_not_the_controller_gets_no_such_rule(box):
    """Same configured address, but it belongs to another machine."""
    box.agent_points_at("https://10.0.0.9:9000")
    box.listening([22], local_addrs=("192.168.1.50",))

    box.run(fw.cmd_set_ufw_enabled(True))

    assert "allow 9000/tcp" not in box.rules, box.rules


def test_the_controller_still_gets_it_from_the_listener_scan(box):
    """Belt and braces: when 9000 IS listening the original path still works,
    whatever the agent config says."""
    box.no_agent()
    box.listening([22, 9000])
    box.run(fw.cmd_set_ufw_enabled(True))
    assert "allow 9000/tcp" in box.rules


# ---- firewalld ---------------------------------------------------------------
def test_firewalld_gets_no_outbound_rule(box):
    """firewalld's zones filter inbound; egress is not restricted by default, so
    an outbound rule there would be ceremony rather than protection."""
    assert "allow out" not in fw.cmd_set_firewalld_enabled(True)


def test_the_agnostic_tab_carries_the_same_protection(box, tmp_path):
    """The Firewall tab is the one most people will use — it must not be the
    weaker path. On a UFW host: the fixture's default has firewall-cmd present,
    so the agnostic action correctly picks firewalld and never reaches the ufw
    branch this is about."""
    (tmp_path / "bin" / "firewall-cmd").unlink()

    box.run(fw.cmd_fw_turn_on())
    assert "allow out to 192.168.8.139 port 9000 proto tcp" in box.rules
