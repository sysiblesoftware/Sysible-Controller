"""Turning a firewall OFF must be a button that says so, and must ask first.

From "why do random hosts keep turning off their firewall?".

"Enable/disable firewalld" was ONE button. Its direction came from an "Enabled"
checkbox rendered above it — in a group shared with Status, Install and Reload,
so the checkbox did not visibly belong to it. With the box clear, that button
turned a host's firewall off: same label, no confirmation, no second look.

It was no better afterwards. The activity feed records an action by its LABEL,
and a checkbox is deliberately excluded from the recorded detail (only
text/select/number params are appended), so every one of these runs was logged
as the string "Enable/disable firewalld" — the audit trail could not say which
direction had been taken either.

Two buttons instead. Disabling is danger-marked, so the console confirms before
running it, like every other destructive action in this tool.
"""
import pytest

from webgui import actions

SERVICE_STATE = [("Firewalld", "fw_enable", "fw_disable"),
                 ("ufw", "fw_ufw_enable", "fw_ufw_disable")]


def _action(name):
    a = actions.get(name)
    assert a is not None, f"{name} is not registered"
    return a


def _catalog_entry(name):
    for tool in actions.catalog():
        for a in tool["actions"]:
            if a["name"] == name:
                return a
    raise AssertionError(f"{name} is not in the catalog the console renders")


# ---- the ambiguous control is gone -----------------------------------------
def test_no_firewall_action_is_driven_by_a_checkbox():
    """A checkbox decided whether a host ended up with a firewall or without
    one, and it sat in a group with three unrelated buttons."""
    for tool in actions.catalog():
        if tool["tool"] != "Firewall Administration":
            continue
        for a in tool["actions"]:
            kinds = [p["type"] for p in a["params"]]
            assert "checkbox" not in kinds, f"{a['name']} still toggles on a checkbox"


def test_the_old_toggle_is_no_longer_registered():
    for gone in ("fw_set_enabled", "fw_ufw_set_enabled"):
        assert actions.get(gone) is None, f"{gone} is still dispatchable"


# ---- two buttons, and the destructive one asks ------------------------------
@pytest.mark.parametrize("tab,on,off", SERVICE_STATE)
def test_enable_and_disable_are_separate_buttons(tab, on, off):
    assert _action(on).label.lower().startswith("enable")
    assert _action(off).label.lower().startswith("disable")


@pytest.mark.parametrize("tab,on,off", SERVICE_STATE)
def test_turning_a_firewall_off_asks_first(tab, on, off):
    assert _action(off).danger is True, "it disables a host's firewall with no confirmation"
    assert _action(on).danger is False, "enabling a firewall should not need a confirmation"


@pytest.mark.parametrize("tab,on,off", SERVICE_STATE)
def test_each_button_does_what_its_label_says(tab, on, off):
    """The direction is in the action now, not in a param the caller supplies —
    so there is no request that can make the Enable button disable."""
    enabling, disabling = _action(on).build({}), _action(off).build({})
    assert "--force enable" in enabling or "enable --now" in enabling, enabling
    assert "ufw disable" in disabling or "disable --now" in disabling, disabling
    assert enabling != disabling


@pytest.mark.parametrize("tab,on,off", SERVICE_STATE)
def test_the_direction_cannot_be_flipped_by_the_request(tab, on, off):
    """The old build() read a boolean out of the posted params. Params are now
    ignored entirely, so a crafted request cannot invert either button."""
    for hostile in ({"enabled": False}, {"enabled": "no"}, {"enabled": 0}):
        assert _action(on).build(hostile) == _action(on).build({})
        assert _action(off).build(hostile) == _action(off).build({})


# ---- the audit trail can finally answer the question ------------------------
@pytest.mark.parametrize("tab,on,off", SERVICE_STATE)
def test_the_activity_feed_says_which_direction_was_taken(tab, on, off):
    """The feed logs an action by its label. "Enable/disable firewalld" told a
    reader nothing, which is exactly the question being asked of it."""
    on_label, off_label = _action(on).label, _action(off).label
    assert on_label != off_label
    for label in (on_label, off_label):
        assert "/" not in label, f"{label!r} is ambiguous in the activity feed"


# ---- both still appear in the console --------------------------------------
@pytest.mark.parametrize("tab,on,off", SERVICE_STATE)
def test_both_buttons_are_rendered_in_the_right_place(tab, on, off):
    for name in (on, off):
        entry = _catalog_entry(name)
        assert entry["tab"] == tab, entry
        assert entry["group"] == "Service State", entry
        assert entry["params"] == []
