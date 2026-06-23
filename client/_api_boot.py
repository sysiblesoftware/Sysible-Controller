"""System Boot & Recovery command builders (dual-host).

Plain POSIX sh, shlex.quote() on interpolated values, explicit messages
for missing tools, and real exit codes for the result banner.
"""
import re
import shlex

_BOOT_TARGETS = {"rescue", "emergency", "multi-user", "graphical"}
# Conservative kernel-cmdline allowlist (params like "quiet splash
# console=ttyS0,115200 nomodeset" - no shell metacharacters).
_CMDLINE_RE = re.compile(r"^[\w\s=,.:/+\-]*$")


def _grub_rebuild_fragment() -> str:
    """Regenerate grub.cfg wherever this distro keeps it."""
    return (
        "if command -v update-grub >/dev/null 2>&1; then update-grub; "
        "elif command -v grub2-mkconfig >/dev/null 2>&1; then "
        "cfg=$(ls /boot/grub2/grub.cfg /boot/efi/EFI/*/grub.cfg 2>/dev/null | head -n1); "
        "[ -z \"$cfg\" ] && cfg=/boot/grub2/grub.cfg; grub2-mkconfig -o \"$cfg\"; "
        "elif command -v grub-mkconfig >/dev/null 2>&1; then grub-mkconfig -o /boot/grub/grub.cfg; "
        "else echo 'No grub config tool found (update-grub/grub2-mkconfig).' >&2; exit 1; fi"
    )


def cmd_analyze_boot_failures() -> str:
    return r"""
echo '== Recent boots =='
if command -v journalctl >/dev/null 2>&1; then journalctl --list-boots --no-pager 2>&1 | tail -n 10; else echo 'journalctl not available.'; fi
echo
echo '== Boot time / slowest units =='
if command -v systemd-analyze >/dev/null 2>&1; then systemd-analyze 2>&1; echo; systemd-analyze blame --no-pager 2>&1 | head -n 15; else echo 'systemd-analyze not available.'; fi
echo
echo '== Failed units this boot =='
if command -v systemctl >/dev/null 2>&1; then out=$(systemctl --failed --no-legend 2>/dev/null); [ -z "$out" ] && echo 'No failed units.' || echo "$out"; else echo 'systemctl not available.'; fi
echo
echo '== Boot errors (journal, priority err) =='
if command -v journalctl >/dev/null 2>&1; then journalctl -b -p err --no-pager 2>&1 | tail -n 30; fi
""".strip()


def cmd_show_grub_config() -> str:
    return (
        "echo '== /etc/default/grub =='; cat /etc/default/grub 2>/dev/null || echo '(not present)'; "
        "echo; echo '== Default entry =='; "
        "(grub2-editenv list 2>/dev/null || grub-editenv list 2>/dev/null || echo '(no grubenv)')"
    )


def cmd_set_grub_default(entry: str) -> str:
    """Set the default boot entry (index like 0, or a menu-entry title)."""
    entry = (entry or "").strip()
    if not entry:
        raise ValueError("Default entry (index or title) is required.")
    q = shlex.quote(entry)
    return (
        "if command -v grub2-set-default >/dev/null 2>&1; then grub2-set-default " + q + "; "
        "elif command -v grub-set-default >/dev/null 2>&1; then grub-set-default " + q + "; "
        "else echo 'grub-set-default not found.' >&2; exit 1; fi && "
        "echo 'Default boot entry set to " + entry + ".'"
    )


def cmd_set_grub_timeout(seconds: str) -> str:
    seconds = (seconds or "").strip()
    if not seconds.isdigit():
        raise ValueError("Timeout must be a whole number of seconds.")
    return (
        f"sed -i 's/^GRUB_TIMEOUT=.*/GRUB_TIMEOUT={seconds}/' /etc/default/grub && "
        "grep -q '^GRUB_TIMEOUT=' /etc/default/grub || echo 'GRUB_TIMEOUT="
        f"{seconds}' >> /etc/default/grub; " + _grub_rebuild_fragment() +
        f" && echo 'GRUB timeout set to {seconds}s and grub.cfg rebuilt.'"
    )


