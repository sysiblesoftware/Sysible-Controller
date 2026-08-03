"""Regression: Arch Linux (pacman) support across the command builders.

Arch uses systemd, so the service/boot/initramfs paths already worked once the
systemic sbin-PATH fix landed; this covers the package-manager surface — the
central _pkgmgr dispatch plus every install / package-op builder — so an Arch
host no longer falls through to "no supported package manager".
"""
import subprocess

import pytest

import client.api  # noqa: F401
import client._pkgmgr as P
import client._api_automation as A
import client._api_dispatch as D
import client._api_security as S
import client._api_storage as St
import client._api_repo as R
import client._api_timesync as T
import client._api_certs as C
import client._api_directory as Dir
import client._api_firewall as F


def _bash_n(cmd):
    r = subprocess.run(["bash", "-n"], input=cmd, text=True, capture_output=True)
    assert r.returncode == 0, f"invalid shell:\n{r.stderr}\n---\n{cmd}"


def test_pkgmgr_detects_pacman():
    assert "pacman" in P.pkgmgr_detect_fragment()


def test_pkgmgr_dispatch_routes_pacman():
    out = P.pkgmgr_dispatch("RPM", "ZYP", "APT", "PACMANCMD")
    assert '"$PKGMGR" = "pacman"' in out and "PACMANCMD" in out


def test_pkgmgr_dispatch_default_message_when_no_pacman_arm():
    out = P.pkgmgr_dispatch("RPM", "ZYP", "APT")
    assert "not supported on Arch" in out
    _bash_n(out)


# Every package-manager-dependent builder must (a) reference pacman and
# (b) still be valid shell.
PACMAN_BUILDERS = {
    "install_packages": A.cmd_install_packages("nginx vim"),
    "remove_packages": A.cmd_remove_packages("nginx"),
    "update_all": A.cmd_update_packages(),
    "update_named": A.cmd_update_packages("nginx"),
    "package_status": A.cmd_package_status("nginx"),
    "query_package": A.cmd_query_package("nginx"),
    "verify_package": A.cmd_verify_package("nginx"),
    "clean_cache": A.cmd_clean_package_cache(),
    "search_packages": A.cmd_search_packages("web"),
    "list_installed": A.cmd_list_installed_packages(),
    "update_status": D.cmd_update_status(True),
    "clean_cache_dispatch": D.cmd_clean_package_cache(),
    "install_auditd": D.cmd_install_auditd(),
    "install_sos": D.cmd_install_sos(),
    "install_selinux_tools": S.cmd_install_selinux_tools(),
    "install_lynis": S.cmd_install_lynis(),
    "install_rkhunter": S.cmd_install_rkhunter(),
    "check_security_updates": S.cmd_check_security_updates(),
    "install_security_updates": S.cmd_install_security_updates(),
    "install_smartmontools": St.cmd_install_smartmontools(),
    "install_lvm_tools": St.cmd_install_lvm_tools(),
    "install_mdadm": St.cmd_install_mdadm(),
    "list_repositories": R.cmd_list_repositories(),
    "install_chrony": T.cmd_install_chrony(),
    "install_ntp": T.cmd_install_ntp(),
    "install_certbot": C.cmd_install_certbot(),
    "install_ad_dependencies": Dir.cmd_install_ad_dependencies(),
    "install_firewalld": F.cmd_install_firewalld(),
    "install_ufw": F.cmd_install_ufw(),
}


@pytest.mark.parametrize("cmd", list(PACMAN_BUILDERS.values()), ids=list(PACMAN_BUILDERS))
def test_builder_has_pacman_arm_and_valid_shell(cmd):
    assert "pacman" in cmd
    _bash_n(cmd)


def test_install_uses_needed_and_noconfirm():
    # The idempotent, non-interactive install form must be used.
    cmd = A.cmd_install_packages("nginx")
    assert "pacman -Sy --needed --noconfirm" in cmd


def test_update_all_is_full_syu():
    assert "pacman -Syu --noconfirm" in A.cmd_update_packages()


def test_repo_write_actions_point_to_pacman_conf():
    # Repos live in /etc/pacman.conf; write actions must not blindly rewrite it.
    for cmd in (R.cmd_add_repository("https://e/x.repo", "r"),
                R.cmd_enable_repository("r"),
                R.cmd_disable_repository("r")):
        assert "pacman.conf" in cmd
        _bash_n(cmd)
