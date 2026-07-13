"""
openSUSE / SLES compatibility regression tests for the command builders.

Several builders were written assuming Debian (apt, `sudo` group, pam-auth-update)
or RHEL (`kernel`/`kernel-core` packages, authselect). On SUSE those assumptions
turn into silent no-ops: the command runs, exits 0, but does nothing useful. These
tests pin the SUSE-correct behavior so it can't regress.
"""
import re
import subprocess
from pathlib import Path

import pytest

import client.api as api

REPO_ROOT = Path(__file__).resolve().parent.parent


def _bash_syntax_ok(cmd: str) -> bool:
    return subprocess.run(["bash", "-n"], input=cmd, text=True,
                          capture_output=True).returncode == 0


# ---------------------------------------------------------------------------
# Sudo policy: group must be detected at runtime, not hard-coded to Debian 'sudo'
# ---------------------------------------------------------------------------
def test_sudo_policy_detects_group_when_unspecified():
    cmd = api.cmd_set_sudo_policy(require_password=True)
    # Runtime detection present (apt => sudo, else wheel) — so SUSE gets %wheel.
    assert "grp=sudo" in cmd and "grp=wheel" in cmd
    # The rule line is built from the detected $grp, never a literal "%sudo".
    assert '"%$grp ALL=(ALL:ALL) ALL"' in cmd
    assert "%sudo " not in cmd
    # Existence gate so an ineffective policy isn't written to a missing group.
    assert 'getent group "$grp"' in cmd
    assert _bash_syntax_ok(cmd)


def test_sudo_policy_honors_explicit_group():
    cmd = api.cmd_set_sudo_policy(require_password=False, group="wheel")
    assert "grp=wheel" in cmd
    assert "NOPASSWD" in cmd
    assert _bash_syntax_ok(cmd)


def test_sudo_policy_timeout_only_needs_no_group():
    cmd = api.cmd_set_sudo_policy(timestamp_timeout=15)
    assert "timestamp_timeout=15" in cmd
    assert "grp=" not in cmd            # no group rule => no detection needed
    assert _bash_syntax_ok(cmd)


def test_sudo_policy_rejects_bad_group():
    with pytest.raises(ValueError):
        api.cmd_set_sudo_policy(require_password=True, group="bad;rm -rf /")


# ---------------------------------------------------------------------------
# Kernel listing: must not query RHEL-only package names (empty on SUSE)
# ---------------------------------------------------------------------------
def test_list_kernels_enumerates_generically():
    cmd = api.cmd_list_kernels()
    # RHEL-only `rpm -q kernel kernel-core` returns nothing on SUSE
    # (kernel-default et al.) — must enumerate the whole kernel* family instead.
    assert "rpm -q kernel kernel-core" not in cmd
    assert "rpm -qa 'kernel*'" in cmd
    assert _bash_syntax_ok(cmd)


# ---------------------------------------------------------------------------
# mkhomedir: SUSE uses pam-config (no authselect, no pam-auth-update)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cmd", [
    api.cmd_enable_mkhomedir(),
    api.cmd_prepare_ad_join("corp.example.com"),
])
def test_mkhomedir_has_suse_pam_config_branch(cmd):
    assert "command -v pam-config" in cmd
    assert "pam-config --add --mkhomedir" in cmd
    # The RHEL/Debian branches are still present (cross-distro).
    assert "authselect" in cmd and "pam-auth-update" in cmd
    assert _bash_syntax_ok(cmd)


# ---------------------------------------------------------------------------
# Installer: zypper branch must not carry the apt/dnf `-y` flag
# ---------------------------------------------------------------------------
def test_installer_zypper_branch_has_no_dash_y():
    text = (REPO_ROOT / "install_sysible.sh").read_text()
    # Every zypper invocation relies on --non-interactive; a stray `install -y`
    # breaks under `set -e` on older zypper (pre-1.14).
    assert not re.search(r"zypper[^\n]*install -y", text), \
        "zypper 'install -y' found — use --non-interactive (drop -y)"
    assert "zypper --non-interactive install" in text


# ---------------------------------------------------------------------------
# Python 3.6 safety: code that runs ON MANAGED HOSTS (the agent, and the
# embedded scripts the controller ships to hosts via `python3 -c`) must not use
# subprocess.run kwargs added in 3.7 — `capture_output=` and `text=`. openSUSE
# Leap / SLES 15 ship Python 3.6, where those raise
# "__init__() got an unexpected keyword argument 'capture_output'". Use
# stdout=PIPE, stderr=PIPE, universal_newlines=True instead (works on 3.6+).
# ---------------------------------------------------------------------------
def test_embedded_user_sync_script_is_py36_safe():
    import client._api_users as u
    script = u._SYNC_USERS_SCRIPT
    assert "capture_output" not in script, \
        "_SYNC_USERS_SCRIPT runs on the host's python3 (3.6 on SUSE) — no capture_output="
    assert "text=True" not in script, \
        "_SYNC_USERS_SCRIPT runs on the host's python3 (3.6 on SUSE) — no text=True"
    compile(script, "<sync_users>", "exec")   # still valid Python


@pytest.mark.parametrize("rel", ["host_agent/agent.py", "client/_api_users.py"])
def test_host_side_code_has_no_py37_only_subprocess_kwargs(rel):
    src = (REPO_ROOT / rel).read_text()
    assert "capture_output=" not in src, \
        f"{rel} runs on managed hosts (SUSE python 3.6) — replace capture_output= with stdout/stderr=PIPE"
    assert "text=True" not in src, \
        f"{rel} runs on managed hosts (SUSE python 3.6) — replace text=True with universal_newlines=True"
