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
import client._api_process_service as P
import client._api_timesync as T
import client._api_directory as Dir
import client._api_automation as A

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


# === 2nd audit, deferred batch: more cross-distro correctness =================

# --- generic service builders resolve ssh/sshd, cron/crond, chrony/chronyd ----

@pytest.mark.parametrize("verb_cmd", [
    P.cmd_service_start("sshd"), P.cmd_service_restart("cron"),
    P.cmd_service_enable("chronyd"),
])
def test_service_builders_resolve_unit_aliases(verb_cmd):
    assert "list-unit-files --no-legend" in verb_cmd
    assert "sshd.service" in verb_cmd and "ssh.service" in verb_cmd
    assert "crond.service" in verb_cmd and "cron.service" in verb_cmd
    assert "chronyd.service" in verb_cmd and "chrony.service" in verb_cmd
    _bash_n(verb_cmd)


def test_service_alias_resolves_sshd_to_ssh_on_debian():
    """On a Debian-like host where only ssh.service exists, a 'sshd' request runs ssh."""
    cmd = P.cmd_service_restart("sshd")
    stub = (
        'systemctl(){ case "$1 $2 $3" in '
        '"list-unit-files --no-legend sshd.service") return 0;; '
        '"list-unit-files --no-legend ssh.service") echo "ssh.service enabled"; return 0;; '
        'is-active*) echo active;; is-enabled*) echo enabled;; *) return 0;; '
        'esac; }; export -f systemctl; '
    )
    r = subprocess.run(["bash", "-c", stub + cmd], capture_output=True, text=True)
    # The builder's own confirmation names the RESOLVED unit ($u), proving sshd->ssh.
    assert "Restarted ssh.service." in r.stdout


# --- chrony: disable DHCP-supplied NTP so the chosen servers are exclusive -----

def test_set_ntp_servers_disables_dhcp_sourcedir():
    cmd = T.cmd_set_ntp_servers("1.1.1.1 8.8.8.8")
    assert "sourcedir" in cmd                       # Debian DHCP NTP source
    assert "server 1.1.1.1 iburst" in cmd
    _bash_n(cmd)


# --- netplan DHCP switch must warn about a static IP left in another file ------

def test_dhcp_warns_about_conflicting_static_netplan():
    cmd = N.cmd_configure_dhcp("ens3")
    # netplan concatenates address lists across files, so a static IP elsewhere
    # survives; the builder must detect and warn rather than silently half-switch.
    assert "/etc/netplan/*.yaml" in cmd
    assert "appends address lists" in cmd
    _bash_n(cmd)


# --- netplan config validates the interface actually exists -------------------

@pytest.mark.parametrize("cmd", [
    N.cmd_configure_static_ip("ens3", "1.2.3.4/24", "1.2.3.1"),
    N.cmd_configure_dhcp("ens3"),
])
def test_netplan_path_checks_iface_exists(cmd):
    assert "/sys/class/net/$IFACE" in cmd or 'ip link show "$IFACE"' in cmd
    _bash_n(cmd)


# --- dnf5 addrepo: bare baseurl needs --set=baseurl, .repo needs --from-repofile

def test_repo_add_dnf5_baseurl_vs_repofile():
    cmd = R.cmd_add_repository("https://repo.example.com/x/", "myrepo")
    assert "--add-repo" in cmd                      # dnf4 form
    assert "--set=baseurl=" in cmd                  # dnf5 bare-baseurl form
    assert "--from-repofile=" in cmd                # dnf5 .repo-file form
    assert "--id=myrepo" in cmd
    _bash_n(cmd)


# --- Fedora system-upgrade: install the plugin matching the active dnf --------

@pytest.mark.parametrize("build", [
    lambda: A.cmd_release_upgrade("41"),
    lambda: A.cmd_release_upgrade_check("41"),
])
def test_fedora_upgrade_plugin_keyed_on_dnf_major(build):
    cmd = build()
    assert "dnf5-plugin-system-upgrade" in cmd
    assert "dnf-plugin-system-upgrade" in cmd
    assert "grep -q '^dnf5'" in cmd
    _bash_n(cmd)


