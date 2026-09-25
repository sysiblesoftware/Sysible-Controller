"""zypper's informational exit codes are not failures.

Reported from an openSUSE host: "failed · exit 103", above a log showing the
transaction completing — packages retrieved, installed, post-transaction scripts
run — and ending with zypper's own words: "Run this command once more to install
any other needed patches."

103 is ZYPPER_EXIT_INF_RESTART_NEEDED. zypper reserves 100-107 for INFORMATION;
its errors are 1-99. The console judges a host by `code == 0`, which is right for
apt, dnf and pacman and wrong for zypper, so a successful patch run was reported
as a failed one — and, worse, the second pass zypper asks for never happened, so
the remaining patches silently went unapplied.

These tests run the GENERATED SHELL against a stub zypper that returns the codes
a real one does. What matters is not the text of the command but what the console
ends up recording, so that is what they assert.
"""
import subprocess
import textwrap
from pathlib import Path

import pytest

from client._pkgmgr import ZYPPER_INFO_SUCCESS, zypper_transaction


@pytest.fixture()
def run(tmp_path):
    """Run a built command with a stub `zypper` that exits with the given codes,
    one per invocation. Returns (exit_code, output, number_of_zypper_runs)."""
    calls = tmp_path / "calls"
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    (stub_dir / "zypper").write_text(textwrap.dedent(f"""\
        #!/bin/sh
        n=$(cat {calls} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {calls}
        echo "zypper $* (run #$n)"
        code=$(echo "$CODES" | cut -d' ' -f$n)
        [ -z "$code" ] && code=$(echo "$CODES" | awk '{{print $NF}}')
        exit "$code"
        """))
    (stub_dir / "zypper").chmod(0o755)
    # The per-package status readout the console appends uses rpm.
    (stub_dir / "rpm").write_text('#!/bin/sh\n[ "$1" = "-q" ] && { echo "1.2.3-1"; exit 0; }\nexit 1\n')
    (stub_dir / "rpm").chmod(0o755)

    def _run(script, codes):
        calls.write_text("0")
        r = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                           env={"PATH": f"{stub_dir}:/usr/bin:/bin", "CODES": codes},
                           timeout=60)
        return r.returncode, r.stdout + r.stderr, int(calls.read_text().strip())
    return _run


CMD = zypper_transaction('zypper --non-interactive update', rerun_on_restart=True)


# ---- the reported bug ------------------------------------------------------
def test_103_is_a_success_and_zypper_is_run_again(run):
    """The exact report. 103 means the transaction worked and zypper replaced
    itself; the second pass is what applies the remaining patches."""
    rc, out, runs = run(CMD, "103 0")
    assert rc == 0, f"a completed patch run was reported as failed (exit {rc})"
    assert runs == 2, "zypper asked to be run once more and never was"
    assert "re-running once" in out


def test_103_twice_is_still_a_success_and_does_not_loop(run):
    """A second 103 is legitimate; it must not become an unbounded retry."""
    rc, _, runs = run(CMD, "103 103")
    assert rc == 0
    assert runs == 2, "the re-run must happen exactly once"


def test_102_reboot_needed_is_a_success(run):
    """Installed; the host wants a reboot. The console has a reboot column for
    precisely this — it is not a failed update."""
    rc, _, runs = run(CMD, "102")
    assert rc == 0 and runs == 1


@pytest.mark.parametrize("code", [100, 101])
def test_the_other_informational_codes_are_not_failures(run, code):
    rc, _, _ = run(CMD, str(code))
    assert rc == 0


# ---- ...and a real failure is still a failure ------------------------------
@pytest.mark.parametrize("code,why", [
    (1, "an ordinary zypper error"),
    (104, "capability not found — the requested thing is not there"),
    (105, "killed by a signal"),
    (106, "a repository was SKIPPED, so patches may never have been considered"),
    (107, "an rpm scriptlet failed"),
])
def test_real_failures_still_fail(run, code, why):
    rc, _, _ = run(CMD, str(code))
    assert rc == code, f"exit {code} ({why}) was swallowed as success"


def test_only_the_informational_codes_are_translated():
    assert set(ZYPPER_INFO_SUCCESS) == {0, 100, 101, 102, 103}, \
        "104-107 are real problems; translating them would hide them"


# ---- it must not swallow what callers append -------------------------------
def test_the_trailing_status_readout_still_runs(run):
    """webgui/actions._pkg_and_status appends a per-package status query after
    the op. An `exit` in the wrapper would silently drop it."""
    import webgui.actions as actions
    script = actions.get("pkg_update").build({"names": "libzypp", "flags": ""})

    rc, out, runs = run(script, "103 0")

    assert rc == 0
    assert runs == 2
    assert "SYSIBLE_PKG libzypp installed" in out, \
        "the per-package status readout was swallowed by the exit-code wrapper"


def test_a_failure_still_reaches_the_console_through_that_readout(run):
    import webgui.actions as actions
    script = actions.get("pkg_update").build({"names": "libzypp", "flags": ""})

    rc, out, _ = run(script, "104")

    assert rc == 104, "the op's failure was masked by the trailing status query"
    assert "SYSIBLE_PKG libzypp installed" in out


def test_security_updates_get_the_same_treatment(run):
    """`zypper patch` returns 103 for the same reason, and the security path is
    where it matters most — an unapplied patch reported as a failure is one thing,
    an unapplied patch nobody re-ran is another."""
    from client._api_security import cmd_install_security_updates
    script = cmd_install_security_updates()
    assert "_zrc" in script, "the security-update path does not translate zypper's exit codes"
    # Drive the zypper branch directly: the built script picks a manager by what
    # is on PATH, and the stub dir has only zypper.
    rc, out, runs = run(script, "103 0")
    assert rc == 0 and runs == 2
