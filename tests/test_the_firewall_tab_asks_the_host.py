"""One set of firewall controls that work on any host.

Every other tab in this tool names a BACKEND, which asks the operator to know
whether each selected host runs firewalld or ufw before they can do anything —
and, with a mixed fleet selected together, to be right about all of them at
once. Being wrong is not a harmless mis-click: enabling the rival firewall is
what left three openSUSE hosts unable to pass traffic.

An operator wants "open 443 on these hosts". Which tool carries that out is the
host's business, and the host knows. These run the real shell against hosts of
each shape and check it asks.
"""
import subprocess

import pytest

from client import api  # noqa: F401 - import FIRST; see test_power_actions_report_back
from client import _api_firewall as fw
from client._api_dispatch import _with_sbin_path


@pytest.fixture()
def box(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "ran"
    log.write_text("")

    class Box:
        def tool(self, name, body):
            f = bindir / name
            f.write_text("#!/bin/sh\n" + body + "\n")
            f.chmod(0o755)

        def record(self, name, extra="exit 0"):
            """A tool that records how it was called, so the test can assert WHICH
            backend did the work rather than guessing from stdout."""
            self.tool(name, f'echo "{name} $*" >> {log}\n' + extra)

        def _only(self, keep):
            """These are DIFFERENT hosts, not a host accumulating tools. Leaving
            the other backend's binary behind made detection correctly pick it,
            and the test read as though the translation were broken."""
            for other in ("firewall-cmd", "ufw"):
                if other != keep and (bindir / other).exists():
                    (bindir / other).unlink()

        # --- hosts of each shape -------------------------------------------
        def firewalld_host(self, running=True):
            self._only("firewall-cmd")
            self.record("firewall-cmd", "exit 0" if running else
                        'case "$1" in --state) exit 1;; esac; exit 0')
            self.tool("systemctl", 'case "$1" in is-enabled) [ "$3" = firewalld ] && exit 0; exit 1;;'
                                   ' is-active) exit 0;; esac; exit 0')

        def ufw_host(self, active=True):
            self._only("ufw")
            self.record("ufw", f'echo "Status: {"active" if active else "inactive"}"; exit 0')
            self.tool("systemctl", 'case "$1" in is-enabled) [ "$3" = ufw ] && exit 0; exit 1;;'
                                   ' is-active) exit 1;; esac; exit 0')

        def bare_host(self):
            self._only(None)
            self.tool("systemctl", "exit 1")

        def run(self, command):
            return subprocess.run(["/bin/bash", "-c", _with_sbin_path(command)],
                                  capture_output=True, text=True,
                                  env={"PATH": f"{bindir}:/usr/bin:/bin"}, timeout=30)

        @property
        def ran(self):
            return log.read_text()

    b = Box()
    b.tool("ss", "exit 1")
    b.tool("sshd", "exit 1")
    return b


# ---- it picks the backend the host actually uses ---------------------------
def test_a_firewalld_host_is_driven_with_firewall_cmd(box):
    box.firewalld_host()
    p = box.run(fw.cmd_fw_allow_port("443", "tcp"))
    assert p.returncode == 0, p.stderr
    assert "firewall-cmd --permanent --add-port=443/tcp" in box.ran
    assert "ufw" not in box.ran


def test_a_ufw_host_is_driven_with_ufw(box):
    box.ufw_host()
    p = box.run(fw.cmd_fw_allow_port("443", "tcp"))
    assert p.returncode == 0, p.stderr
    assert "ufw allow 443/tcp" in box.ran
    assert "firewall-cmd" not in box.ran


def test_a_host_running_neither_is_judged_on_what_is_enabled(box):
    """Stopped is not the same as absent. A firewalld host whose daemon is down
    is still a firewalld host, and a rule must not be written into ufw."""
    box.firewalld_host(running=False)
    p = box.run(fw.cmd_fw_allow_port("443", "tcp"))
    assert "firewall-cmd --permanent --add-port=443/tcp" in box.ran, p.stdout + p.stderr


def test_a_host_with_no_firewall_says_so_and_installs_nothing(box):
    box.bare_host()
    p = box.run(fw.cmd_fw_allow_port("443", "tcp"))
    assert p.returncode != 0
    assert "No firewall is installed" in p.stderr
    assert "firewalld on RHEL" in p.stderr, "it does not say which to install"


# ---- the port range, written either way ------------------------------------
def test_a_range_is_translated_per_backend(box):
    """firewalld writes 8000-9000 and ufw writes 8000:9000. The operator should
    not have to know that, and should not be made to type it twice."""
    for written in ("8000-9000", "8000:9000"):
        box.firewalld_host()
        box.run(fw.cmd_fw_allow_port(written, "tcp"))
        assert "--add-port=8000-9000/tcp" in box.ran, written

    for written in ("8000-9000", "8000:9000"):
        box.ufw_host()
        box.run(fw.cmd_fw_allow_port(written, "tcp"))
        assert "ufw allow 8000:9000/tcp" in box.ran, written


@pytest.mark.parametrize("bad", ["", "0", "70000", "9000-8000", "http", "80;rm"])
def test_a_bad_port_is_refused_before_anything_runs(bad):
    with pytest.raises(ValueError):
        fw.cmd_fw_allow_port(bad, "tcp")


@pytest.mark.parametrize("bad", ["", "icmp", "tcp;id"])
def test_a_bad_protocol_is_refused(bad):
    with pytest.raises(ValueError):
        fw.cmd_fw_allow_port("443", bad)


# ---- turning it on never introduces the other one --------------------------
def test_turning_it_on_uses_the_hosts_own_firewall(box):
    """The failure this whole tab exists to prevent: a ufw host must not have
    firewalld started on it, and vice versa."""
    box.ufw_host(active=False)
    p = box.run(fw.cmd_fw_turn_on())
    assert p.returncode == 0, p.stderr
    assert "ufw --force enable" in box.ran
    assert "firewall-cmd" not in box.ran


def test_turning_it_on_keeps_the_management_ports_open(box):
    """Same protection as the backend-specific enable: it must not cut the
    connection it is pressed over."""
    cmd = fw.cmd_fw_turn_on()
    assert "_sysible_keep_open" in cmd
    assert cmd.index("_sysible_keep_open() {") < cmd.index("ufw --force enable")


def test_turning_it_off_is_backend_correct(box):
    box.ufw_host()
    p = box.run(fw.cmd_fw_turn_off())
    assert p.returncode == 0, p.stderr
    assert "ufw disable" in box.ran


def test_turning_off_a_host_with_no_firewall_is_not_an_error(box):
    """Nothing to do is not a failure, and must not show the fleet a red row."""
    box.bare_host()
    p = box.run(fw.cmd_fw_turn_off())
    assert p.returncode == 0, p.stderr
    assert "nothing to turn off" in p.stdout


# ---- and it is the tab you land on -----------------------------------------
def test_the_agnostic_tab_comes_first():
    from webgui import actions
    tool = next(t for t in actions.catalog() if t["tool"] == "Firewall Administration")
    tabs = list(dict.fromkeys(a["tab"] for a in tool["actions"]))
    assert tabs[0] == "Firewall", tabs


def test_closing_a_port_asks_first():
    from webgui import actions
    assert actions.get("fw_any_close").danger is True
    assert actions.get("fw_any_off").danger is True
    assert actions.get("fw_any_allow").danger is False
