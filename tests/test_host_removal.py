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


class TestForceDelete:
    """Force Delete (purge_token=1) is the escape hatch for a ZOMBIE agent whose
    enroll token is baked into its env: a plain delete leaves that token valid, so
    the zombie just re-enrolls onto the same host_id and the record you deleted
    reappears. Force Delete must ALSO purge the bound token so removal sticks."""

    def _bind_token_to_host(self, host_id="host-1", secret="agent-secret-1"):
        tok = "enroll-" + "a" * 24
        db.create_enroll_token(tok)
        db.consume_enroll_token(tok, host_id)  # simulate a completed enroll
        db.create_or_update_agent(host_id, "web1", "linux", "6.1", "online",
                                  time.time(), secret, "10.0.0.11")
        return tok

    def test_force_delete_purges_the_bound_token(self, controller, superuser_headers):
        host_id = "host-1"
        tok = self._bind_token_to_host(host_id)
        assert db.validate_enroll_token(tok)          # token is live before

        r = controller.delete(f"/agents/{host_id}?purge_token=1", headers=superuser_headers)
        assert r.status_code == 200
        assert r.json().get("tokens_purged") == 1     # the route reports the purge
        assert not db.agent_exists(host_id)           # record gone
        assert not db.validate_enroll_token(tok)      # token gone -> can't re-enroll

    def test_plain_delete_leaves_the_token(self, controller, superuser_headers):
        # Contrast: a NON-force delete removes the record but deliberately keeps
        # the token (a graceful disenroll expects to re-enroll the same bundle).
        host_id = "host-2"
        tok = self._bind_token_to_host(host_id)
        r = controller.delete(f"/agents/{host_id}", headers=superuser_headers)
        assert r.status_code == 200
        assert not db.agent_exists(host_id)
        assert db.validate_enroll_token(tok)          # token still usable

    def test_force_delete_blocks_zombie_reenroll(self, controller, superuser_headers):
        # End-to-end: after a Force Delete, replaying the zombie's baked-in token
        # against /agents/enroll is rejected — the record cannot come back.
        host_id = "host-3"
        tok = self._bind_token_to_host(host_id)
        controller.delete(f"/agents/{host_id}?purge_token=1", headers=superuser_headers)
        r = controller.post("/agents/enroll", json={
            "token": tok, "host_id": host_id, "hostname": "web1",
            "platform": "linux", "kernel": "6.1", "ip": "10.0.0.11",
        })
        assert r.status_code == 403                   # invalid/expired token
        assert not db.agent_exists(host_id)
