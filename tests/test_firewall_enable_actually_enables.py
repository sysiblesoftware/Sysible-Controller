"""The firewall Enable buttons, reported as "not working in any of the distros".

Two faults, both in the firewalld half, and both of which make the button fail
in a way that sends the operator after the wrong thing:

  * NO INSTALLED-CHECK. Every other firewalld builder refuses early when
    firewall-cmd is absent. The service-state one did not, so on any host without
    firewalld — which is every Debian/Ubuntu and Arch host — pressing Enable ran
    `systemctl enable --now firewalld` and reported systemd's "unit does not
    exist" under a message about sudo.

  * THE MESSAGE LOOKED LIKE A PRIVILEGE ERROR TO OUR OWN AGENT. The agent decides
    whether to retry under sudo by substring-matching the combined output against
    _PRIV_ERROR_HINTS, and the advice printed here quoted "authentication
    required" and "access denied" while explaining them. That advice was printed
    on EVERY non-zero exit, so every failure — not installed, unit masked,
    dependency failed — was classified as a privilege problem, re-run pointlessly
    under sudo, and reported to the operator as a sudo problem.

systemd's and ufw's own wording is printed either way and is what the agent
should be reading. These tests run the real shell against a sandboxed PATH, so
they fail against a command that merely looks right.

A third, quieter one on the ufw side: `systemctl enable ufw` had its output and
exit code thrown away, so a host could come back from a reboot with no firewall
while the console had reported success.
"""
import subprocess

import pytest

from client import api  # noqa: F401 - import FIRST; see test_power_actions_report_back
from client import _api_firewall as fw
from client._api_dispatch import _with_sbin_path
from host_agent.agent import _PRIV_ERROR_HINTS, _looks_like_privilege_error


@pytest.fixture()
def host(tmp_path):
    """A PATH holding only what the test puts in it, so `command -v firewall-cmd`
    answers what this host is meant to have rather than what this machine has."""
    bindir = tmp_path / "bin"
    bindir.mkdir()

    class Host:
        def has(self, name, body="exit 0"):
            f = bindir / name
            f.write_text("#!/bin/sh\n" + body + "\n")
            f.chmod(0o755)

        def run(self, command):
            return subprocess.run(["/bin/bash", "-c", _with_sbin_path(command)],
                                  capture_output=True, text=True,
                                  env={"PATH": str(bindir)}, timeout=30)

    return Host()


POLKIT = 'echo "Failed to enable unit: Interactive authentication required." >&2; exit 1'


# ---- the regression that caused the report ---------------------------------
def test_our_own_advice_is_not_mistaken_for_a_privilege_error():
    """The agent reads the combined output. If the text we print to EXPLAIN a
    privilege error contains the phrases it matches on, every failure looks like
    one — including the ones that are not."""
    for enabled in (True, False):
        for build in (fw.cmd_set_firewalld_enabled, fw.cmd_set_ufw_enabled):
            cmd = build(enabled)
            ours = " ".join(part.split("'")[0] for part in cmd.split("echo '")[1:]).lower()
            hits = [h for h in _PRIV_ERROR_HINTS if h in ours]
            assert not hits, f"{build.__name__}({enabled}) prints {hits} itself"


@pytest.mark.parametrize("build,tool", [(fw.cmd_set_firewalld_enabled, "firewall-cmd"),
                                        (fw.cmd_set_ufw_enabled, "ufw")])
def test_a_host_without_the_firewall_is_told_that_and_nothing_else(host, build, tool):
    """Not installed is not a sudo problem, and must not be reported as one —
    nor retried under sudo, which cannot help."""
    host.has("systemctl", POLKIT)          # would blame polkit if it ever ran

    p = host.run(build(True))

    assert p.returncode != 0
    assert "not installed" in (p.stdout + p.stderr)
    assert "Install" in (p.stdout + p.stderr), "it does not say how to fix it"
    combined = p.stderr + "\n" + p.stdout
    assert not _looks_like_privilege_error(combined), \
        "the agent would retry this under sudo, which cannot install a package"


