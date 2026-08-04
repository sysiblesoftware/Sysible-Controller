"""
Re-enrollment duplicate reconciliation + safe SSH cleanup.

Reproduces the reported bug: a host re-enrolled with a FRESH token used to spawn a
second "zombie" record at the same IP, and deleting that zombie wiped the valid
host's SSH record (matched by IP). These tests lock in the fix — re-enroll adopts a
stale, SAME-hostname record, and delete forgets an SSH record only when hostname
AND IP both match the deleted agent.

Security: adoption is deliberately narrow. `ip`/`hostname` are unauthenticated
request-body fields, so a REVOKED record is never adopted/un-revoked by re-enroll
(that would be a revocation bypass), and adoption requires the hostname to match,
never IP alone.
"""
import time

import pytest

import backend.db as db
from backend import remote_routes
from conftest import key_headers


def _enroll(controller, token, host_id, hostname, ip):
    return controller.post("/agents/enroll", json={
        "token": token, "host_id": host_id, "hostname": hostname,
        "platform": "linux", "kernel": "6.1", "ip": ip,
    })


def _agents_at_ip(ip):
    return [a for a in db.list_agents() if (a.get("ip") or "") == ip]


# ---------------------------------------------------------------------------
# Reconciliation: re-enroll with a fresh token adopts a stale SAME-hostname record
# ---------------------------------------------------------------------------
def test_stale_same_host_reenroll_with_fresh_token_is_refused(controller, enroll_token):
    # A FRESH token + a spoofable (hostname, IP) match must NOT silently adopt/re-bind
    # a stale existing record — that is the offline-host takeover (an attacker who can
    # claim a victim's hostname+IP would otherwise seize its identity). It is refused,
    # and — importantly — no duplicate zombie row is created either.
    ip = "192.168.100.23"
    r1 = _enroll(controller, enroll_token(), "rocky-old-id", "rocky-030", ip)
    assert r1.status_code == 200
    old_id = r1.json()["host_id"]

    conn = db._connect(); conn.execute(
        "UPDATE agents SET last_seen=? WHERE host_id=?", (time.time() - 10_000, old_id))
    conn.commit(); conn.close()

    r2 = _enroll(controller, enroll_token(), "rocky-new-random-id", "rocky-030", ip)
    assert r2.status_code == 403
    assert len(_agents_at_ip(ip)) == 1           # refused, and no zombie duplicate


def test_stale_same_host_reenroll_with_reissue_token_adopts(controller, enroll_token):
    # The supported reinstall path: an admin mints a REISSUE token bound to the stale
    # host's id, and re-enrollment then re-binds that one record (no duplicate).
    ip = "192.168.100.24"
    r1 = _enroll(controller, enroll_token(), "rocky-old-id2", "rocky-031", ip)
    assert r1.status_code == 200
    old_id = r1.json()["host_id"]

    conn = db._connect(); conn.execute(
        "UPDATE agents SET last_seen=? WHERE host_id=?", (time.time() - 10_000, old_id))
    conn.commit(); conn.close()

    reissue = "reissue-adopt-1"
    db.create_reissue_token(reissue, old_id)
    r2 = _enroll(controller, reissue, "rocky-new-random-id2", "rocky-031", ip)
    assert r2.status_code == 200
    assert r2.json()["host_id"] == old_id        # re-bound onto the original record
    assert len(_agents_at_ip(ip)) == 1           # exactly one record, no zombie


def test_revoked_host_is_not_resurrected_by_fresh_token_reenroll(controller, enroll_token):
    # SECURITY: a revoked host must NOT be silently un-revoked by anyone who holds a
    # fresh enroll token and claims its hostname/IP — that would defeat revocation.
    ip = "192.168.100.99"
    r1 = _enroll(controller, enroll_token(), "rev-old-id", "revhost", ip)
    assert r1.status_code == 200
    old_id = r1.json()["host_id"]

    db.revoke_agent(old_id)
    conn = db._connect(); conn.execute(
        "UPDATE agents SET last_seen=? WHERE host_id=?", (time.time() - 10_000, old_id))
    conn.commit(); conn.close()

    # Fresh token, same hostname+IP — the revoked record is NOT adopted.
    r2 = _enroll(controller, enroll_token(), "rev-new-id", "revhost", ip)
    assert r2.status_code == 200
    # A distinct new record was created; the original stays revoked (not hijacked).
    assert r2.json()["host_id"] != old_id
    old = db.get_agent(old_id) if hasattr(db, "get_agent") else next(
        (a for a in db.list_agents() if a["host_id"] == old_id), None)
    assert old is not None and old.get("revoked")        # revocation preserved


