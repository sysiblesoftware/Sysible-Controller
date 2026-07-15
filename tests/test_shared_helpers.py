"""
The pkgmgr detection/dispatch helpers and the small validators are now single-
sourced (client/_pkgmgr.py, client/_validators.py) instead of copy-pasted across
_api_* modules. These tests guard against (a) a copy sneaking back in, and (b) the
installer's own shell cascade drifting out of sync with the Python fragment.
"""
import pathlib

from client import _pkgmgr, _validators


REPO = pathlib.Path(__file__).resolve().parent.parent
CLIENT = REPO / "client"


def test_no_module_redefines_the_shared_helpers():
    # No _api_* module should define its own _pkgmgr_detect_fragment / _pkgmgr_dispatch
    # or the consolidated validators -- they import them from the shared modules.
    banned = ("def _pkgmgr_detect_fragment", "def _pkgmgr_dispatch",
              "def _validate_int_range", "def _validate_nonempty_line",
              "def _validate_user_or_group")
    offenders = []
    for f in CLIENT.glob("_api_*.py"):
        text = f.read_text()
        for b in banned:
            if b in text:
                offenders.append(f"{f.name}: {b}")
    assert not offenders, "these should import the shared helper, not redefine it:\n" + "\n".join(offenders)


def test_pkgmgr_order_and_dispatch():
    frag = _pkgmgr.pkgmgr_detect_fragment()
    # dnf preferred over yum, then zypper, then apt-get.
    assert frag.index("dnf") < frag.index("yum") < frag.index("zypper") < frag.index("apt-get")
    d = _pkgmgr.pkgmgr_dispatch("RPM", "ZYP", "APT")
    assert '"$PKGMGR" = "dnf"' in d and '"$PKGMGR" = "zypper"' in d and "RPM" in d and "ZYP" in d and "APT" in d


def test_installer_pkgmgr_cascade_matches_python():
    # The installer keeps its own shell copy (it can't import Python); assert its
    # detection order stays in agreement with the shared Python fragment.
    installer = (REPO / "install_sysible.sh").read_text()
    order = [installer.index(f"command -v {p}") for p in ("dnf", "yum", "zypper", "apt-get")]
    assert order == sorted(order), "installer package-manager detection order drifted from dnf>yum>zypper>apt-get"


def test_shared_validators_behave():
    assert _validators.validate_int_range("7", 1, 10, "x") == 7
    for bad in ("abc", "0", "99"):
        try:
            _validators.validate_int_range(bad, 1, 10, "x"); raise AssertionError("should have raised")
        except ValueError:
            pass
    assert _validators.validate_nonempty_line("hi", "x") == "hi"
    try:
        _validators.validate_nonempty_line("a\nb", "x"); raise AssertionError("should have raised")
    except ValueError:
        pass
    assert _validators.validate_user_or_group("root", "User") == "root"
    try:
        _validators.validate_user_or_group("bad name", "User"); raise AssertionError("should have raised")
    except ValueError:
        pass
