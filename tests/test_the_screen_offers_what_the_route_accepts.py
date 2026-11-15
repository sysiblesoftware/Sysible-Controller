"""The enrollment preview must not be stricter than the route it previews.

The screen reported `0 of 1 line(s) can be enrolled` for a hosts file holding
one machine, with `already managed as 'ubuntu-lab'` beside it -- and the host
it refused to enrol was called ubuntu-lab. Posting that very line to
/enroll-ssh would have been accepted: the route excludes the host's OWN record
from the duplicate-address check, because re-enrolling a machine under its own
name is how an operator repairs one whose key has broken. The preview called
_ip_owner() without that exclusion, so it refused on the route's behalf,
and there was no way forward anywhere on the page.

The specific bug is one argument. The class of bug is a second copy of a
rule, drifting from the first, in the half the operator can see -- so these
tests assert the two AGREE rather than asserting the fixed argument.
"""
import pytest

import backend.remote_routes as rr
from backend.models.remote_models import EnrollSSHRequest

INVENTORY = {"ubuntu-lab": {"ip": "192.168.155.135"},
             "db-01": {"ip": "10.0.0.5"}}


@pytest.fixture()
def inventory(monkeypatch):
    """One Controller with two hosts already enrolled, and no network."""
    monkeypatch.setattr(rr, "load_hosts", lambda: dict(INVENTORY))
    monkeypatch.setattr(rr, "_resolve_addrs", lambda t: {t} if t[0].isdigit() else set())
    monkeypatch.setattr(rr, "enforce_host_limit", lambda *a, **k: None, raising=False)
    try:
        monkeypatch.setattr(rr, "list_agents", lambda: [], raising=False)
    except AttributeError:
        pass
    return INVENTORY


def _preview(text):
    return {r["line"]: r for r in rr.hosts_file_preview({"text": text})["rows"]}


def _route_would_refuse_as_duplicate(name, ip):
    """Whether /enroll-ssh stops at the duplicate-address check for this pair.

    The route goes on to open an SSH connection, which a test must not, so
    this asks the exact question the route asks at that point and nothing
    further."""
    return rr._ip_owner(ip, exclude_name=name) is not None


# ---- the defect ------------------------------------------------------------
def test_a_host_can_be_re_enrolled_under_its_own_name(inventory):
    row = _preview("192.168.155.135  ubuntu-lab")[1]
    assert row["enrollable"], (
        f"the screen refuses to re-enrol a host under its own name: "
        f"{row.get('skip_reason')}")


def test_re_enrolling_says_what_it_will_do(inventory):
    """Offered is not the same as silent. It replaces the key on a machine
    that is already being managed."""
    row = _preview("192.168.155.135  ubuntu-lab")[1]
    assert "already enrolled" in (row.get("conflict") or ""), row
    assert "replaces its key" in row["conflict"], row["conflict"]


def test_a_re_enrollment_is_not_ticked_by_default(inventory):
    """The console ticks `enrollable && !conflict`. A row that overwrites a
    live host has to be a thing someone meant."""
    row = _preview("192.168.155.135  ubuntu-lab")[1]
    assert row["enrollable"] and row.get("conflict"), row


# ---- the rule it has to agree with -----------------------------------------
@pytest.mark.parametrize("line,name,ip", [
    ("192.168.155.135  ubuntu-lab", "ubuntu-lab", "192.168.155.135"),
    ("10.0.0.5  db-01", "db-01", "10.0.0.5"),
    ("10.0.0.5  something-else", "something-else", "10.0.0.5"),
    ("10.0.0.9  brand-new", "brand-new", "10.0.0.9"),
])
def test_the_preview_and_the_route_agree(inventory, line, name, ip):
    row = _preview(line)[1]
    refused_by_screen = not row["enrollable"]
    refused_by_route = _route_would_refuse_as_duplicate(name, ip)
    assert refused_by_screen == refused_by_route, (
        f"{line!r}: the screen says {'no' if refused_by_screen else 'yes'} and "
        f"the route says {'no' if refused_by_route else 'yes'}")


def test_the_same_box_under_a_different_name_is_still_refused(inventory):
    """The exclusion must not become a hole. 10.0.0.5 is db-01; offering it as
    something-else is the duplicate row the check exists to prevent."""
    row = _preview("10.0.0.5  something-else")[1]
    assert not row["enrollable"]
    assert "already managed as 'db-01'" in row["skip_reason"]


def test_an_unknown_machine_is_offered_with_no_warning(inventory):
    row = _preview("10.0.0.9  brand-new")[1]
    assert row["enrollable"] and not row.get("conflict") and not row.get("skip_reason")


# ---- names are case-insensitive, here as in DNS ----------------------------
def test_re_enrolling_under_a_differently_cased_name_still_works(inventory):
    """`n == exclude_name` compared exactly, so re-enrolling Ubuntu-Lab over
    the record ubuntu-lab failed the exclusion and 409'd against the host's
    own record."""
    assert rr._ip_owner("192.168.155.135", exclude_name="Ubuntu-Lab") is None
    row = _preview("192.168.155.135  Ubuntu-Lab")[1]
    assert row["enrollable"], row.get("skip_reason")


def test_the_exclusion_does_not_swallow_a_different_host(inventory):
    assert rr._ip_owner("10.0.0.5", exclude_name="Ubuntu-Lab") == "db-01"


# ---- and the route itself still gets there ---------------------------------
def test_the_route_does_not_stop_at_the_duplicate_check_for_its_own_host(inventory, monkeypatch):
    """Past the duplicate check is as far as this can go without opening an
    SSH connection -- so the connection is what it is caught doing."""
    reached = []

    def no_key():
        reached.append("got past the duplicate check")
        raise RuntimeError("stop here")
    monkeypatch.setattr(rr, "_ensure_controller_key", no_key)
    with pytest.raises(Exception) as caught:
        rr.enroll_ssh(EnrollSSHRequest(name="ubuntu-lab", ip="192.168.155.135",
                                       password="x"))
    assert reached, f"refused before it tried: {getattr(caught.value, 'detail', caught.value)}"


def test_the_route_still_refuses_the_same_box_under_a_new_name(inventory):
    with pytest.raises(Exception) as caught:
        rr.enroll_ssh(EnrollSSHRequest(name="something-else", ip="10.0.0.5",
                                       password="x"))
    assert getattr(caught.value, "status_code", None) == 409
    assert "already managed as 'db-01'" in caught.value.detail