# --- LDAP client wires NSS + PAM (SSSD alone doesn't) -------------------------

def test_ldap_client_wires_nss_and_pam():
    cmd = Dir.cmd_configure_ldap_client("ldap.example.com", "dc=example,dc=com")
    assert "nsswitch.conf" in cmd                    # NSS sss wiring
    assert "grep -qw sss" in cmd
    assert "authselect select sssd" in cmd           # RHEL PAM
    assert "pam-auth-update --enable sss" in cmd     # Debian PAM
    assert "pam-config --add --sss" in cmd           # SUSE PAM
    _bash_n(cmd)


# --- mkfs gives a friendly 'not installed' message per distro -----------------

def test_format_filesystem_guards_missing_mkfs():
    cmd = St.cmd_format_filesystem("/dev/sdb1", "btrfs")
    assert "command -v mkfs.btrfs" in cmd
    assert "btrfs-progs" in cmd                       # package hint
    _bash_n(cmd)


# --- pwquality.conf is inert on default Debian/Ubuntu: warn -------------------

def test_pwquality_warns_on_debian():
    cmd = S.cmd_set_pwquality_option("minlen", "12")
    assert "pam_pwquality" in cmd
    assert "libpam-pwquality" in cmd
    _bash_n(cmd)


# --- keep-N kernels: note the count is dnf-only -------------------------------

def test_remove_old_kernels_notes_keep_is_dnf_only():
    cmd = B.cmd_remove_old_kernels("5")
    assert "APT autoremove" in cmd
    assert "multiversion.kernels" in cmd
    _bash_n(cmd)


# --- Disk usage analyzer: read-only du/find, single-fs, bounded ---------------
def test_disk_usage_analyzer_readonly_bounded_quoted():
    cmd = St.cmd_analyze_disk_usage("/opt/a b", 10)
    _bash_n(cmd)
    # Portable single-filesystem flags (GNU/BusyBox/BSD): du -k -x -d 1, find -xdev.
    assert "du -k -x -d 1" in cmd                  # largest dirs, one filesystem
    assert "--max-depth" not in cmd and "-printf" not in cmd  # no GNU-only flags
    assert "find" in cmd and "-xdev" in cmd       # largest files, one filesystem
    assert "ls -ldn" in cmd                       # portable size listing (not -printf)
    assert "timeout 120" in cmd                   # can't hang the agent
    assert "head -n 10" in cmd                    # bounded output
    assert "'/opt/a b'" in cmd                    # a path with a space is quoted
    for bad in ("rm ", "mkfs", "dd if=", "shred", "truncate", "chmod", "chown", "> /"):
        assert bad not in cmd                     # strictly read-only


def test_disk_usage_analyzer_validates_inputs():
    with pytest.raises(ValueError):
        St.cmd_analyze_disk_usage("bad\npath", 5)     # newline injection rejected
    with pytest.raises(ValueError):
        St.cmd_analyze_disk_usage("/", 9999)          # top must be 1..200
    with pytest.raises(ValueError):
        St.cmd_analyze_disk_usage("-rf /tmp", 5)      # leading-dash path rejected
    assert "_p=/" in St.cmd_analyze_disk_usage("", 20)  # empty path defaults to /


# ===========================================================================
# Functionality + cross-distro sweep regressions (dnf/apt/zypper/pacman)
# ===========================================================================
import client._api_certs as Certs  # noqa: E402


def test_update_named_packages_pacman_is_full_upgrade_not_partial():
    # Arch can't safely update individual packages (partial upgrade); the pacman branch
    # must run a full -Syu, not `pacman -Sy <pkg>`.
    cmd = A.cmd_update_packages("nginx")
    _bash_n(cmd)
    assert "pacman -Syu --noconfirm" in cmd
    assert "pacman -Sy --noconfirm nginx" not in cmd
    assert "pacman -Sy nginx" not in cmd