def cmd_rebuild_grub() -> str:
    return _grub_rebuild_fragment() + " && echo 'GRUB configuration rebuilt.'"


def cmd_set_boot_target(target: str) -> str:
    """Persistent default systemd target for the next boot."""
    target = (target or "").strip()
    if target not in _BOOT_TARGETS:
        raise ValueError(f"Target must be one of: {', '.join(sorted(_BOOT_TARGETS))}.")
    return (
        f"systemctl set-default {target}.target && "
        f"echo 'Default boot target set to {target}.target (takes effect on next reboot).'"
    )


def cmd_set_kernel_cmdline(params: str) -> str:
    """Set GRUB_CMDLINE_LINUX kernel parameters and rebuild grub."""
    params = (params or "").strip()
    if not _CMDLINE_RE.match(params):
        raise ValueError("Kernel parameters contain unexpected characters.")
    q = shlex.quote(params)
    return (
        f"newline='GRUB_CMDLINE_LINUX=\"'{q}'\"'; "
        "if grep -q '^GRUB_CMDLINE_LINUX=' /etc/default/grub; then "
        "sed -i \"s|^GRUB_CMDLINE_LINUX=.*|$newline|\" /etc/default/grub; "
        "else echo \"$newline\" >> /etc/default/grub; fi && " + _grub_rebuild_fragment() +
        " && echo 'Kernel parameters updated and grub.cfg rebuilt (effective next boot).'"
    )


def cmd_regenerate_initramfs() -> str:
    return (
        "if command -v dracut >/dev/null 2>&1; then dracut -f && echo 'initramfs regenerated (dracut).'; "
        "elif command -v update-initramfs >/dev/null 2>&1; then update-initramfs -u -k all && echo 'initramfs regenerated (update-initramfs).'; "
        "elif command -v mkinitcpio >/dev/null 2>&1; then mkinitcpio -P && echo 'initramfs regenerated (mkinitcpio).'; "
        "else echo 'No initramfs tool found (dracut/update-initramfs/mkinitcpio).' >&2; exit 1; fi"
    )


def cmd_list_kernels() -> str:
    return (
        "echo \"Running kernel: $(uname -r)\"; echo; echo 'Installed kernels:'; "
        "if command -v rpm >/dev/null 2>&1; then rpm -q kernel kernel-core 2>/dev/null | grep -v 'not installed'; "
        "elif command -v dpkg-query >/dev/null 2>&1; then dpkg-query -W -f='${Package} ${Version}\\n' 'linux-image-*' 2>/dev/null | grep -v -- '-dbg'; "
        "else echo 'Neither rpm nor dpkg found.'; fi"
    )


def cmd_remove_old_kernels(keep: str = "2") -> str:
    """Remove superfluous old kernels, always keeping the running one and
    the most recent `keep` total."""
    keep = (keep or "2").strip()
    if not keep.isdigit() or int(keep) < 1:
        raise ValueError("Keep count must be a positive whole number.")
    return (
        "if command -v dnf >/dev/null 2>&1; then "
        f"dnf -y remove --oldinstallonly --setopt installonly_limit={keep} 2>&1 || "
        "echo 'Nothing to remove (or dnf-plugins-core missing).'; "
        "elif command -v apt-get >/dev/null 2>&1; then "
        "DEBIAN_FRONTEND=noninteractive apt-get -y --purge autoremove 2>&1; "
        "elif command -v zypper >/dev/null 2>&1; then "
        "zypper --non-interactive purge-kernels 2>&1 || echo 'purge-kernels needs the zypper purge-kernels plugin.'; "
        "else echo 'No supported package manager found.' >&2; exit 1; fi; "
        "echo; echo 'Done. Current running kernel is always kept.'"
    )