def test_bound_token_reenroll_of_revoked_host_is_blocked(controller, enroll_token):
    # Re-enrolling a revoked host with its OWN bound token (reuse window) is refused
    # with a clear "re-issue from console" 403 — the admin must deliberately restore it.
    ip = "192.168.100.77"
    tok = enroll_token()
    r1 = _enroll(controller, tok, "bt-id", "bthost", ip)
    assert r1.status_code == 200
    hid = r1.json()["host_id"]

    db.revoke_agent(hid)
    conn = db._connect(); conn.execute(
        "UPDATE agents SET last_seen=? WHERE host_id=?", (time.time() - 10_000, hid))
    conn.commit(); conn.close()

    again = _enroll(controller, tok, "bt-id", "bthost", ip)   # same bound token
    assert again.status_code == 403


def test_empty_hostname_reenroll_adopts_own_stale_record_by_ip(controller, enroll_token):
    # A bastion/minimal host that reports an EMPTY hostname used to spawn a duplicate
    # <uuid> row on re-enroll (the old dedup required a non-empty hostname match), and
    # the original record went offline. The box must adopt its own stale record by IP
    # when it reports no hostname of its own.
    ip = "172.16.5.40"
    r1 = _enroll(controller, enroll_token(), "rocky-community-id", "rocky-community00", ip)
    assert r1.status_code == 200
    old_id = r1.json()["host_id"]

    conn = db._connect(); conn.execute(
        "UPDATE agents SET last_seen=? WHERE host_id=?", (time.time() - 10_000, old_id))
    conn.commit(); conn.close()

    # Re-enroll via bastion with a reissue token bound to the stale id: fresh random
    # host_id, EMPTY hostname, same IP → re-binds the original record, no duplicate.
    # (A plain fresh token here is refused — an empty-hostname/IP claim must not be
    # able to seize an existing record; see test_..._refused above.)
    assert _enroll(controller, enroll_token(), "98647310-eb00-new-random", "", ip).status_code == 403
    reissue = "reissue-empty-1"
    db.create_reissue_token(reissue, old_id)
    r2 = _enroll(controller, reissue, "98647310-eb00-new-random", "", ip)
    assert r2.status_code == 200
    assert r2.json()["host_id"] == old_id          # adopted, not duplicated
    assert len(_agents_at_ip(ip)) == 1             # no second row


def test_empty_hostname_does_not_hijack_live_named_host(controller, enroll_token):
    # SECURITY: an empty-hostname claim must not seize a LIVE record at the same IP.
    ip = "172.16.5.41"
    r1 = _enroll(controller, enroll_token(), "live-named-id", "livenamed", ip)
    assert r1.status_code == 200
    # Still live (no last_seen backdating) — an empty-hostname enroll at this IP
    # must create a distinct record, never adopt the live one.
    r2 = _enroll(controller, enroll_token(), "empty-claimant", "", ip)
    assert r2.status_code == 200
    assert r2.json()["host_id"] != "live-named-id"


def test_live_host_is_not_hijacked_by_same_ip_enroll(controller, enroll_token):
    ip = "10.0.0.50"
    r1 = _enroll(controller, enroll_token(), "live-id", "livehost", ip)
    assert r1.status_code == 200
    # A DIFFERENT machine (different hostname) enrolls at the same IP while the
    # first is still live — must NOT adopt/hijack the live record.
    r2 = _enroll(controller, enroll_token(), "other-id", "otherhost", ip)
    assert r2.status_code == 200
    assert r2.json()["host_id"] != "live-id"     # a distinct record, not a takeover


