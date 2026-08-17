"""Same-identity fleet migration: re-point selected agents at a new controller.

The Migrate page dispatches, to each selected host, a task that rewrites
SYSIBLE_CONTROLLER in the agent's env file and restarts the agent — the agent
keeps its identity and re-attaches to the new controller (valid when the new box
shares the same database: a restore/failover). These lock in URL validation and
that the correct re-point command is queued.
"""
import backend.db as db


def test_migrate_rejects_bad_input(controller, superuser_headers):
    assert controller.post("/controller/migrate-agents", headers=superuser_headers,
                           json={"new_url": "", "host_ids": ["x"]}).status_code == 400
    assert controller.post("/controller/migrate-agents", headers=superuser_headers,
                           json={"new_url": "10.0.0.5", "host_ids": []}).status_code == 400
    # a host value with shell metacharacters is rejected, not shipped into a command
    r = controller.post("/controller/migrate-agents", headers=superuser_headers,
                        json={"new_url": "10.0.0.5; rm -rf /", "host_ids": ["x"]})
    assert r.status_code == 400


def test_migrate_normalizes_url_and_reports_unknown_host(controller, superuser_headers):
    r = controller.post("/controller/migrate-agents", headers=superuser_headers,
                        json={"new_url": "10.0.0.5", "host_ids": ["nope"]})
    assert r.status_code == 200
    b = r.json()
    assert b["new_url"] == "https://10.0.0.5:9000"        # scheme + default port added
    assert b["dispatched"] == 0 and b["results"][0]["ok"] is False


def test_migrate_queues_repoint_command(controller, superuser_headers, agent):
    hid, _ = agent(host_id="mig-1", hostname="web1")
    r = controller.post("/controller/migrate-agents", headers=superuser_headers,
                        json={"new_url": "ctrl.example:9443", "host_ids": [hid]})
    assert r.status_code == 200 and r.json()["dispatched"] == 1
    joined = " ".join(t.get("command", "") for t in db.fetch_pending_tasks(hid))
    assert "SYSIBLE_CONTROLLER=https://ctrl.example:9443" in joined
    assert "/opt/sysible-agent/sysible_agent.env" in joined
    assert "systemctl restart sysible-agent" in joined
