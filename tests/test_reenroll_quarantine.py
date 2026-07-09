"""
Regression: re-enrolling a host must NOT leave it auto-quarantined.

The integrity baseline is keyed by host_id and survives disenroll. A re-enroll
reuses the same host_id with a freshly-installed agent whose self-measurement
legitimately differs (the bundle patches the controller address into agent.py at
build time, and the agent may be a newer build). Before the fix, the very next
heartbeat compared the new measurement against the STALE baseline and quarantined
a perfectly healthy re-enrolled host. Enroll (and both disenroll paths) now drop
the baseline so the next heartbeat re-seals.
"""
import time

import backend.db as db
from backend import agent_integrity


def _enroll(controller, token, host_id, hostname, ip):
    return controller.post("/agents/enroll", json={
        "token": token, "host_id": host_id, "hostname": hostname,
        "platform": "linux", "kernel": "6.1", "ip": ip,
    })


def _go_stale(host_id):
    conn = db._connect()
    conn.execute("UPDATE agents SET last_seen=? WHERE host_id=?",
                 (time.time() - 10_000, host_id))
    conn.commit(); conn.close()


def test_reenroll_clears_stale_quarantine(controller, enroll_token):
    ip = "10.9.9.9"
    r1 = _enroll(controller, enroll_token(), "rocky-id", "rocky-1", ip)
    assert r1.status_code == 200
    host_id = r1.json()["host_id"]

    # Seal a baseline (first heartbeat), then a NEW measurement quarantines it.
    agent_integrity.evaluate(host_id, {"version": "v1", "manifest": "aaa"})
    agent_integrity.evaluate(host_id, {"version": "v2", "manifest": "bbb"})
    assert agent_integrity.is_quarantined(host_id)

    # Host goes stale, then re-enrolls on the SAME id with a fresh token.
    _go_stale(host_id)
    r2 = _enroll(controller, enroll_token(), "rocky-id", "rocky-1", ip)
    assert r2.status_code == 200
    assert r2.json()["host_id"] == host_id

    # The stale baseline was dropped: it is no longer quarantined, and the next
    # heartbeat with the NEW measurement re-seals cleanly (not a mismatch).
    assert not agent_integrity.is_quarantined(host_id)
    res = agent_integrity.evaluate(host_id, {"version": "v2", "manifest": "bbb"})
    assert res["status"] == "ok"
    assert not agent_integrity.is_quarantined(host_id)


def test_disenroll_forgets_baseline(controller, enroll_token):
    # Console disenroll (DELETE /agents/{id}) must also forget the baseline, so a
    # future re-enroll on the same id starts clean even without the enroll re-seal.
    ip = "10.9.9.10"
    r1 = _enroll(controller, enroll_token(), "h2-id", "h2", ip)
    host_id = r1.json()["host_id"]
    agent_integrity.evaluate(host_id, {"version": "v1"})
    assert host_id in agent_integrity._load()

    from conftest import key_headers
    r = controller.delete(f"/agents/{host_id}", headers=key_headers())
    assert r.status_code == 200
    assert host_id not in agent_integrity._load()


def test_self_disenroll_forgets_baseline(controller, enroll_token, agent):
    # Agent-driven self-disenroll must forget the baseline too.
    host_id, secret = agent(host_id="sd-1", secret="sekret", hostname="sd", ip="10.9.9.11")
    agent_integrity.evaluate(host_id, {"version": "v1"})
    assert host_id in agent_integrity._load()

    r = controller.post(f"/agents/{host_id}/disenroll",
                        json={"host_id": host_id, "agent_secret": secret})
    assert r.status_code == 200
    assert host_id not in agent_integrity._load()
