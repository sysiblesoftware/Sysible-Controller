"""The controller notices its own address died, and moves onto the live one.

The failure this exists for: the host's network dropped, it came back on a
different subnet, and the controller went on advertising the dead address. The
check that should have caught it (db.get_controller_config's `ip_stale`) cannot
fire in a container, because detect_local_ips() there returns only
SYSIBLE_CONTROLLER_ADDR — baked in at create time with the old address — so the
saved IP always equals "detected".

These cover the policy, which is where the risk is. Healing onto the wrong
address strands every agent at once, so the bar for replacing one is deliberately
high, and most of this file is about refusing to move.
"""
import time

import pytest

from backend import self_address as sa


def _seen(store, addr, peers, count, first_offset, last_offset, now):
    store[addr] = {"first": now - first_offset, "last": now - last_offset,
                   "count": count, "peers": list(peers)}


def _earned(store, addr, now, last=0):
    """A candidate that clears every bar."""
    _seen(store, addr, [f"10.9.0.{i}" for i in range(sa.MIN_SOURCES)],
          sa.MIN_SIGHTINGS, sa.MIN_SPREAD + 60, last, now)


# ----------------------------------------------------------------- normalising
@pytest.mark.parametrize("raw,want", [
    ("192.168.1.40", "192.168.1.40"),
    ("192.168.1.40:9000", "192.168.1.40"),
    ("[2001:db8::5]:9000", "2001:db8::5"),
    ("2001:db8::5", "2001:db8::5"),
    ("controller.example.com", ""),      # a name is never an address here
    ("controller.example.com:9000", ""),
    ("", ""),
])
def test_only_ip_literals_are_addresses(raw, want):
    assert sa._normalise(raw) == want


def test_a_hostname_can_never_become_the_advertised_address():
    """Bundles are IP-only by design (agent_bundle): a name assumes DNS is set up
    on every managed host. Healing onto one would reintroduce exactly that."""
    store = {}
    sa.observe(store, "controller.example.com", "10.0.0.1")
    assert store == {}


@pytest.mark.parametrize("addr,ok", [
    ("192.168.1.40", True),     # LAN
    ("203.0.113.9", True),      # public / NAT front door
    ("127.0.0.1", False),       # the operator on the box itself
    ("169.254.12.3", False),    # DHCP failed — the fault we are recovering from
    ("0.0.0.0", False),
    ("224.0.0.1", False),
])
def test_what_counts_as_reachable_by_the_fleet(addr, ok):
    assert sa.is_routable_for_fleet(addr) is ok


def test_link_local_is_never_a_candidate():
    """A 169.254 address means DHCP failed. Moving onto it would turn a
    recoverable outage into an unreachable controller."""
    now = time.time()
    store = {}
    for i in range(sa.MIN_SIGHTINGS):
        sa.observe(store, "169.254.1.7", f"10.9.0.{i % 4}", now=now)
    assert store == {}


# --------------------------------------------------------------------- holding
def test_a_live_address_is_left_alone():
    now = time.time()
    store = {}
    _seen(store, "192.168.1.22", ["10.0.0.5"], 3, 600, 30, now)
    v = sa.assess(store, "192.168.1.22", now=now)
    assert v["stale"] is False and v["candidate"] == ""


def test_silence_alone_does_not_move_the_address():
    """The controller may simply be idle — nobody has asked it anything. Nothing
    may be replaced until something else has demonstrably answered."""
    now = time.time()
    store = {}
    _seen(store, "192.168.1.22", ["10.0.0.5"], 50, 9000, sa.STALE_AFTER + 60, now)
    v = sa.assess(store, "192.168.1.22", now=now)
    assert v["stale"] is True
    assert v["candidate"] == "", v["reason"]


def test_one_stray_request_cannot_move_the_address():
    now = time.time()
    store = {}
    _seen(store, "192.168.1.22", ["10.0.0.5"], 50, 9000, sa.STALE_AFTER + 60, now)
    _seen(store, "192.168.1.40", ["10.0.0.9"], 1, 0, 0, now)
    assert sa.assess(store, "192.168.1.22", now=now)["candidate"] == ""


