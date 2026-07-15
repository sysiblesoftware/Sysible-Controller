"""
SSH Auth Policy uses explicit-intent buttons, not a checkbox-then-apply toggle.
Hardening a host (deny root SSH) is now a single unambiguous "Disable root login"
click instead of the counter-intuitive "leave the box unchecked, then Apply".
"""
import pytest

from client import api as capi


def test_root_login_mode_builder():
    # The chosen value is applied to sshd's PermitRootLogin (as the `v=` value the
    # generated script writes) and each script targets that option.
    for mode in ("no", "yes", "prohibit-password"):
        s = capi.cmd_set_root_login_mode(mode)
        assert "PermitRootLogin" in s
        assert f"v={mode}" in s
    # Whitelisted: anything else is rejected (never reaches sshd_config).
    for bad in ("maybe", "", "yes; rm -rf /", "No\nPermitRootLogin yes"):
        with pytest.raises(ValueError):
            capi.cmd_set_root_login_mode(bad)


def test_root_login_bool_wrapper_still_works():
    assert "v=yes" in capi.cmd_set_root_login(True)
    assert "v=no" in capi.cmd_set_root_login(False)


def test_catalog_exposes_explicit_auth_buttons_without_checkboxes():
    from webgui import actions
    cat = {t["tool"]: t for t in actions.catalog()}
    acts = {a["name"]: a for a in cat["Security Administration"]["actions"]}
    for name in ("sec_root_login_off", "sec_root_login_keyonly", "sec_root_login_on",
                 "sec_pubkey_on", "sec_pubkey_off", "sec_password_off", "sec_password_on"):
        assert name in acts, f"missing {name}"
        # Explicit buttons carry NO parameters (no checkbox to toggle).
        assert acts[name]["params"] == []
    # The old checkbox-driven action is gone.
    assert "sec_set_root_login" not in acts
    # Loosening actions are marked danger (so the console can confirm them).
    assert acts["sec_root_login_on"]["danger"] is True
    assert acts["sec_password_on"]["danger"] is True
    # Hardening actions are not danger.
    assert acts["sec_root_login_off"]["danger"] is False
