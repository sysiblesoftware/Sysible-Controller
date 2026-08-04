"""Regression: identifier validators reject a leading dash (option-injection shape).

Benign in practice (useradd/usermod/userdel/chage/nmcli have no command-executing
option), but kept consistent with the package-name and _api_security validators.
"""
import client.api  # noqa: F401  (initialise the client package / resolve import order)
import client._api_users as U
import client._api_network as N

import pytest


@pytest.mark.parametrize("fn", [
    U.cmd_create_user, U.cmd_delete_user, U.cmd_lock_user,
    U.cmd_unlock_user, U.cmd_force_password_reset,
])
def test_username_leading_dash_rejected(fn):
    with pytest.raises(ValueError):
        fn("-rf")


def test_set_user_shell_leading_dash_rejected():
    with pytest.raises(ValueError):
        U.cmd_set_user_shell("-rf", "/bin/bash")


def test_legit_usernames_still_work():
    assert "alice" in U.cmd_create_user("alice")
    assert "web-01" in U.cmd_delete_user("web-01")   # an internal dash is fine


def test_connection_name_leading_dash_rejected():
    with pytest.raises(ValueError):
        N._validate_connection_name("-x")
    assert N._validate_connection_name("eth0") == "eth0"