def test_one_chatty_client_is_one_opinion_not_fifty():
    """A single host retrying in a loop clears MIN_SIGHTINGS on its own. Distinct
    peers are what make it evidence."""
    now = time.time()
    store = {}
    _seen(store, "192.168.1.22", ["10.0.0.5"], 50, 9000, sa.STALE_AFTER + 60, now)
    _seen(store, "192.168.1.40", ["10.0.0.9"], sa.MIN_SIGHTINGS * 5,
          sa.MIN_SPREAD + 60, 0, now)
    assert sa.assess(store, "192.168.1.22", now=now)["candidate"] == ""


def test_a_brief_burst_is_not_enough():
    """Seen often, by several peers, but all inside a few seconds — that is one
    event, not a settled new address."""
    now = time.time()
    store = {}
    _seen(store, "192.168.1.22", ["10.0.0.5"], 50, 9000, sa.STALE_AFTER + 60, now)
    _seen(store, "192.168.1.40", ["10.0.0.9", "10.0.0.10"], sa.MIN_SIGHTINGS * 3,
          sa.MIN_SPREAD - 30, 0, now)
    assert sa.assess(store, "192.168.1.22", now=now)["candidate"] == ""


def test_a_candidate_that_has_itself_gone_quiet_is_not_used():
    now = time.time()
    store = {}
    _seen(store, "192.168.1.22", ["10.0.0.5"], 50, 99999, sa.STALE_AFTER + 60, now)
    _earned(store, "192.168.1.40", now, last=sa.STALE_AFTER + 120)
    assert sa.assess(store, "192.168.1.22", now=now)["candidate"] == ""


def test_nothing_happens_when_no_address_is_advertised():
    now = time.time()
    store = {}
    _earned(store, "192.168.1.40", now)
    v = sa.assess(store, "", now=now)
    assert v["stale"] is False and v["candidate"] == ""


# --------------------------------------------------------------------- moving
def test_it_moves_once_a_candidate_has_earned_it():
    now = time.time()
    store = {}
    _seen(store, "192.168.1.22", ["10.0.0.5"], 50, 99999, sa.STALE_AFTER + 60, now)
    _earned(store, "192.168.1.40", now)
    v = sa.assess(store, "192.168.1.22", now=now)
    assert v["stale"] is True
    assert v["candidate"] == "192.168.1.40", v["reason"]
    assert "192.168.1.40" in v["reason"] and "192.168.1.22" in v["reason"]


def test_an_address_never_seen_at_all_still_needs_a_real_candidate():
    """First boot after the address was typed in by hand: it has no sighting. That
    is not evidence it is dead, so the same bar applies to replacing it."""
    now = time.time()
    store = {}
    assert sa.assess(store, "192.168.1.22", now=now)["candidate"] == ""
    _earned(store, "192.168.1.40", now)
    assert sa.assess(store, "192.168.1.22", now=now)["candidate"] == "192.168.1.40"


def test_the_busiest_qualifying_candidate_wins():
    now = time.time()
    store = {}
    _seen(store, "192.168.1.22", ["10.0.0.5"], 50, 99999, sa.STALE_AFTER + 60, now)
    _earned(store, "192.168.1.40", now)
    _seen(store, "10.1.1.7", ["10.0.0.9", "10.0.0.10"], sa.MIN_SIGHTINGS * 9,
          sa.MIN_SPREAD + 600, 0, now)
    assert sa.assess(store, "192.168.1.22", now=now)["candidate"] == "10.1.1.7"


# ------------------------------------------------------------------ the tally
def test_observing_counts_sightings_and_distinct_peers():
    now = time.time()
    store = {}
    for i in range(5):
        sa.observe(store, "192.168.1.40:9000", "10.0.0.1", now=now)
    sa.observe(store, "192.168.1.40", "10.0.0.2", now=now)
    assert store["192.168.1.40"]["count"] == 6
    assert store["192.168.1.40"]["peers"] == ["10.0.0.1", "10.0.0.2"]