def test_a_masked_unit_is_not_reported_as_a_sudo_problem(host):
    """Any non-privilege failure of the unit. systemd says what is wrong; we must
    not overwrite that with advice about sudo."""
    host.has("firewall-cmd")
    host.has("systemctl", 'echo "Failed to enable unit: Unit firewalld.service is masked." >&2; exit 1')

    p = host.run(fw.cmd_set_firewalld_enabled(True))

    assert p.returncode != 0
    assert "is masked" in (p.stdout + p.stderr), "systemd's reason was lost"
    assert not _looks_like_privilege_error(p.stderr + "\n" + p.stdout)


# ---- the case escalation exists for ----------------------------------------
def test_a_real_privilege_refusal_still_makes_the_agent_escalate(host):
    """The whole point of propagating rc: polkit refuses the non-root attempt,
    the agent sees it and re-runs the command under the host's sudo."""
    host.has("firewall-cmd")
    host.has("systemctl", POLKIT)

    p = host.run(fw.cmd_set_firewalld_enabled(True))

    assert p.returncode != 0
    assert "Interactive authentication required" in (p.stdout + p.stderr)
    assert _looks_like_privilege_error(p.stderr + "\n" + p.stdout), \
        "the agent would not escalate, so the firewall is never enabled"


@pytest.mark.parametrize("refusal", [
    'echo "ERROR: You need to be root to run this script" >&2; exit 1',   # ufw's own
    'echo "ERROR: Permission denied" >&2; exit 1',
])
def test_ufw_refusing_a_non_root_caller_makes_the_agent_escalate(host, refusal):
    host.has("ufw", refusal)
    host.has("systemctl")

    p = host.run(fw.cmd_set_ufw_enabled(True))

    assert p.returncode != 0
    assert _looks_like_privilege_error(p.stderr + "\n" + p.stdout)


# ---- enabling, when it can actually work -----------------------------------
def test_firewalld_enable_starts_it_and_says_so(host, tmp_path):
    host.has("firewall-cmd")
    host.has("systemctl", f'''
case "$1" in
  enable) printf '%s\\n' "$@" > {tmp_path}/argv; : > {tmp_path}/running; exit 0 ;;
  is-active) [ -f {tmp_path}/running ] && exit 0 || exit 3 ;;
esac
exit 0''')

    p = host.run(fw.cmd_set_firewalld_enabled(True))

    assert p.returncode == 0, p.stderr
    assert "enabled and started" in p.stdout
    argv = (tmp_path / "argv").read_text().split()
    assert "--now" in argv, "it was enabled for boot but not started now"


def test_ufw_enable_is_noninteractive_and_persists(host, tmp_path):
    """--force so it never hangs on ufw's 'this may disrupt ssh' prompt, and the
    unit enabled so it survives a reboot."""
    # APPEND: the builder calls ufw twice (enable, then status), and overwriting
    # here recorded only the status call — the assertion below then failed for a
    # reason that had nothing to do with the command under test.
    host.has("ufw", f'printf "%s\\n" "$@" >> {tmp_path}/ufwargv; echo "Firewall is active"; exit 0')
    host.has("systemctl", f'printf "%s\\n" "$@" > {tmp_path}/scargv; exit 0')

    p = host.run(fw.cmd_set_ufw_enabled(True))

    assert p.returncode == 0, p.stderr
    assert "--force" in (tmp_path / "ufwargv").read_text()
    assert "enable" in (tmp_path / "scargv").read_text()


def test_ufw_that_will_not_survive_a_reboot_says_so(host):
    """The boot half used to be >/dev/null 2>&1: ufw came up, the console said
    success, and the host came back from a restart with no firewall."""
    host.has("ufw", 'echo "Firewall is active and enabled on system startup"; exit 0')
    host.has("systemctl", POLKIT)

    p = host.run(fw.cmd_set_ufw_enabled(True))

    assert p.returncode != 0, "a half-done enable was reported as success"
    assert "NOT set to start at boot" in p.stderr
    assert _looks_like_privilege_error(p.stderr + "\n" + p.stdout), \
        "the agent cannot see that the boot half needs elevating"
