"""Hosts whose firewall was up the whole time, reported in red as "enabled but
not running".

Reported from a live fleet: three openSUSE hosts and a Debian one sitting at the
top of Needs Attention, every one of them flagged "Firewall enabled but not
running", every one of them with a firewall that was enabled AND running.

The flag is computed from two posture keys that come from two DIFFERENT places:

    fw.enabled_at_boot  <- systemctl is-enabled firewalld|ufw|nftables
    fw.active           <- the firewall's own CLI (firewall-cmd / ufw / nft)

and `fw.active=0` carried two meanings at once — "nothing is running" and "every
way of asking failed". systemd almost always answers, so any CLI that could not
(missing from PATH, D-Bus unreachable, polkit refusing, the binary timing out)
produced active=0 beside a perfectly good enabled_at_boot. The combination reads
as "someone configured a firewall and it died", which is a critical finding, so
it went to the top of the page in red.

The fix is that the host now says whether it managed to look at all
(fw.determined), and a host that could not look is reported as UNKNOWN rather
than as either a problem or an all-clear. "I do not know" is a worse answer than
"it is running" and a much better one than a red alert about a healthy host —
the cost of crying wolf here is that the operator stops reading the one panel
that is supposed to tell them their fleet is unprotected.

What must NOT regress: a firewall that is genuinely stopped is still a critical
finding, and a host with no firewall at all is still "disabled". Those are the
findings this panel exists for.
"""
import re
from pathlib import Path

import pytest

from webgui.server import _posture_flags

REPO = Path(__file__).resolve().parents[1]
PROBE = REPO / "client" / "_api_dispatch.py"


def _p(active, boot, determined=None, why=None):
    fw = {"active": active, "enabled_at_boot": boot}
    if determined is not None:
        fw["determined"] = determined
    if why is not None:
        fw["why"] = why
    return {"fw": fw}


# ---- the report -----------------------------------------------------------

def test_a_host_that_could_not_look_is_not_called_broken():
    """The bug, in one assertion."""
    f = _posture_flags(_p("0", "firewalld", determined="0",
                          why="firewall-cmd:rc=1:Failed to connect to bus"))
    assert f["firewall_failed_to_start"] is None, (
        "a host that could not determine its firewall state is being reported "
        "as a firewall that failed to start — a critical finding about a host "
        "that may be perfectly healthy")


def test_a_host_that_could_not_look_is_not_called_clean_either():
    """The opposite error, and the worse one. 'Unknown' must not be laundered
    into 'fine' — this panel's whole job is saying when a host is unprotected."""
    f = _posture_flags(_p("0", "none", determined="0", why="ufw:rc=1:permission denied"))
    assert f["firewall_disabled"] is None, \
        "an undetermined firewall state is being reported as an all-clear"


# ---- what must still fire -------------------------------------------------

def test_a_genuinely_stopped_firewall_is_still_critical():
    """firewall-cmd exits 252 and says "not running" — a trustworthy negative,
    and exactly the case this flag was added for."""
    f = _posture_flags(_p("0", "firewalld", determined="1", why="firewalld:stopped"))
    assert f["firewall_failed_to_start"] is True
    assert f["firewall_disabled"] is False


def test_a_host_with_no_firewall_at_all_is_still_disabled():
    f = _posture_flags(_p("0", "none", determined="1", why="none-installed"))
    assert f["firewall_disabled"] is True
    assert f["firewall_failed_to_start"] is False


def test_a_running_firewall_raises_nothing():
    f = _posture_flags(_p("1", "firewalld", determined="1"))
    assert f["firewall_failed_to_start"] is False
    assert f["firewall_disabled"] is False


def test_an_agent_that_predates_the_key_behaves_exactly_as_before():
    """Agents update on their own heartbeat and some are powered off for weeks.
    Until one is updated it sends no fw.determined, and the flag must keep its
    old meaning rather than every host going quiet — a silent panel is how a
    genuinely unprotected host gets missed."""
    f = _posture_flags(_p("0", "firewalld"))
    assert f["firewall_failed_to_start"] is True
    f = _posture_flags(_p("0", "none"))
    assert f["firewall_disabled"] is True


