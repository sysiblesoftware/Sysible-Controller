"""Enrollment pause — the runaway kill-switch.

When an admin pauses enrollment, /agents/enroll refuses every new host so a
crash-looping/re-provisioning agent can't keep spawning rows faster than they're
deleted. Existing agents are unaffected; resuming restores normal enrollment.
"""
import backend.db as db
from tests.conftest import key_headers


def _enroll(controller, token, host_id="pause-host", hostname="web1", ip="10.0.0.7"):
    return controller.post("/agents/enroll", json={
        "token": token, "host_id": host_id, "hostname": hostname,
        "platform": "linux", "kernel": "6.1", "ip": ip})


def test_default_not_paused():
    assert db.get_enrollment_paused() is False
    assert db.get_enrollment_control()["paused"] is False


def test_pause_blocks_enroll_then_resume_allows(controller, enroll_token):
    db.set_enrollment_paused(True, actor="admin")
    r = _enroll(controller, enroll_token())
    assert r.status_code == 503 and "paused" in r.text.lower()

    db.set_enrollment_paused(False, actor="admin")
    r2 = _enroll(controller, enroll_token())
    assert r2.status_code == 200, r2.text


def test_routes_roundtrip_and_gate(controller, superuser_headers, sysadmin_headers):
    # Superuser can read + toggle.
    r = controller.get("/admin/enrollment-pause", headers=superuser_headers)
    assert r.status_code == 200 and r.json()["paused"] is False
    r = controller.post("/admin/enrollment-pause", headers=superuser_headers, json={"paused": True})
    assert r.status_code == 200 and r.json()["paused"] is True
    assert db.get_enrollment_paused() is True
    # A sysadmin (non-superuser) cannot toggle it.
    r = controller.post("/admin/enrollment-pause", headers=sysadmin_headers, json={"paused": False})
    assert r.status_code == 403
    # Still paused after the rejected call.
    assert db.get_enrollment_paused() is True
