"""A firewall that failed to start must not be reported as "no firewall".

From "why do random hosts keep turning off their firewall?" — asked about a host
whose journal showed nothing of the sort. firewalld was enabled, systemd had
dropped its start job to break an ordering cycle involving cloud-init, and the
service simply never came up.

The posture probe asked one question: is a firewall RUNNING. So that host
reported backend=none, active=0 — byte for byte what a host nobody ever
configured reports. The console then offered the finding for the wrong problem:
"No host firewall is active ... Enable ufw/firewalld", which is a button that
cannot fix a service that failed to start, and which reads as though somebody had
switched the firewall off.

The probe now also reports what is enabled AT BOOT, which separates the two.
"""
import subprocess

import pytest

import client.api  # noqa: F401 - initialise the client package / import order
import client._api_dispatch as D


@pytest.fixture()
def host(tmp_path):
    """A sandboxed PATH where the test decides what systemctl and the firewall
    tools say, so the probe runs for real against a host of its choosing."""
    bindir = tmp_path / "bin"
    bindir.mkdir()

    class Host:
        def tool(self, name, body):
            f = bindir / name
            f.write_text("#!/bin/sh\n" + body + "\n")
            f.chmod(0o755)

        def probe_firewall(self):
            """Run JUST the firewall stanza of the posture script."""
            src = D.cmd_host_posture() if hasattr(D, "cmd_host_posture") else _posture_src()
            start = src.index("# --- Firewall")
            end = src.index("# --- Users / accounts")
            snippet = "p() { printf '%s=%s\\n' \"$1\" \"$2\"; }\n" + src[start:end]
            r = subprocess.run(["/bin/bash", "-c", snippet], capture_output=True,
                               text=True, env={"PATH": str(bindir)}, timeout=30)
            return dict(l.split("=", 1) for l in r.stdout.strip().splitlines() if "=" in l)

    return Host()


def _posture_src():
    import inspect
    return inspect.getsource(D)


# ---- the host this came from ----------------------------------------------
def test_firewalld_enabled_but_dead_is_distinguishable(host):
    """ubuntu-02: firewalld enabled at boot, not running, ufw inactive."""
    host.tool("firewall-cmd", "exit 1")            # --state fails: not running
    host.tool("ufw", 'echo "Status: inactive"; exit 0')
    host.tool("systemctl", 'case "$2" in firewalld) exit 0;; esac; exit 1')

    got = host.probe_firewall()

    assert got["fw.active"] == "0"
    assert got["fw.backend"] == "none"
    assert got["fw.enabled_at_boot"] == "firewalld", \
        "indistinguishable from a host with no firewall at all"


def test_a_host_with_no_firewall_at_all_still_reads_as_none(host):
    """The other side of the same coin — this one really is 'nobody set one up',
    and must not start claiming a firewall is configured."""
    host.tool("systemctl", "exit 1")               # nothing enabled

    got = host.probe_firewall()

    assert got["fw.active"] == "0"
    assert got["fw.enabled_at_boot"] == "none"


def test_a_healthy_host_reports_both(host):
    host.tool("firewall-cmd", "exit 0")            # --state succeeds: running
    host.tool("systemctl", 'case "$2" in firewalld) exit 0;; esac; exit 1')

    got = host.probe_firewall()

    assert got["fw.active"] == "1"
    assert got["fw.backend"] == "firewalld"
    assert got["fw.enabled_at_boot"] == "firewalld"


def test_ufw_is_recognised_as_enabled_at_boot(host):
    host.tool("ufw", 'echo "Status: inactive"; exit 0')
    host.tool("systemctl", 'case "$2" in ufw) exit 0;; esac; exit 1')

    got = host.probe_firewall()

    assert got["fw.enabled_at_boot"] == "ufw"
    assert got["fw.active"] == "0"


def test_the_probe_survives_a_host_with_no_systemctl(host):
    """Not every managed host runs systemd. Reporting 'none' is right; failing
    the whole posture sweep over it is not."""
    host.tool("ufw", 'echo "Status: inactive"; exit 0')

    got = host.probe_firewall()

    assert got["fw.enabled_at_boot"] == "none"
    assert "fw.active" in got, "the rest of the firewall stanza stopped reporting"


# ---- the console must act on the distinction -------------------------------
def test_the_console_has_a_separate_explanation():
    """A finding that says 'enable a firewall' is the wrong instruction for a
    firewall that is already enabled."""
    from pathlib import Path
    src = Path("webgui/frontend/src/views/HostDetail.jsx").read_text(encoding="utf-8")
    assert '"fw.failed_to_start"' in src
    assert "fw.enabled_at_boot" in src, "the console never reads the new key"
    # and it must not tell that operator to press Enable
    i = src.index('"fw.failed_to_start": {')
    block = src[i:i + 700]
    assert "will not help" in block, "it still offers the useless remedy"
    assert "journalctl" in block, "it does not say how to find the real cause"
