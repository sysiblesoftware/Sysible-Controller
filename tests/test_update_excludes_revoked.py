"""
Revoked/disenrolled hosts must be excluded from the agent-update surface.

A revoked host keeps its DB row (for history / re-enroll), but its agent_secret
is revoked so it can never poll a task — targeting it for "Update agents" is
pointless and misleading (it showed up as an outdated host to update). Both
GET /update-status and POST /agents/update must skip revoked hosts.
"""
import time

import backend.db as db


def _agent(host_id, version):
    db.create_or_update_agent(host_id, host_id, "linux", "6.1", "online",
                              time.time(), "sec-" + host_id, "10.0.0.1")
    conn = db._connect()
    cur = conn.cursor()
    cur.execute("UPDATE agents SET agent_version=? WHERE host_id=?", (version, host_id))
    conn.commit()
    conn.close()


def test_update_status_excludes_revoked(controller, superuser_headers):
    _agent("live-host", "oldver000000")
    _agent("gone-host", "oldver000000")
    db.revoke_agent("gone-host")     # disenrolled/revoked — keeps the row

    r = controller.get("/update-status", headers=superuser_headers)
    assert r.status_code == 200
    agents = r.json()["agents"]
    ids = {o["host_id"] for o in agents["outdated"]}
    assert "live-host" in ids
    assert "gone-host" not in ids          # revoked host is NOT "outdated"
    assert agents["total"] == 1            # revoked host not counted in the fleet


def test_update_agents_does_not_queue_for_revoked(controller, superuser_headers):
    _agent("live-host", "oldver000000")
    _agent("gone-host", "oldver000000")
    db.revoke_agent("gone-host")

    r = controller.post("/agents/update", headers=superuser_headers)
    assert r.status_code == 200, r.text
    # Only the live host was queued.
    assert r.json().get("queued") == 1

    def _has_task(host_id):
        conn = db._connect()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM agent_tasks WHERE host_id=? AND kind='agent-update'", (host_id,))
        n = cur.fetchone()[0]
        conn.close()
        return n > 0

    assert _has_task("live-host")
    assert not _has_task("gone-host")      # no update task queued for the revoked host
