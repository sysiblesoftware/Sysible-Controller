"""Cross-distro package-manager detection — the single source of truth.

Several dual-host command builders need the same shell fragment that picks
whichever of dnf / yum / zypper / apt-get / pacman is present (preferring dnf
over yum). This used to be copy-pasted, byte-for-byte, into _api_automation,
_api_security, and _api_storage (with a 4th cascade in install_sysible.sh), so
adding a package manager or changing the preference order meant editing every
copy. It lives here now; those modules import it. The installer keeps its own
shell copy (it can't import Python) but a test cross-checks the two stay in
agreement.

This module intentionally imports nothing from client/ — so any _api_* module can
import it without the circular-import risk the old "each module self-contained"
rule was avoiding.
"""


def pkgmgr_detect_fragment(var: str = "PKGMGR") -> str:
    """Shell fragment that sets $<var> to whichever of dnf / yum / zypper /
    apt-get / pacman is actually present on this host, preferring dnf over yum
    where a host has both."""
    return (
        f"if command -v dnf >/dev/null 2>&1; then {var}=dnf; "
        f"elif command -v yum >/dev/null 2>&1; then {var}=yum; "
        f"elif command -v zypper >/dev/null 2>&1; then {var}=zypper; "
        f"elif command -v apt-get >/dev/null 2>&1; then {var}=apt-get; "
        f"elif command -v pacman >/dev/null 2>&1; then {var}=pacman; "
        f"else echo 'No supported package manager found (looked for dnf, yum, zypper, apt-get, pacman).' >&2; exit 1; fi"
    )


def pacman_install(pkgs: str) -> str:
    """Arch install command: --needed skips already-installed packages (so it's
    idempotent) and --noconfirm keeps it non-interactive. -Sy first refreshes the
    sync database, since Arch has no separate 'update metadata' step and
    installing against a stale db can fail with 404s on rolling mirrors."""
    return f"pacman -Sy --needed --noconfirm {pkgs}"


# zypper reserves exit codes 100-107 for INFORMATION; its actual errors are 1-99.
# Two of those informational codes are ordinary outcomes of a SUCCESSFUL
# transaction, and both were being reported to the operator as "failed · exit N"
# beside a log that plainly shows every package installed:
#
#   102  ZYPPER_EXIT_INF_REBOOT_NEEDED   installed; the host wants a reboot
#   103  ZYPPER_EXIT_INF_RESTART_NEEDED  installed; zypper updated ITSELF, and says
#        so in as many words: "Run this command once more to install any other
#        needed patches."
#
# 100/101 ("updates"/"security updates are available") belong to the query
# commands and are likewise not failures.
#
# These stay failures, because they are: 104 capability not found, 105 killed by a
# signal, 106 a repository was SKIPPED (treating that as success could hide
# security patches that were never even considered), 107 an rpm scriptlet failed.
ZYPPER_INFO_SUCCESS = (0, 100, 101, 102, 103)


def zypper_transaction(cmd: str, rerun_on_restart: bool = False) -> str:
    """Wrap a zypper transaction so its exit status means what the console thinks.

    The console judges a host by `code == 0`, which is right for every other
    package manager and wrong for zypper. This translates the informational codes
    to success and leaves the real errors alone.

    rerun_on_restart: on 103, run the command a SECOND time. That is not a retry
    of something that failed — the first run succeeded; zypper replaced itself
    mid-transaction and asks to be re-run so the remaining patches get applied.
    Doing it here is the difference between an operator being told a patch run
    failed and the patch run actually finishing.

    Deliberately does NOT call `exit`: callers append further commands (the
    per-package status readout in webgui/actions._pkg_and_status), and exiting
    here would swallow them. `(exit N)` sets $? without leaving the script.
    """
    ok = "|".join(str(c) for c in ZYPPER_INFO_SUCCESS)
    rerun = ""
    if rerun_on_restart:
        rerun = (
            'if [ "$_zrc" -eq 103 ]; then '
            "echo 'zypper updated the package manager itself (exit 103); "
            "re-running once, as zypper instructs, to apply the rest.' >&2; "
            f'{cmd}; _zrc=$?; fi; '
        )
    return (
        f'{cmd}; _zrc=$?; '
        f'{rerun}'
        f'case "$_zrc" in {ok}) _zrc=0 ;; esac; '
        f'(exit "$_zrc")'
    )


def pkgmgr_dispatch(rpm_cmd: str, zypper_cmd: str, apt_cmd: str,
                    pacman_cmd: str = None) -> str:
    """Wraps the detection fragment above around per-family command templates and
    branches to whichever one matches. `pacman_cmd` is optional: when a caller
    hasn't supplied an Arch variant, an Arch host gets a clear message instead of
    a wrong command (the action simply isn't wired for pacman yet)."""
    detect = pkgmgr_detect_fragment()
    if pacman_cmd is None:
        pacman_cmd = (
            "echo 'This action is not supported on Arch Linux (pacman) yet.' >&2; exit 1"
        )
    return (
        f'{detect}; '
        f'if [ "$PKGMGR" = "dnf" ] || [ "$PKGMGR" = "yum" ]; then {rpm_cmd}; '
        f'elif [ "$PKGMGR" = "zypper" ]; then {zypper_cmd}; '
        f'elif [ "$PKGMGR" = "pacman" ]; then {pacman_cmd}; '
        f'else {apt_cmd}; fi'
    )
