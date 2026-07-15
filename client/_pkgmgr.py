"""Cross-distro package-manager detection — the single source of truth.

Several dual-host command builders need the same shell fragment that picks
whichever of dnf / yum / zypper / apt-get is present (preferring dnf over yum).
This used to be copy-pasted, byte-for-byte, into _api_automation, _api_security,
and _api_storage (with a 4th cascade in install_sysible.sh), so adding a package
manager or changing the preference order meant editing every copy. It lives here
now; those modules import it. The installer keeps its own shell copy (it can't
import Python) but a test cross-checks the two stay in agreement.

This module intentionally imports nothing from client/ — so any _api_* module can
import it without the circular-import risk the old "each module self-contained"
rule was avoiding.
"""


def pkgmgr_detect_fragment(var: str = "PKGMGR") -> str:
    """Shell fragment that sets $<var> to whichever of dnf / yum / zypper /
    apt-get is actually present on this host, preferring dnf over yum where a
    host has both."""
    return (
        f"if command -v dnf >/dev/null 2>&1; then {var}=dnf; "
        f"elif command -v yum >/dev/null 2>&1; then {var}=yum; "
        f"elif command -v zypper >/dev/null 2>&1; then {var}=zypper; "
        f"elif command -v apt-get >/dev/null 2>&1; then {var}=apt-get; "
        f"else echo 'No supported package manager found (looked for dnf, yum, zypper, apt-get).' >&2; exit 1; fi"
    )


def pkgmgr_dispatch(rpm_cmd: str, zypper_cmd: str, apt_cmd: str) -> str:
    """Wraps the detection fragment above around three command templates and
    branches to whichever one matches."""
    detect = pkgmgr_detect_fragment()
    return (
        f'{detect}; '
        f'if [ "$PKGMGR" = "dnf" ] || [ "$PKGMGR" = "yum" ]; then {rpm_cmd}; '
        f'elif [ "$PKGMGR" = "zypper" ]; then {zypper_cmd}; '
        f'else {apt_cmd}; fi'
    )
