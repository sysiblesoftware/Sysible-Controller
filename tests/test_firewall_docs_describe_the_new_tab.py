"""The documentation must describe the tab people will actually land on.

Three places tell an operator what this tool does, and all three described a
console that no longer exists — firewalld zones, rich rules, nftables, iptables,
and nothing about the backend-agnostic tab that is now the default. The in-app
search is the worst of the three: typing "open port" sent the operator to a tool
whose first tab is firewalld, which is the wrong tab on every Debian/Ubuntu host.
"""
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SEARCH = (ROOT / "webgui/frontend/src/featureSearch.js").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")


def _readme_row():
    for line in README.splitlines():
        if line.startswith("| **Firewall Administration**"):
            return line
    raise AssertionError("the Firewall Administration row is gone from the README")


# ---- the in-app task search ------------------------------------------------
@pytest.mark.parametrize("phrase", ["open port", "close port", "enable firewall",
                                    "disable firewall", "firewall status"])
def test_plain_language_searches_land_on_the_agnostic_tab(phrase):
    """These are the words an operator types. None of them should require
    knowing whether the host runs firewalld or ufw."""
    entries = re.findall(r"\{[^{}]*?keywords:\s*\[[^\]]*\][^{}]*\}", SEARCH, re.S)
    hit = [e for e in entries if f'"{phrase}"' in e]
    assert hit, f"nothing in the task search matches {phrase!r}"
    assert any('tab: "Firewall"' in e for e in hit), \
        f"{phrase!r} does not open the tab that works on any host"


@pytest.mark.parametrize("name", ["firewalld", "ufw", "nftables", "iptables"])
def test_searching_a_backend_by_name_still_reaches_its_own_tab(name):
    """Someone who typed the name of a tool means that tool, and the zone/rich
    rule/raw-ruleset actions live only there."""
    assert f'"{name}"' in SEARCH


# ---- the README ------------------------------------------------------------
def test_the_readme_leads_with_the_tab_that_works_anywhere():
    row = _readme_row()
    assert "without you knowing its backend" in row, \
        "the README still describes a console organised by backend"
    assert "mixed fleet" in row


def test_the_readme_records_the_two_protections():
    """Both exist because their absence broke a fleet: a firewall that cut the
    connection it was enabled over, and two firewalls fighting over netfilter."""
    row = _readme_row()
    assert "never enables the second firewall" in row
    assert "keeps the ports already in use open" in row


def test_the_readme_still_credits_the_backend_tabs():
    """They are not deprecated — zones, rich rules and raw rulesets live there
    and nowhere else."""
    row = _readme_row()
    for kept in ("zones", "rich rules", "nftables", "iptables"):
        assert kept in row, kept


# ---- the published manual --------------------------------------------------
def test_the_public_manual_card_was_updated():
    manual = Path("/home/user/sysible-website/controller-manual.html")
    if not manual.exists():
        pytest.skip("the website checkout is not present")
    html = manual.read_text(encoding="utf-8")
    i = html.index("<h4>Firewall Administration</h4>")
    card = html[i:i + 700]
    assert "without you knowing its backend" in card
    assert "nftables" in card, "it dropped the backend tabs entirely"
