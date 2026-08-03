"""Regression: cross-distro / cross-version command-generation correctness.

Covers the fixes from the cross-distro audit:
  * systemic sbin PATH prefix applied at the dispatch choke point
    (_api_dispatch._with_sbin_path) so admin tools in /sbin|/usr/sbin resolve on
    non usr-merged distros (openSUSE/SLES) instead of "command not found";
  * SSH auth directives also neutralize overriding sshd_config.d drop-ins
    (modern Debian/Ubuntu/cloud images) so disabling password/root login isn't
    silently overridden;
  * swap files written with dd, not fallocate (fallocate holes break swapon on
    XFS/btrfs = RHEL/Rocky/Fedora/openSUSE defaults);
  * journalctl uses the resolved SSH unit (ssh.service vs sshd.service);
  * fstab add matches the mount point by field, not substring;
  * mdadm --create answers its signature prompt non-interactively;
  * update-status has a yum branch (RHEL7 / Amazon Linux 2);
  * repo add/enable/disable emit dnf5-compatible config-manager forms.
"""
import subprocess

import pytest

import client.api  # noqa: F401  (initialise the client package / resolve import order)
import client._api_dispatch as D
import client._api_security as S
import client._api_storage as St
import client._api_filesystem_mount as M
import client._api_repo as R
import client._api_network as N
import client._api_firewall as FW
import client._api_users as U
import client._api_boot as B
import client._api_backup as Bk
import client._api_subscriptions as Sub

_PREFIX = 'export PATH="/usr/local/sbin:/usr/sbin:/sbin:$PATH"; '


def _bash_n(cmd):
    r = subprocess.run(["bash", "-n"], input=cmd, text=True, capture_output=True)
    assert r.returncode == 0, f"invalid shell:\n{r.stderr}\n---\n{cmd}"


# --- systemic sbin PATH -------------------------------------------------------

def test_with_sbin_path_prefixes():
    assert D._with_sbin_path("useradd -m x").startswith(_PREFIX)


def test_with_sbin_path_idempotent():
    once = D._with_sbin_path("iptables -L")
    assert D._with_sbin_path(once) == once


def test_with_sbin_path_empty_passthrough():
    assert D._with_sbin_path("") == ""


# --- SSH drop-in override -----------------------------------------------------

@pytest.mark.parametrize("cmd", [
    S.cmd_set_password_auth(False),
    S.cmd_set_root_login_mode("no"),
    S.cmd_set_pubkey_auth(True),
])
def test_sshd_option_neutralizes_dropins(cmd):
    assert "sshd_config.d" in cmd            # strips overriding drop-ins
    assert "/Id" in cmd                       # case-insensitive keyword strip
    assert '"$SSHDBIN" -t' in cmd             # still validates before applying
    _bash_n(cmd)


# --- swap files use dd, not fallocate ----------------------------------------

@pytest.mark.parametrize("cmd", [
    St.cmd_create_swap_file("/swapfile", 1024, True),
    St.cmd_resize_swap_file("/swapfile", 2048, True),
])
def test_swap_uses_dd_not_fallocate(cmd):
    assert "dd if=/dev/zero" in cmd
    assert "fallocate" not in cmd
    _bash_n(cmd)


# --- journalctl uses the resolved unit ---------------------------------------

@pytest.mark.parametrize("cmd", [
    S.cmd_failed_login_summary(20),
    S.cmd_list_failed_logins(50),
])
def test_journalctl_uses_resolved_ssh_unit(cmd):
    assert 'journalctl -u "$SSHSVC"' in cmd
    assert "journalctl -u sshd " not in cmd
    _bash_n(cmd)


# --- fstab add matches by field ----------------------------------------------

def test_fstab_add_field_match():
    cmd = M.cmd_add_fstab_entry("/dev/sdb1", "/data", "ext4")
    assert "$2==m" in cmd
    assert "grep -qF" not in cmd
    _bash_n(cmd)


# --- mdadm non-interactive ----------------------------------------------------

def test_mdadm_create_non_interactive():
    cmd = St.cmd_create_raid_array("/dev/md0", "1", "/dev/sdb /dev/sdc")
    assert "yes 2>/dev/null | mdadm --create" in cmd
    _bash_n(cmd)


# --- update-status yum branch -------------------------------------------------

