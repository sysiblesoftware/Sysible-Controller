"""
Reserved/unsafe host_id rejection at enrollment + in-place revoked-host Restore.
"""
import backend.db as db
from conftest import key_headers


def test_enroll_rejects_reserved_host_id(controller, enroll_token):
    r = controller.post("/agents/enroll", json={
        "token": enroll_token(), "host_id": "*", "hostname": "h",
        "platform": "linux", "kernel": "6.1", "ip": "10.7.7.7",
    })
    assert r.status_code == 400


def test_enroll_rejects_unsafe_host_id(controller, enroll_token):
    r = controller.post("/agents/enroll", json={
        "token": enroll_token(), "host_id": "a b;rm", "hostname": "h",
        "platform": "linux", "kernel": "6.1", "ip": "10.7.7.8",
    })
    assert r.status_code == 400


def test_restore_unrevokes_in_place(controller, agent, superuser_headers):
    host_id, secret = agent(host_id="rv", secret="s", hostname="h", ip="10.8.8.8")
    db.revoke_agent(host_id)
    assert db.is_agent_revoked(host_id) is True
    r = controller.post(f"/agents/{host_id}/restore", headers=superuser_headers)
    assert r.status_code == 200 and r.json()["status"] == "restored"
    assert db.is_agent_revoked(host_id) is False
    # The still-installed agent (same secret) can talk again.
    hb = controller.post("/agents/heartbeat",
                         json={"host_id": host_id, "agent_secret": secret})
    assert hb.status_code == 200