def test_no_posture_at_all_is_still_unknown():
    assert _posture_flags({}) == {}
    assert _posture_flags(None) == {}


# ---- the probe side -------------------------------------------------------

@pytest.fixture(scope="module")
def probe():
    """The firewall block of the shipping probe, WITH COMMENTS STRIPPED.

    Not a nicety. The reasoning for each rule here is written in a comment
    directly above the code that implements it, quoting the very strings these
    assertions search for — so a substring test over the raw block matches the
    explanation and passes whatever the code does. Three tests in this change
    were written that way and had to be found by deleting the thing they
    protected and watching them stay green."""
    src = PROBE.read_text()
    i = src.index("# --- Firewall ---")
    block = src[i:src.index("# --- Users / accounts ---", i)]
    return "\n".join(l for l in block.splitlines() if not l.lstrip().startswith("#"))


def test_the_probe_reports_whether_it_managed_to_look(probe):
    assert "fw.determined" in probe, "the host cannot say 'I could not tell'"
    assert "fw.why" in probe, \
        "nothing records WHY the firewall read as inactive, so the next report " \
        "of this starts from zero again"


@pytest.mark.parametrize("signal,what", [
    ("252", "firewalld's stopped exit code"),
    ("not running", "firewalld's stopped message"),
    ("Status: inactive", "ufw reporting itself inactive"),
])
def test_a_trustworthy_negative_is_still_recorded_as_determined(probe, signal, what):
    """252 / 'not running' from firewall-cmd, 'Status: inactive' from ufw and an
    empty nft ruleset are real answers, and each one must set fwdet=1.

    Checking only that the signal is still MENTIONED is not enough, and this
    test was written that way first: deleting the `fwdet=1` from the stopped
    branch left it green. That change would have made a genuinely stopped
    firewall report as 'unknown', which this panel renders as nothing at all —
    turning a fix for crying wolf into a panel that stays quiet while a host
    sits unprotected. That is the failure worth being strict about."""
    assert signal in probe, f"{what} is no longer detected"
    # The branch that recognises this signal must mark the answer trustworthy.
    line = next((l for l in probe.splitlines() if signal in l), None)
    assert line is not None
    idx = probe.splitlines().index(line)
    window = "\n".join(probe.splitlines()[idx:idx + 3])
    assert "fwdet=1" in window, (
        f"{what} is recognised but not recorded as a definite answer — a "
        f"genuinely stopped firewall would be reported as 'unknown', and this "
        f"panel shows nothing for unknown")


def test_an_empty_nft_ruleset_is_a_definite_answer(probe):
    line = next(l for l in probe.splitlines() if "nft:empty" in l)
    assert "fwdet=1" in line, "an empty nft ruleset is being treated as unknown"


def test_ufw_is_not_decided_from_systemd(probe):
    """ufw's unit is a RemainAfterExit oneshot: it reads active long after
    `ufw disable` has flushed every rule. Deciding ufw from systemd would report
    protection that does not exist, which is worse than the bug being fixed."""
    assert not re.search(r"is-active[^\n]*\bufw\b", probe), (
        "ufw's running state is being taken from systemd — it will report a "
        "disabled firewall as active")


def test_every_probe_failure_is_distinguishable_from_a_negative(probe):
    """Each tool's branch has to separate 'answered no' from 'could not answer',
    or the bug comes straight back for that tool only — which is how it survived
    on exactly one distro family last time."""
    for tool in ("firewall-cmd", "ufw", "nft", "iptables"):
        assert f"{tool}:rc=" in probe or f'_fwnote "{tool}' in probe, \
            f"{tool} cannot report that it failed, so its failure reads as 'off'"