@pytest.mark.parametrize("refresh", [False, True])
def test_update_status_has_yum_branch(refresh):
    cmd = D.cmd_update_status(refresh=refresh)
    assert "mgr=yum" in cmd
    assert "command -v yum" in cmd
    _bash_n(cmd)


# --- dnf5 config-manager ------------------------------------------------------

def test_repo_add_dnf5_form():
    cmd = R.cmd_add_repository("https://example.com/x.repo", "myrepo")
    assert "addrepo --from-repofile=" in cmd   # dnf5 form present
    assert "--add-repo" in cmd                  # dnf4 form still present
    _bash_n(cmd)


@pytest.mark.parametrize("build,flag", [
    (lambda: R.cmd_enable_repository("myrepo"), "enabled=1"),
    (lambda: R.cmd_disable_repository("myrepo"), "enabled=0"),
])
def test_repo_enable_disable_dnf5_form(build, flag):
    cmd = build()
    assert f"setopt myrepo.{flag}" in cmd
    _bash_n(cmd)


# --- netplan network config (Ubuntu Server) -----------------------------------

def test_static_ip_has_nmcli_and_netplan_branches():
    cmd = N.cmd_configure_static_ip("ens3", "192.168.1.50/24", "192.168.1.1", "8.8.8.8 8.8.4.4")
    assert "nmcli connection modify" in cmd            # NM path
    assert "command -v netplan" in cmd                 # netplan path
    assert "/etc/netplan/90-sysible-" in cmd
    assert "netplan generate" in cmd and "netplan apply" in cmd
    _bash_n(cmd)


def test_dhcp_has_netplan_branch():
    cmd = N.cmd_configure_dhcp("ens3")
    assert "dhcp4: true" in cmd
    assert "command -v netplan" in cmd
    _bash_n(cmd)


def test_netplan_yaml_renders_and_parses(tmp_path):
    """Execute the netplan file-writing block in isolation and confirm the YAML
    it produces is well-formed and correct."""
    cmd = N.cmd_configure_static_ip("ens3", "192.168.1.50/24", "192.168.1.1", "1.1.1.1 9.9.9.9")
    block = cmd.split("umask 077; ", 1)[1].split("if netplan generate", 1)[0]
    f = tmp_path / "n.yaml"
    block = block.replace('"$f"', f'"{f}"').replace('"$IFACE"', '"ens3"')
    subprocess.run(["bash", "-c", block], check=True)
    content = f.read_text()
    assert "dhcp4: false" in content
    assert "addresses: [192.168.1.50/24]" in content
    assert "via: 192.168.1.1" in content
    assert "addresses: [1.1.1.1, 9.9.9.9]" in content
    # Explicit default-route CIDR, NOT the `to: default` alias (rejected by the
    # netplan 0.99 shipped on Ubuntu 18.04 / un-SRU'd 20.04).
    assert "to: 0.0.0.0/0" in content
    assert "to: default" not in content
    yaml = pytest.importorskip("yaml")
    eth = yaml.safe_load(content)["network"]["ethernets"]["ens3"]
    assert eth["dhcp4"] is False
    assert eth["routes"] == [{"to": "0.0.0.0/0", "via": "192.168.1.1"}]


@pytest.mark.parametrize("bad", [
    lambda: N.cmd_configure_static_ip("ens3", "not-an-ip"),
    lambda: N.cmd_configure_static_ip("ens3", "1.2.3.4/24", "evil; rm -rf /"),
    lambda: N.cmd_configure_static_ip("ens3", "1.2.3.4/24", "", "8.8.8.8; reboot"),
])
def test_network_validators_reject_injection(bad):
    with pytest.raises(ValueError):
        bad()


# --- firewalld/ufw enable must propagate the real exit code (sudo escalation) --

@pytest.mark.parametrize("cmd,tool", [
    (FW.cmd_set_firewalld_enabled(True), "systemctl enable --now firewalld"),
    (FW.cmd_set_ufw_enabled(True), "ufw --force enable"),
    (FW.cmd_set_ufw_enabled(False), "ufw disable"),
])
def test_service_enable_propagates_exit_code(cmd, tool):
    # The mutating command's exit code must gate the rest, so a polkit refusal
    # (non-root) surfaces as non-zero + a privilege phrase -> the agent escalates
    # to sudo, instead of a trailing status command masking it as exit 0.
    assert "rc=$?" in cmd and 'exit "$rc"' in cmd
    _bash_n(cmd)


