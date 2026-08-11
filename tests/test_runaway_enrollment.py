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


def _go_offline(hid):
    conn = db._connect()
    conn.execute("UPDATE agents SET last_seen=? WHERE host_id=?",
                 (time.time() - 10_000, hid))
    conn.commit(); conn.close()


def test_fresh_token_cannot_rebind_existing_offline_host(controller, enroll_token):
    # A FRESH bearer token + the (machine-derivable, non-secret) host_id of an
    # existing host must NOT silently re-bind it, even when that host is offline —
    # that is the offline-host takeover. It is refused (403) with no duplicate and,
    # crucially, without overwriting the incumbent's agent_secret.
    hid = "stable-host-abc"
    assert _enroll(controller, enroll_token(), hid).status_code == 200
    assert len(_rows(hid)) == 1
    secret_before = db.get_agent_secret(hid)

    _go_offline(hid)

    r2 = _enroll(controller, enroll_token(), hid)
    assert r2.status_code == 403, r2.text
    assert len(_rows(hid)) == 1, "refused re-bind must never spawn a duplicate row"
    assert db.get_agent_secret(hid) == secret_before, "incumbent secret must be intact"


def test_reissue_token_authorizes_rebind_of_existing_host(controller, enroll_token):
    # The supported reinstall path: an admin mints a REISSUE token bound to the
    # existing host_id, and enrollment re-binds exactly that host onto its one row.
    hid = "stable-host-reissue"
    assert _enroll(controller, enroll_token(), hid).status_code == 200
    _go_offline(hid)

    reissue = "reissue-tok-1"
    db.create_reissue_token(reissue, hid)
    r = _enroll(controller, reissue, hid)
    assert r.status_code == 200, r.text
    assert len(_rows(hid)) == 1, "reissue must re-bind the one row, not duplicate"


def test_reissue_token_cannot_be_redirected_to_another_host(controller, enroll_token):
    # A reissue token is BOUND to one host_id at generation, so an attacker holding
    # a reissue token for `other` cannot point it at `victim`: resolve pins the
    # request onto `other`, leaving victim's identity untouched.
    victim = "stable-host-victim"
    other = "stable-host-other"
    assert _enroll(controller, enroll_token(), victim).status_code == 200
    assert _enroll(controller, enroll_token(), other).status_code == 200
    victim_secret = db.get_agent_secret(victim)
    _go_offline(victim)

    reissue_for_other = "reissue-tok-2"
    db.create_reissue_token(reissue_for_other, other)   # bound to `other`, not victim
    # Attacker asks for victim's host_id but presents a reissue token for `other`.
    _enroll(controller, reissue_for_other, victim)
    # Whatever happened to `other`, victim's credential must be untouched.
    assert db.get_agent_secret(victim) == victim_secret, "victim must not be taken over"


def test_prev_agent_secret_authorizes_rebind(controller, enroll_token):
    # Proof of possession: a host that still holds its current agent_secret can
    # re-enroll onto its own row (e.g. a deliberate secret refresh).
    hid = "stable-host-pop"
    resp = _enroll(controller, enroll_token(), hid)
    assert resp.status_code == 200
    secret = resp.json()["agent_secret"]      # the RAW secret the agent holds
    stored_hash = db.get_agent_secret(hid)    # the SHA-256 kept at rest

    # Pass-the-hash regression: the at-rest HASH must NOT authorize a rebind, so a
    # leaked DB snapshot (or a SQL read primitive) can't be replayed as the agent
    # credential. Only the raw secret the agent holds may prove possession.
    _go_offline(hid)
    r_hash = controller.post("/agents/enroll", json={
        "token": enroll_token(), "host_id": hid, "hostname": "web1",
        "platform": "linux", "kernel": "6.1", "ip": "10.0.0.5",
        "prev_agent_secret": stored_hash})
    assert r_hash.status_code == 403, r_hash.text

    # The raw secret the agent actually received at enrollment DOES authorize it.
    _go_offline(hid)
    r = controller.post("/agents/enroll", json={
        "token": enroll_token(), "host_id": hid, "hostname": "web1",
        "platform": "linux", "kernel": "6.1", "ip": "10.0.0.5",
        "prev_agent_secret": secret})
    assert r.status_code == 200, r.text
    assert len(_rows(hid)) == 1

    # A WRONG secret does not authorize the re-bind.
    _go_offline(hid)
    r2 = controller.post("/agents/enroll", json={
        "token": enroll_token(), "host_id": hid, "hostname": "web1",
        "platform": "linux", "kernel": "6.1", "ip": "10.0.0.5",
        "prev_agent_secret": "not-the-real-secret"})
    assert r2.status_code == 403, r2.text


def test_live_stable_host_reenroll_is_blocked_not_duplicated(controller, enroll_token):
    hid = "stable-host-live"
    assert _enroll(controller, enroll_token(), hid).status_code == 200
    # This IS the fresh-token takeover attempt: a FRESH token + the (non-secret,
    # machine-derivable) host_id of a still-LIVE host. It must be refused — otherwise a
    # token holder could overwrite the live host's agent secret and hijack it. A genuine
    # restart re-enrolls fine once the old agent goes stale (that path isn't blocked).
    r2 = _enroll(controller, enroll_token(), hid)
    assert r2.status_code == 409
    assert len(_rows(hid)) == 1              # never a duplicate


def test_token_is_bound_before_row_exists(controller, enroll_token):
    # The token is consumed/bound as part of a successful enroll, so a retry with
    # the same token resolves back to the same host_id rather than spawning a new
    # row (the ordering that closes the duplicate path).
    hid = "stable-host-bind"
    tok = enroll_token()
    assert _enroll(controller, tok, hid).status_code == 200
    assert db.resolve_enroll_token_host(tok, "some-other-id") == hid