def test_fresh_first_enrollment_still_works(controller, enroll_token):
    r = _enroll(controller, enroll_token(), "brand-new", "freshhost", "10.0.0.77")
    assert r.status_code == 200 and r.json()["host_id"] == "brand-new"
    assert len(_agents_at_ip("10.0.0.77")) == 1


# ---------------------------------------------------------------------------
# Safe SSH cleanup on delete: a shared-IP sibling's record survives
# ---------------------------------------------------------------------------
def test_deleting_same_ip_zombie_keeps_valid_hosts_ssh_record(controller, superuser_headers, agent):
    ip = "192.168.100.23"
    # A leftover zombie + the valid host at the same IP. Only ONE SSH record exists
    # for the IP (register_agent_ssh_host refuses a second at an owned IP), and here
    # it belongs to the VALID host. The old delete keyed on IP and would wrongly
    # wipe it when the zombie was removed.
    agent(host_id="zombie-id", secret="s1", hostname="rocky-030", ip=ip)
    agent(host_id="valid-id", secret="s2", hostname="rocky-0300", ip=ip)
    remote_routes.register_agent_ssh_host("rocky-0300", ip)

    r = controller.delete("/agents/zombie-id", headers=superuser_headers)
    assert r.status_code == 200

    hosts = remote_routes.load_hosts()
    assert "rocky-0300" in hosts           # the VALID host's SSH record survives the zombie delete


def test_revoked_host_deletes_via_endpoint(controller, superuser_headers, agent):
    # The reported symptom: a revoked, stale host won't delete. Confirm the DELETE
    # endpoint removes it outright (no teardown wait, no revoked-guard blocking it).
    import time
    hid, _ = agent(host_id="rev-id", secret="s", hostname="rocky-030", ip="192.168.100.23")
    import backend.db as _db
    _db.revoke_agent(hid)
    conn = _db._connect(); conn.execute(
        "UPDATE agents SET last_seen=? WHERE host_id=?", (time.time() - 9000, hid))
    conn.commit(); conn.close()

    r = controller.delete(f"/agents/{hid}", headers=superuser_headers)
    assert r.status_code == 200 and r.json()["status"] == "removed"
    assert all(a["host_id"] != hid for a in _db.list_agents())   # actually gone


def test_force_delete_purges_token_so_zombie_cant_resurrect(controller, superuser_headers, enroll_token):
    # A zombie agent whose token is still baked into sysible_agent.env would
    # re-enroll onto the same host_id after deletion. Force Delete purges that
    # bound token so re-enrollment is refused and the record stays gone.
    tok = enroll_token()
    r = _enroll(controller, tok, "zh1", "zhost", "10.9.9.9")
    assert r.status_code == 200
    hid = r.json()["host_id"]

    d = controller.delete(f"/agents/{hid}?purge_token=1", headers=superuser_headers)
    assert d.status_code == 200 and d.json()["tokens_purged"] >= 1

    # The same token can no longer re-enroll (would-be resurrection is refused).
    again = _enroll(controller, tok, "zh1-new", "zhost", "10.9.9.9")
    assert again.status_code == 403


def test_normal_delete_preserves_token_reuse(controller, superuser_headers, enroll_token):
    # A normal (non-force) disenroll keeps the 7-day token-reuse convenience.
    tok = enroll_token()
    r = _enroll(controller, tok, "nh1", "nhost", "10.9.9.10")
    hid = r.json()["host_id"]
    d = controller.delete(f"/agents/{hid}", headers=superuser_headers)
    assert d.status_code == 200 and d.json().get("tokens_purged", 0) == 0
    # Token still valid → the host can come back without a fresh bundle.
    again = _enroll(controller, tok, "nh1-new", "nhost", "10.9.9.10")
    assert again.status_code == 200


def test_deleting_sole_host_at_ip_forgets_its_ssh_record(controller, superuser_headers, agent):
    ip = "10.0.0.99"
    agent(host_id="only-id", secret="s", hostname="onlyhost", ip=ip)
    remote_routes.register_agent_ssh_host("onlyhost", ip)
    r = controller.delete("/agents/only-id", headers=superuser_headers)
    assert r.status_code == 200
    assert "onlyhost" not in remote_routes.load_hosts()   # fully cleaned up