def test_firewalld_enable_refusal_is_not_masked():
    """Simulate the enable being refused: the whole command must exit non-zero."""
    cmd = FW.cmd_set_firewalld_enabled(True)
    stub = "sh -c 'echo \"Failed to enable unit: Interactive authentication required.\" >&2; exit 1'"
    sim = cmd.replace("systemctl enable --now firewalld", stub, 1)
    r = subprocess.run(["bash", "-c", sim], capture_output=True, text=True)
    assert r.returncode != 0, "refused enable was masked as success"
    assert "authentication required" in (r.stdout + r.stderr).lower()


# --- openSUSE targetpw fix: user's own sudo password, no NOPASSWD --------------

def test_fix_targetpw_minimal_and_no_nopasswd():
    cmd = S.cmd_allow_user_sudo_password("deploy")
    assert "!targetpw" in cmd
    assert "NOPASSWD" not in cmd            # must NOT weaken the host policy
    assert "visudo -cf" in cmd              # validated before install
    assert "0440" in cmd
    _bash_n(cmd)


@pytest.mark.parametrize("bad", ["root ALL=(ALL)", "a b", "x;y", "-rf", ""])
def test_fix_targetpw_rejects_bad_username(bad):
    with pytest.raises(ValueError):
        S.cmd_allow_user_sudo_password(bad)


# --- create-user same-name group collision (Ubuntu legacy `admin` group) ------

def test_create_user_same_name_group_guard():
    cmd = U.cmd_create_user("admin", "", "/bin/bash")
    assert "getent group admin" in cmd
    assert "useradd -m -N -s" in cmd   # collision path: default group, no join
    assert "useradd -m -s" in cmd      # normal path: private group
    _bash_n(cmd)


# === 2nd cross-distro audit: fixes for silent-wrong / hard-error bugs ==========

# --- fsck is a no-op stub on XFS/btrfs: dispatch to the real repair tool -------

@pytest.mark.parametrize("auto", [True, False])
def test_repair_dispatches_by_fstype_not_bare_fsck(auto):
    cmd = M.cmd_repair_filesystem("/dev/sdb1", auto)
    # A bare `fsck` execs fsck.xfs / fsck.btrfs which exit 0 without repairing.
    assert "xfs_repair" in cmd            # XFS (RHEL/Rocky/Alma default root)
    assert "btrfs check" in cmd           # btrfs (Fedora/openSUSE default root)
    assert "e2fsck" in cmd                # ext*
    assert "blkid -o value -s TYPE" in cmd or "lsblk -dno FSTYPE" in cmd
    _bash_n(cmd)


def test_repair_xfs_runs_xfs_repair(tmp_path):
    """Simulate an XFS device: the real repair tool must run, not a fsck no-op."""
    cmd = M.cmd_repair_filesystem("/dev/sdb1")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "xfs_repair").write_text("#!/bin/sh\necho XFS_REPAIR_RAN\n")
    (bindir / "xfs_repair").chmod(0o755)
    sim = (
        "findmnt(){ return 1; }; blkid(){ echo xfs; }; lsblk(){ echo xfs; }; "
        f'export PATH="{bindir}:$PATH"; ' + cmd
    )
    r = subprocess.run(["bash", "-c", sim], capture_output=True, text=True)
    assert "XFS_REPAIR_RAN" in r.stdout


# --- swap files on btrfs must be NOCOW (chattr +C) or swapon rejects them ------

@pytest.mark.parametrize("cmd", [
    St.cmd_create_swap_file("/swapfile", 1024, True),
    St.cmd_resize_swap_file("/swapfile", 2048, True),
])
def test_swap_btrfs_nocow(cmd):
    assert 'findmnt -no FSTYPE -T' in cmd     # detect the target dir's fs
    assert "chattr +C" in cmd                 # NOCOW, required for btrfs swapfiles
    assert "dd if=/dev/zero" in cmd           # still dd (not fallocate) for XFS
    _bash_n(cmd)


# --- kernel cmdline on RHEL8+/Fedora BLS needs grubby, not just grub.cfg -------

