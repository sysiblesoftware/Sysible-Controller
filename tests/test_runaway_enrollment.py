"""Runaway-enrollment prevention.

A host whose agent can't persist its state (read-only/ephemeral state dir) used to
mint a fresh random uuid host_id every restart and, under systemd Restart=always,
re-enroll in a tight loop — filling the console with one <uuid> row per cycle.

Two defenses:
  1. The agent now derives a STABLE, machine-based host_id when it has no saved
     one, so every restart re-enrolls onto the SAME row.
  2. Even given a stable host_id and a fresh token each cycle, the controller keys
     the inventory on host_id, so a re-enroll updates the one row (or is blocked
     while live) — never a duplicate.
"""
import sys
import time

import backend.db as db


def _stable_id_mod():
    sys.path.insert(0, "host_agent")
    import agent  # noqa: E402
    return agent


def test_agent_stable_host_id_is_deterministic():
    import uuid
    agent = _stable_id_mod()
    a = agent._stable_host_id()
    b = agent._stable_host_id()
    assert a == b, "a state-less restart must regenerate the SAME id, not a new one"
    uuid.UUID(a)  # well-formed uuid


def _enroll(controller, token, host_id, hostname="web1", ip="10.0.0.5"):
    return controller.post("/agents/enroll", json={
        "token": token, "host_id": host_id, "hostname": hostname,
        "platform": "linux", "kernel": "6.1", "ip": ip})


def _rows(host_id):
    return [a for a in db.list_agents() if a["host_id"] == host_id]


def test_same_stable_host_id_new_token_does_not_duplicate(controller, enroll_token):
    hid = "stable-host-abc"
    r1 = _enroll(controller, enroll_token(), hid)
    assert r1.status_code == 200, r1.text
    assert len(_rows(hid)) == 1

    # Simulate a real restart gap so the host is no longer "live".
    conn = db._connect()
    conn.execute("UPDATE agents SET last_seen=? WHERE host_id=?",
                 (time.time() - 10_000, hid))
    conn.commit(); conn.close()

    # A fresh token + the SAME stable host_id (state-less restart) must reuse the
    # one row, not create a second.
    r2 = _enroll(controller, enroll_token(), hid)
    assert r2.status_code == 200, r2.text
    assert len(_rows(hid)) == 1, "a stable host_id must never spawn a duplicate row"


def test_live_stable_host_reenroll_is_blocked_not_duplicated(controller, enroll_token):
    hid = "stable-host-live"
    assert _enroll(controller, enroll_token(), hid).status_code == 200
    # Still live (just enrolled) → a re-enroll is refused, and crucially adds no row.
    r2 = _enroll(controller, enroll_token(), hid)
    assert r2.status_code == 409
    assert len(_rows(hid)) == 1


def test_token_is_bound_before_row_exists(controller, enroll_token):
    # The token is consumed/bound as part of a successful enroll, so a retry with
    # the same token resolves back to the same host_id rather than spawning a new
    # row (the ordering that closes the duplicate path).
    hid = "stable-host-bind"
    tok = enroll_token()
    assert _enroll(controller, tok, hid).status_code == 200
    assert db.resolve_enroll_token_host(tok, "some-other-id") == hid
