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


# ---- run the probe for real against a host that refuses to answer ---------
#
# Everything above reads the probe. These RUN it, against fakes that reproduce
# verbatim what three real hosts printed when the probe was denied:
#
#   firewall-cmd  NotAuthorizedException ... Make sure polkit agent is running
#   nft           netlink: Error: cache initialization failed: Operation not permitted
#   iptables      Could not fetch rule set generation id: Permission denied
#
# The first version of this fix captured each tool with 2>&1 and then asked "is
# the output non-empty?" to decide whether a ruleset exists. Against that nft
# message it answered yes, and reported backend=nftables active=1 determined=1 —
# a firewall that is not running, stated with confidence, on a host with no
# firewall at all. Claiming protection that does not exist is worse than the
# false alarm this whole change set out to fix, so these tests exist to keep
# stdout and stderr apart.

import subprocess as _sp
import textwrap as _tw

DENIED = {
    "firewall-cmd": (253,
                     "ERROR:dbus.proxies:Introspect error on :1.6:/org/fedoraproject/FirewallD1: "
                     "dbus.exceptions.DBusException: org.fedoraproject.FirewallD1."
                     "NotAuthorizedException: Not Authorized(uid)\n"
                     "Authorization failed.\n"
                     "    Make sure polkit agent is running or run the application as superuser."),
    "nft": (0, "netlink: Error: cache initialization failed: Operation not permitted"),
    "iptables": (0, "iptables v1.8.13 (nf_tables): Could not fetch rule set generation id: "
                    "Permission denied (you must be root)"),
}


def _fw_block():
    from client import _api_dispatch as d
    sh = d.cmd_posture_snapshot()
    i = sh.index("# --- Firewall ---")
    return sh[i:sh.index("# --- Users / accounts ---", i)]


def _run_probe(tmp_path, tools, enabled_at_boot="firewalld"):
    """The shipping firewall block, run with these fakes on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, (rc, err) in tools.items():
        f = bindir / name
        f.write_text("#!/bin/sh\ncat >&2 <<'SYSEOF'\n" + err + "\nSYSEOF\nexit %d\n" % rc)
        f.chmod(0o755)
    sc = bindir / "systemctl"
    sc.write_text(_tw.dedent(f"""\
        #!/bin/sh
        case "$*" in *is-enabled*{enabled_at_boot}*) echo enabled; exit 0;; esac
        exit 1
        """))
    sc.chmod(0o755)
    script = 'p(){ printf "POSTURE|%s=%s\\n" "$1" "$2"; }\nTMO=""\n' + _fw_block()
    r = _sp.run(["sh", "-s"], input=script, capture_output=True, text=True,
                env={"PATH": f"{bindir}:/usr/bin:/bin"}, timeout=60)
    assert r.returncode == 0, r.stderr
    out = {}
    for line in r.stdout.splitlines():
        if line.startswith("POSTURE|"):
            k, _, v = line[len("POSTURE|"):].partition("=")
            out[k] = v
    return out


def test_a_denied_probe_never_invents_a_firewall(tmp_path):
    """The regression that matters most. nft's permission error on stderr must
    not be mistaken for a ruleset."""
    out = _run_probe(tmp_path, DENIED)
    assert out["fw.active"] == "0", (
        f"the probe reported a firewall as RUNNING on a host that refused every "
        f"query (backend={out['fw.backend']}) — that is protection claimed and "
        f"not present")
    assert out["fw.backend"] == "none"


def test_a_denied_probe_reports_unknown_not_broken(tmp_path):
    out = _run_probe(tmp_path, DENIED)
    assert out["fw.determined"] == "0", \
        "a host that refused every query is being reported as a definite answer"


@pytest.mark.parametrize("tool", list(DENIED))
def test_each_denial_is_named_in_the_reason(tmp_path, tool):
    """fw.why is what stops the next report of this starting from zero. The
    first round of this bug cost a full round trip precisely because the console
    said nothing about WHY it thought the firewall was down."""
    out = _run_probe(tmp_path, DENIED)
    assert tool in out["fw.why"], f"{tool}'s refusal is not recorded"
    assert "denied" in out["fw.why"]


def test_the_probe_records_the_uid_it_ran_as(tmp_path):
    """Every one of these CLIs needs root on most distros, so this is the first
    thing worth knowing when a host's firewall state looks wrong."""
    out = _run_probe(tmp_path, DENIED)
    assert out.get("fw.probe_uid") not in (None, ""), \
        "the probe does not say what uid it ran as"


def test_a_real_empty_ruleset_is_still_a_definite_no(tmp_path):
    """The opposite direction: nft answering cleanly with nothing is a real
    answer and must stay determined, or the panel goes quiet on hosts that
    genuinely have no firewall."""
    out = _run_probe(tmp_path, {"nft": (0, "")}, enabled_at_boot="nothing")
    assert out["fw.active"] == "0"
    assert out["fw.determined"] == "1", "an empty ruleset is being called unknown"
    assert "nft:empty" in out["fw.why"]