def test_kernel_cmdline_uses_grubby_on_bls():
    cmd = B.cmd_set_kernel_cmdline("quiet nosmt")
    # BLS entries take options from /boot/loader/entries, not a regenerated
    # grub.cfg — grubby applies to existing kernels; grub2-mkconfig alone is a no-op.
    assert "command -v grubby" in cmd
    assert "grubby --update-kernel=ALL --args=" in cmd
    assert "grub2-mkconfig" in cmd or "update-grub" in cmd   # fallback still present
    _bash_n(cmd)


# --- sudo grant: openSUSE ships %wheel commented out, membership is inert ------

def test_set_sudo_installs_validated_group_rule():
    cmd = U.cmd_set_sudo("deploy", True)
    assert "visudo -cf" in cmd                 # validated before install
    assert "/etc/sudoers.d/sysible-sudo-group" in cmd
    assert "0440" in cmd
    assert "NOPASSWD" not in cmd               # no extra privilege
    _bash_n(cmd)


def test_set_sudo_rule_is_group_scoped():
    """The installed sudoers line grants the detected group, not a literal group."""
    cmd = U.cmd_set_sudo("deploy", True)
    # printf builds `%<grp> ALL=(ALL:ALL) ALL` from the runtime-detected $grp.
    assert "ALL=(ALL:ALL) ALL" in cmd
    assert '"$grp"' in cmd


# --- account lockout: faillock.conf is inert unless wired into PAM ------------

def test_account_lockout_policy_wires_or_warns():
    cmd = U.cmd_set_account_lockout_policy(5, 900)
    assert "faillock.conf" in cmd
    assert "authselect enable-feature with-faillock" in cmd   # RHEL8+ wiring
    assert "pam_faillock" in cmd                               # warning for the rest
    _bash_n(cmd)


# --- security updates: dnf5 renamed `updateinfo` -> `advisory` ----------------

def test_check_security_updates_dnf5_advisory_branch():
    cmd = S.cmd_check_security_updates()
    assert "dnf advisory --help" in cmd                 # probe for dnf5
    assert "dnf advisory list --security" in cmd        # dnf5 form
    assert "dnf updateinfo list security" in cmd        # dnf4 fallback
    _bash_n(cmd)


# --- scheduled backup: /etc/cron.d is inert without a cron daemon -------------

def test_backup_schedule_ensures_cron_daemon():
    cmd = Bk.cmd_configure_backup_schedule("/etc", "/backups", "0 2 * * *")
    assert "/etc/cron.d/sysible-backup" in cmd
    # Must ensure a cron daemon exists+runs (crond on RHEL/SUSE, cron on Debian).
    assert "command -v crond" in cmd and "command -v cron" in cmd
    assert "systemctl enable --now crond" in cmd or "systemctl enable --now cron" in cmd
    assert "cronie" in cmd                    # installs where absent
    _bash_n(cmd)


# --- Ubuntu Pro: older LTS ships the legacy `ua` CLI, not `pro` ---------------

@pytest.mark.parametrize("cmd", [
    Sub.cmd_pro_status(),
    Sub.cmd_pro_attach("TOKEN"),
    Sub.cmd_pro_refresh(),
])
def test_pro_resolves_ua_or_pro(cmd):
    assert "command -v pro" in cmd and "command -v ua" in cmd   # resolve either
    assert '"$PRO"' in cmd                                      # invoke the resolved CLI
    _bash_n(cmd)


def test_pro_only_ua_present_runs(tmp_path):
    """With only the legacy `ua` on PATH, the pro builder must still run it."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "ua").write_text("#!/bin/sh\necho UA_RAN: \"$@\"\n")
    (bindir / "ua").chmod(0o755)
    cmd = Sub.cmd_pro_status()
    sim = f'export PATH="{bindir}:$PATH"; ' + cmd
    r = subprocess.run(["bash", "-c", sim], capture_output=True, text=True)
    assert "UA_RAN: status --all" in r.stdout


# --- XFS quotas use mount options, not quotacheck/quotaon ---------------------

def test_enable_quotas_has_xfs_branch():
    cmd = M.cmd_enable_quotas("/data")
    assert 'findmnt -no FSTYPE' in cmd
    assert "uquota" in cmd and "gquota" in cmd     # XFS guidance
    assert "quotacheck" in cmd                     # ext path still present
    _bash_n(cmd)