def test_security_check_updates_pacman_is_read_only():
    # A read-only "check" must never mutate the live sync db with `pacman -Sy`.
    cmd = S.cmd_check_security_updates()
    _bash_n(cmd)
    assert "pacman -Sy >" not in cmd and "pacman -Sy;" not in cmd
    assert "checkupdates" in cmd and "pacman -Qu" in cmd


def test_list_and_remove_kernels_have_pacman_branches():
    lk = B.cmd_list_kernels(); _bash_n(lk)
    assert "pacman -Q" in lk and "linux-lts" in lk
    rk = B.cmd_remove_old_kernels("2"); _bash_n(rk)
    assert "command -v pacman" in rk and "does not retain old kernel" in rk


def test_timer_enable_disable_use_now():
    en = A.cmd_timer_enable("mytimer"); _bash_n(en)
    dis = A.cmd_timer_disable("mytimer"); _bash_n(dis)
    assert "enable --now" in en and "disable --now" in dis


def test_certbot_single_domain_renew_uses_cert_name_not_certonly():
    cmd = Certs.cmd_renew_certbot("example.com"); _bash_n(cmd)
    assert "renew --force-renewal --cert-name" in cmd
    assert "certonly" not in cmd
    assert Certs.cmd_renew_certbot("") .strip().endswith("certbot renew 2>&1")


def test_destructive_storage_creators_refuse_busy_devices():
    for cmd in (St.cmd_create_physical_volume("/dev/sdb"),
                St.cmd_create_raid_array("/dev/md0", "1", "/dev/sdb /dev/sdc"),
                St.cmd_create_swap_partition("/dev/sdb1", False)):
        _bash_n(cmd)
        assert "Refusing" in cmd and "swapon" in cmd


def test_raid_per_level_minimum_members():
    with pytest.raises(ValueError):
        St.cmd_create_raid_array("/dev/md0", "5", "/dev/sdb /dev/sdc")   # RAID5 needs 3
    with pytest.raises(ValueError):
        St.cmd_create_raid_array("/dev/md0", "6", "/dev/sdb /dev/sdc /dev/sdd")  # RAID6 needs 4
    _bash_n(St.cmd_create_raid_array("/dev/md0", "5", "/dev/sdb /dev/sdc /dev/sdd"))


def test_cron_job_rejects_newline_injection():
    with pytest.raises(ValueError):
        A.cmd_add_cron_job("*/5 * * * *", "/bin/foo\n0 0 * * * /bin/evil", "")
    with pytest.raises(ValueError):
        A.cmd_add_cron_job("*/5 * * * *\nMAILTO=x", "/bin/foo", "")


def test_network_diagnostics_reject_leading_dash():
    for fn in (lambda: N.cmd_ping("-flood", 4),
               lambda: N.cmd_traceroute("-x"),
               lambda: N.cmd_dns_lookup("-h")):
        with pytest.raises(ValueError):
            fn()


def test_set_password_keeps_hash_off_argv():
    cmd = U.cmd_set_password("alice", "hunter2"); _bash_n(cmd)
    assert "chpasswd -e" in cmd and "usermod -p" not in cmd
    with pytest.raises(ValueError):
        U.cmd_set_password("-alice", "x")


def test_group_ops_reject_leading_dash():
    for fn in (lambda: U.cmd_create_group("-g"),
               lambda: U.cmd_delete_group("-g"),
               lambda: U.cmd_add_user_to_group("-g", "u"),
               lambda: U.cmd_remove_user_from_group("g", "-u")):
        with pytest.raises(ValueError):
            fn()


def test_nft_and_iptables_persistence_cover_arch():
    nft = FW.cmd_nft_save_persist(); _bash_n(nft)
    assert "nftables.service" in nft and "nft list ruleset" in nft
    ipt = FW.cmd_iptables_save_persist(); _bash_n(ipt)
    assert "/etc/iptables/iptables.rules" in ipt


def test_install_filesystem_tools_all_managers():
    cmd = St.cmd_install_filesystem_tools(); _bash_n(cmd)
    assert "parted e2fsprogs xfsprogs dosfstools" in cmd
    for tok in ("zypper --non-interactive install", "apt-get install -y", "pacman -Sy"):
        assert tok in cmd
