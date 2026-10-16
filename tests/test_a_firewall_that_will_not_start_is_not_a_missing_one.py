"""Reported as: "I have turned on the firewall multiple times but it's still
showing as disabled in the morning."

They had turned it on, and the console was wrong about it. Every firewall
builder persists correctly — `systemctl enable --now`, and `ufw --force enable`
followed by `systemctl enable ufw` — so the enabling survives a reboot. What does
not always survive is the START: one of these hosts has a systemd ordering cycle
involving cloud-init, and systemd breaks it by deleting firewalld's job. The
host reboots at 08:00 every morning and comes up configured but unprotected.

The posture flag was computed from fw.active alone:

    "firewall_disabled": (fw == "0") if fw is not None else None

so that host was reported as "Firewall disabled" — on the dashboard chip, in the
Needs-attention row, and in the alert email. To the operator who turned it on the
night before, that reads as "your change did not take", and the obvious response
is to turn it on again, which cannot help.

Two flags now. Both are severity 1, because the host is unprotected either way;
they differ in what to go and do about it.
"""
import pytest

from webgui import server


def _flags(active, enabled_at_boot=None):
    """Posture as an agent reports it. enabled_at_boot=None is an OLDER agent,
    from before the probe learned to report it."""
    fw = {"active": active}
    if enabled_at_boot is not None:
        fw["enabled_at_boot"] = enabled_at_boot
    return server._posture_flags({"fw": fw})


# ---- the host that prompted this -------------------------------------------
def test_a_firewall_enabled_at_boot_but_not_running_is_its_own_finding():
    f = _flags("0", "firewalld")
    assert f["firewall_failed_to_start"] is True
    assert f["firewall_disabled"] is False, \
        "the operator is still being told their firewall is switched off"


def test_a_host_with_no_firewall_at_all_still_reads_as_disabled():
    """The other side of the split must not go quiet — this one really is
    'nobody set one up'."""
    f = _flags("0", "none")
    assert f["firewall_disabled"] is True
    assert f["firewall_failed_to_start"] is False


def test_a_healthy_host_fires_neither():
    f = _flags("1", "firewalld")
    assert f["firewall_disabled"] is False
    assert f["firewall_failed_to_start"] is False


@pytest.mark.parametrize("backend", ["firewalld", "ufw", "nftables"])
def test_any_backend_counts_as_configured(backend):
    assert _flags("0", backend)["firewall_failed_to_start"] is True


# ---- older agents must not change behaviour --------------------------------
def test_an_agent_that_does_not_report_the_new_key_behaves_as_before():
    """The probe key is new. Until a host's agent updates it reports only
    fw.active, and that host must keep reading exactly as it did — not silently
    flip to the new finding on no evidence."""
    f = _flags("0")
    assert f["firewall_disabled"] is True
    assert f["firewall_failed_to_start"] is False


def test_a_host_that_answered_nothing_is_accused_of_nothing():
    """Unknown is not bad. An empty posture yields no flags at all — documented
    behaviour, and the reason the dashboard counts only the True ones."""
    assert server._posture_flags({}) == {}


def test_a_host_with_no_firewall_section_reports_neither_as_unknown():
    """It answered, but said nothing about a firewall: both flags are None
    (undetermined), not False (clean) and not True (a problem)."""
    f = server._posture_flags({"os": {"distro": "ubuntu"}})
    assert f["firewall_disabled"] is None
    assert f["firewall_failed_to_start"] is None


# ---- both are still treated as serious -------------------------------------
def test_both_are_critical():
    """The host is unprotected either way. Splitting the finding must not
    quietly demote half of it."""
    assert "firewall_failed_to_start" in server._CRITICAL_POSTURE_LABELS
    assert "firewall_disabled" in server._CRITICAL_POSTURE_LABELS


def test_the_two_labels_do_not_say_the_same_thing():
    labels = server._CRITICAL_POSTURE_LABELS
    assert labels["firewall_disabled"] != labels["firewall_failed_to_start"]
    assert "not running" in labels["firewall_failed_to_start"]
    assert "disabled" not in labels["firewall_failed_to_start"].lower(), \
        "it still tells the operator the firewall was switched off"


# ---- every consumer was updated, not just the computation ------------------
def test_the_dashboard_counts_it_as_its_own_signal():
    from pathlib import Path
    src = Path("webgui/frontend/src/views/Dashboard.jsx").read_text(encoding="utf-8")
    assert '"firewall_failed_to_start"' in src
    assert "firewall_failed_to_start: 1" in src, "it is not treated as critical"


def test_alerting_can_fire_on_it():
    """An operator who alerts on "firewall disabled" must still be alerted when
    the firewall is configured and down — splitting the flag must not create a
    gap that silently stops paging."""
    from webgui import alerts
    assert "firewall_failed_to_start" in alerts.RULES
    label = alerts.RULES["firewall_failed_to_start"][0]
    assert "not running" in label


def test_it_can_be_suppressed_like_any_other_finding():
    """Otherwise a host with a known, accepted cause cannot be silenced and the
    fleet rollup never goes green."""
    from webgui import suppressions
    assert "firewall_failed_to_start" in suppressions.KNOWN_KEYS
