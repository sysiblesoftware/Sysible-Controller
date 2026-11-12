"""You should not have to type a machine's identity twice.

Asked for: "There should be a better way to enroll more hosts. IP should be
optional if using /etc/hosts."

The form had Host name AND Address, both required. On a fleet whose names are
already in /etc/hosts or DNS that is the same string twice, and for a LIST of
hosts it meant looking every address up by hand and typing it back in -- doing
by hand the one job a resolver exists for.

So the address is optional now, and the bulk box takes a bare list of names.
The Controller resolves a name the same way the rest of the host does.

Resolution is also what finally makes the duplicate check work. It compares
addresses, and `web-01` and `10.0.0.6` are the same machine written two ways --
so the check that exists to stop one box becoming two rows used to miss
precisely the case that made it easy.

Run: pytest tests/
"""
import socket
from pathlib import Path

import pytest

from backend import hosts_file
from backend.models.remote_models import EnrollSSHRequest

REPO = Path(__file__).resolve().parents[1]
VIEW = (REPO / "webgui" / "frontend" / "src" / "views" / "HostEnrollment.jsx").read_text(
    encoding="utf-8")


@pytest.fixture()
def rr(monkeypatch):
    import backend.remote_routes as mod
    return mod


# ---- the address is optional ----------------------------------------------
def test_the_model_accepts_a_request_with_no_address():
    r = EnrollSSHRequest(name="web-01", password="x")
    assert r.ip == ""


def test_the_model_still_refuses_an_address_it_could_not_safely_use():
    """Empty is allowed; a value with shell metacharacters is not. That pattern
    is what keeps a leading dash out of an ssh argv."""
    with pytest.raises(Exception):
        EnrollSSHRequest(name="web-01", ip="-oProxyCommand=touch /tmp/x", password="x")


def test_the_form_no_longer_marks_the_address_required():
    assert 'value={push.ip} required' not in VIEW, \
        "the address field is still required in the console"
    assert "(optional)" in VIEW, "nothing tells the operator it can be left out"


# ---- resolution ------------------------------------------------------------
def test_an_address_resolves_to_itself_without_a_lookup(rr, monkeypatch):
    """No DNS round trip for something that is already an address."""
    def boom(*a, **k):
        raise AssertionError("getaddrinfo was called for a literal address")
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    assert rr._resolve_addrs("10.0.0.5") == {"10.0.0.5"}


def test_a_name_resolves_through_the_hosts_resolver(rr):
    """localhost is in every /etc/hosts, which is the mechanism asked for."""
    assert "127.0.0.1" in rr._resolve_addrs("localhost")


def test_an_unresolvable_name_is_an_empty_answer_not_an_exception(rr):
    assert rr._resolve_addrs("no-such-host.invalid") == set()
    assert rr._resolve_addrs("") == set()


# ---- the duplicate check sees through a name ------------------------------
def test_a_name_and_its_address_are_recognised_as_one_machine(rr, monkeypatch):
    """The reason this matters. Enrolling 10.0.0.6 when web-01 is already
    managed must be refused -- it is the same box."""
    monkeypatch.setattr(rr, "load_hosts",
                        lambda: {"web-01": {"ip": "web-01", "addr": "10.0.0.6"}})
    assert rr._ip_owner("10.0.0.6") == "web-01"


def test_the_same_machine_by_name_is_also_caught(rr, monkeypatch):
    monkeypatch.setattr(rr, "load_hosts",
                        lambda: {"web-01": {"ip": "10.0.0.6", "addr": "10.0.0.6"}})
    monkeypatch.setattr(rr, "_resolve_addrs",
                        lambda t: {"10.0.0.6"} if t == "web-01" else {t})
    assert rr._ip_owner("web-01") == "web-01"


def test_an_unrelated_machine_is_not_a_duplicate(rr, monkeypatch):
    monkeypatch.setattr(rr, "load_hosts",
                        lambda: {"web-01": {"ip": "web-01", "addr": "10.0.0.6"}})
    assert rr._ip_owner("10.0.0.7") is None


def test_a_record_written_before_this_change_still_matches(rr, monkeypatch):
    """Existing hosts.json entries have no `addr` key at all."""
    monkeypatch.setattr(rr, "load_hosts", lambda: {"old-01": {"ip": "10.0.0.8"}})
    assert rr._ip_owner("10.0.0.8") == "old-01"


# ---- a list of names -------------------------------------------------------
def test_a_bare_list_of_names_is_a_list_of_candidates():
    """The thing that was asked for. No addresses anywhere."""
    rows = hosts_file.parse("web-01\nweb-02\ndb-01\n")
    assert [r["name"] for r in rows] == ["web-01", "web-02", "db-01"]
    assert all(r["enrollable"] for r in rows)
    assert all(r["target"] == r["name"] for r in rows), \
        "a bare name must connect to itself"
    assert all(r["address"] == "" for r in rows)


def test_a_name_and_an_address_pair_still_works():
    rows = hosts_file.parse("10.0.0.5 db.internal db\n")
    assert rows[0]["name"] == "db" and rows[0]["target"] == "10.0.0.5"


def test_the_two_forms_can_be_mixed_in_one_paste():
    rows = hosts_file.parse("web-01\n10.0.0.5 db\n")
    assert [(r["name"], r["target"]) for r in rows] == [
        ("web-01", "web-01"), ("db", "10.0.0.5")]


def test_an_address_on_its_own_is_offered_under_its_own_name():
    """There is nothing else to call it, and the label is editable."""
    rows = hosts_file.parse("10.0.0.9\n")
    assert rows[0]["enrollable"] and rows[0]["name"] == "10.0.0.9"
    assert rows[0]["target"] == "10.0.0.9"


def test_a_bare_fqdn_is_still_shortened_for_the_label():
    rows = hosts_file.parse("web-01.corp.example.com\n")
    assert rows[0]["name"] == "web-01"
    assert rows[0]["target"] == "web-01.corp.example.com", \
        "the SHORT name is a label; the full one is what to connect to"


def test_two_bare_names_that_would_collide_keep_their_full_form():
    rows = hosts_file.parse("web-01.a.example.com\nweb-01.b.example.com\n")
    assert [r["name"] for r in rows] == ["web-01.a.example.com",
                                         "web-01.b.example.com"]


def test_rubbish_is_still_rubbish():
    rows = hosts_file.parse("not a host!\n")
    assert not rows[0]["enrollable"]


def test_the_loopback_block_is_still_skipped():
    rows = hosts_file.parse("127.0.0.1 localhost\n::1 ip6-localhost\n")
    assert not any(r["enrollable"] for r in rows)


# ---- the console sends the right thing ------------------------------------
def test_the_bulk_enroll_posts_the_target_not_the_address():
    """A bare-name row has no address. Posting it would enroll nothing."""
    assert "ip: r.target || r.address" in VIEW, \
        "the bulk path still posts the address, which is empty for a bare name"


def test_the_table_shows_what_will_be_connected_to():
    assert "r.target || r.address" in VIEW
    assert "<th>Connect to</th>" in VIEW, \
        "the column still claims to be an address when it may be a name"
