"""A test that fakes a host's tools must be the only source of those tools.

THE BUG THIS EXISTS FOR. 32 tests in this suite passed on a developer container
and failed the first time they ran on a machine that had ufw installed.

They build a directory of fake tools and run the real command the product would
send. But client/_api_dispatch._with_sbin_path prepends

    export PATH="/usr/local/sbin:/usr/sbin:/sbin:$PATH";

to every command bound for a host — correct on a real host, where admin
binaries live in sbin and a minimal systemd or runuser PATH omits it. In a test
it put the RUNNER'S OWN /usr/sbin/ufw ahead of the fake. Others set
PATH="<sandbox>:/usr/bin:/bin", and firewall-cmd lives in /usr/bin on
Debian/Ubuntu. Others inherited os.environ["PATH"] outright, which defeated the
`drop=("ip",)` idiom entirely: removing the fake left the real one.

So "this host has ufw" drove the runner's real ufw, and "this host has no
firewall at all" found one. Every one of them passed on an image that shipped
none of those tools — the absence of the bug was an accident of the container.

These guards have teeth only on a machine that HAS the tools, which is exactly
where the suite was broken and where it now runs.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from client import api  # noqa: F401 - import FIRST; see test_power_actions_report_back
from client import _api_firewall as fw
from client._api_dispatch import _SBIN_PATH_EXPORT
from conftest import host_command, link_stock_tools

TESTS = Path(__file__).resolve().parent
REAL_FIREWALL_TOOLS = [t for t in ("ufw", "firewall-cmd") if shutil.which(t)]


def test_host_command_has_the_last_word_on_path():
    """The pin must come AFTER the product's own export, or it does nothing."""
    out = host_command("ufw status", "/sandbox/bin")
    assert out.startswith(_SBIN_PATH_EXPORT), (
        "the product's export must still lead — what is sent to a host is not "
        "the thing under test here")
    assert out.index("/sandbox/bin") > out.index("/usr/sbin"), (
        "the sandbox pin must come after the product's export, otherwise "
        "/usr/sbin wins and the runner's own tools answer for the host")


@pytest.mark.skipif(not REAL_FIREWALL_TOOLS,
                    reason="no real firewall tool installed, so nothing could leak")
def test_a_host_with_no_firewall_finds_none_on_a_machine_that_has_one(tmp_path):
    """The regression, stated directly.

    This machine HAS a firewall backend. A host simulated as having none must
    still report none. Before the fix this found the runner's own tool and
    reported it as the host's.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "systemctl").write_text("#!/bin/sh\nexit 1\n")
    (bindir / "systemctl").chmod(0o755)
    link_stock_tools(bindir)

    p = subprocess.run(["/bin/bash", "-c", host_command(fw.cmd_fw_allow_port("443", "tcp"), bindir)],
                       capture_output=True, text=True, env={"PATH": str(bindir)}, timeout=30)
    assert p.returncode != 0, (
        f"a host with no firewall reported success — it found one of this "
        f"machine's own: {REAL_FIREWALL_TOOLS}\n{p.stdout}\n{p.stderr}")
    assert "No firewall is installed" in p.stderr, (
        f"expected the no-firewall message, got:\n{p.stdout}\n{p.stderr}")


@pytest.mark.skipif(not REAL_FIREWALL_TOOLS,
                    reason="no real firewall tool installed, so nothing could leak")
def test_the_fake_answers_not_the_real_tool(tmp_path):
    """And a host simulated as HAVING ufw must drive the fake, not the real one."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "ran"
    (bindir / "ufw").write_text(f'#!/bin/sh\necho "FAKE-UFW $*" >> {log}\necho "Status: active"\nexit 0\n')
    (bindir / "ufw").chmod(0o755)
    (bindir / "systemctl").write_text(
        '#!/bin/sh\ncase "$1" in is-enabled) [ "$3" = ufw ] && exit 0; exit 1;; is-active) exit 1;; esac\nexit 0\n')
    (bindir / "systemctl").chmod(0o755)
    link_stock_tools(bindir)

    subprocess.run(["/bin/bash", "-c", host_command(fw.cmd_fw_allow_port("443", "tcp"), bindir)],
                   capture_output=True, text=True, env={"PATH": str(bindir)}, timeout=30)
    assert log.exists() and "FAKE-UFW" in log.read_text(), (
        "the fake ufw was never called — this machine's real ufw answered for "
        "the simulated host")


# --- and the shape that caused it, caught statically -------------------------
# A sandbox that fakes a firewall tool must not also expose a directory that
# contains a real one. /usr/bin holds firewall-cmd; os.environ["PATH"] holds
# whatever the runner has.
FAKES_A_BACKEND = re.compile(r'"(ufw|firewall-cmd|ip)"')
LEAKY_PATH = re.compile(r'"PATH":\s*[^,}\n]*(os\.environ|/usr/bin)')


def _test_sources():
    for p in sorted(TESTS.glob("test_*.py")):
        if p.name == Path(__file__).name:
            continue
        yield p, p.read_text(encoding="utf-8")


def test_no_sandbox_that_fakes_a_backend_also_exposes_a_real_one():
    bad = []
    for path, src in _test_sources():
        if not FAKES_A_BACKEND.search(src):
            continue
        for n, line in enumerate(src.splitlines(), 1):
            if LEAKY_PATH.search(line):
                bad.append(f"  {path.name}:{n}: {line.strip()[:110]}")
    assert not bad, (
        "these fake ufw/firewall-cmd/ip AND put a directory holding the real "
        "thing on PATH, so the runner's own tools can answer for the simulated "
        "host (use conftest.link_stock_tools instead):\n" + "\n".join(bad))
