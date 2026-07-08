"""
Re-enrollment duplicate reconciliation + safe SSH cleanup.

Reproduces the reported bug: a host that is revoked and then re-enrolled with a
FRESH token used to spawn a second "zombie" record at the same IP, and deleting
that zombie wiped the valid host's SSH record (matched by IP). These tests lock in
the fix — re-enroll adopts the existing record, and delete only forgets a shared-IP
SSH record when no sibling still uses it.
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
# Reconciliation: re-enroll with a fresh token adopts the existing record
# ---------------------------------------------------------------------------
def test_revoked_host_reenroll_with_new_token_adopts_not_duplicates(controller, enroll_token):
    ip = "192.168.100.23"
    # First enrollment.
    r1 = _enroll(controller, enroll_token(), "rocky-old-id", "rocky-030", ip)
    assert r1.status_code == 200
    old_id = r1.json()["host_id"]

    # Admin revokes it, and it goes stale (no longer heartbeating).
    db.revoke_agent(old_id)
    conn = db._connect(); conn.execute(
        "UPDATE agents SET last_seen=? WHERE host_id=?", (time.time() - 10_000, old_id))
    conn.commit(); conn.close()

    # The box reinstalls: brand-new random host_id, a FRESH token, same IP.
    r2 = _enroll(controller, enroll_token(), "rocky-new-random-id", "rocky-0300", ip)
    assert r2.status_code == 200
    # It adopted the ORIGINAL record instead of creating a zombie.
    assert r2.json()["host_id"] == old_id
    at_ip = _agents_at_ip(ip)
    assert len(at_ip) == 1                       # exactly one record, no zombie
    assert not at_ip[0].get("revoked")           # revocation cleared by the re-enroll
    assert at_ip[0]["hostname"] == "rocky-0300"  # updated to the new reported name


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


def test_deleting_sole_host_at_ip_forgets_its_ssh_record(controller, superuser_headers, agent):
    ip = "10.0.0.99"
    agent(host_id="only-id", secret="s", hostname="onlyhost", ip=ip)
    remote_routes.register_agent_ssh_host("onlyhost", ip)
    r = controller.delete("/agents/only-id", headers=superuser_headers)
    assert r.status_code == 200
    assert "onlyhost" not in remote_routes.load_hosts()   # fully cleaned up
