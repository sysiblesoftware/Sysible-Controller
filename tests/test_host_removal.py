"""Host removal / zombie-agent lockout. Deleting a host's record must drop it
from the console AND invalidate the agent secret, so a zombie agent (a broken
build that keeps heartbeating) is locked out on its next heartbeat even if its
process is still running on the host."""
import time

import backend.db as db

from conftest import key_headers


class TestHostRemoval:
    def test_delete_removes_the_record(self, controller, superuser_headers, agent):
        host_id, _ = agent()
        assert db.agent_exists(host_id)
        r = controller.delete(f"/agents/{host_id}", headers=superuser_headers)
        assert r.status_code == 200
        assert not db.agent_exists(host_id)

    def test_delete_locks_out_a_zombie_that_keeps_heartbeating(self, controller, superuser_headers, agent):
        # A "live" host (just heartbeated) is removed; the still-running agent's
        # next heartbeat with its old secret is rejected — it can't act anymore.
        host_id, secret = agent()
        assert controller.post("/agents/heartbeat",
                               json={"host_id": host_id, "agent_secret": secret}).status_code == 200
        controller.delete(f"/agents/{host_id}", headers=superuser_headers)
        r = controller.post("/agents/heartbeat",
                            json={"host_id": host_id, "agent_secret": secret})
        assert r.status_code in (401, 403, 404)  # zombie rejected (record + secret gone)
        # And it cannot re-create its record by heartbeating (only enroll can).
        assert not db.agent_exists(host_id)

    def test_delete_requires_superuser(self, controller, sysadmin_headers, auditor_headers, agent):
        host_id, _ = agent()
        assert controller.delete(f"/agents/{host_id}", headers=sysadmin_headers).status_code == 403
        assert controller.delete(f"/agents/{host_id}", headers=auditor_headers).status_code == 403
        assert db.agent_exists(host_id)  # still there — neither could remove it

    def test_delete_unknown_host_is_safe(self, controller, superuser_headers):
        r = controller.delete("/agents/does-not-exist", headers=superuser_headers)
        assert r.status_code in (200, 404)  # idempotent-ish, never a 500