def test_the_tally_is_bounded():
    """A controller reached through many addresses, or scanned, must not grow this
    without limit — it is persisted on every cycle."""
    now = time.time()
    store = {}
    for i in range(sa.MAX_TRACKED + 40):
        sa.observe(store, f"10.{i // 256}.{i % 256}.5", "10.0.0.1", now=now + i)
    assert len(store) <= sa.MAX_TRACKED
    for i in range(30):
        sa.observe(store, "192.168.1.40", f"10.0.0.{i}", now=now)
    assert len(store["192.168.1.40"]["peers"]) <= 16


def test_old_sightings_are_forgotten():
    now = time.time()
    store = {}
    _seen(store, "192.168.1.22", ["10.0.0.5"], 5, 0, sa.FORGET_AFTER + 60, now)
    _seen(store, "192.168.1.40", ["10.0.0.5"], 5, 0, 10, now)
    sa.prune(store, now=now)
    assert list(store) == ["192.168.1.40"]


# ------------------------------------------------------- against the real thing
class _TLS:
    """Stands in for backend.tls_manager."""
    def __init__(self, self_signed=True, fail=False):
        self._ss, self._fail, self.calls = self_signed, fail, []

    def current_is_self_signed(self):
        return self._ss

    def regenerate_self_signed(self, hostnames=None, ips=None, days=3650):
        if self._fail:
            raise RuntimeError("no key material")
        self.calls.append({"hostnames": hostnames, "ips": ips})
        return {"ok": True}


def test_healing_writes_the_new_address_and_reissues_the_certificate():
    from backend import db
    db.init_db()
    db.set_controller_config("", "192.168.1.22", "ip", 9000)
    tls = _TLS()

    out = sa.heal(db, tls, db.get_controller_config(), "192.168.1.40")

    assert out["healed"] is True
    assert db.get_controller_config()["ip"] == "192.168.1.40"
    assert tls.calls == [{"hostnames": ["localhost"], "ips": ["192.168.1.40"]}]


def test_a_failed_reissue_leaves_the_address_alone():
    """Advertising an address the certificate does not name is worse than the dead
    one: every agent then fails the TLS check instead of just dialling nowhere."""
    from backend import db
    db.init_db()
    db.set_controller_config("", "192.168.1.22", "ip", 9000)

    out = sa.heal(db, _TLS(fail=True), db.get_controller_config(), "192.168.1.40")

    assert out["healed"] is False
    assert db.get_controller_config()["ip"] == "192.168.1.22"
    assert "could not reissue TLS" in out["note"]


def test_an_operator_installed_certificate_is_never_silently_replaced():
    from backend import db
    db.init_db()
    db.set_controller_config("", "192.168.1.22", "ip", 9000)
    tls = _TLS(self_signed=False)

    out = sa.heal(db, tls, db.get_controller_config(), "192.168.1.40")

    assert out["healed"] is True
    assert tls.calls == [], "it reissued a certificate the operator installed"
    assert "reissue it for 192.168.1.40" in out["note"]


def test_the_tally_survives_a_restart():
    from backend import db
    db.init_db()
    store = {}
    sa.observe(store, "192.168.1.40", "10.0.0.1")
    sa.save(db, store)
    assert sa.load(db)["192.168.1.40"]["count"] == 1


def test_the_console_is_told_what_the_fleet_is_reaching_us_at(controller, superuser_headers):
    """A request through the API is itself a sighting, and the config endpoint
    reports it — so an operator can see the drift before anything moves."""
    from backend import app as app_mod
    app_mod._addr_sightings.clear()

    controller.get("/controller-config", headers={**superuser_headers,
                                                  "Host": "192.168.1.40:9000",
                                                  "X-Forwarded-For": "10.0.0.9"})
    body = controller.get("/controller-config", headers={**superuser_headers,
                                                         "Host": "192.168.1.40:9000",
                                                         "X-Forwarded-For": "10.0.0.9"}).json()

    seen = {o["address"] for o in body["observed_addresses"]}
    assert "192.168.1.40" in seen, body["observed_addresses"]
    assert body["address_repair"] is None
