"""Authorization / RBAC failures. Roles: superuser (full), sysadmin (operate,
no admin management), auditor (read-only oversight — may never act). Each gate
is enforced server-side so it holds for a hand-crafted API call, not just the UI."""
from conftest import key_headers


# ---------------------------------------------------------------------------
# superuser-only actions: sysadmin & auditor must be refused
# ---------------------------------------------------------------------------
class TestSuperuserOnly:
    def test_sysadmin_cannot_create_environment(self, controller, sysadmin_headers):
        r = controller.post("/environments", headers=sysadmin_headers, json={"name": "prod"})
        assert r.status_code == 403

    def test_auditor_cannot_create_environment(self, controller, auditor_headers):
        r = controller.post("/environments", headers=auditor_headers, json={"name": "prod"})
        assert r.status_code == 403

    def test_superuser_can_create_environment(self, controller, superuser_headers):
        r = controller.post("/environments", headers=superuser_headers, json={"name": "prod"})
        assert r.status_code == 200

    def test_sysadmin_cannot_add_administrator(self, controller, sysadmin_headers):
        r = controller.post("/admin/administrators", headers=sysadmin_headers,
                            json={"username": "x", "password": "Password123!", "role": "sysadmin"})
        assert r.status_code == 403

    def test_sysadmin_cannot_delete_administrator(self, controller, sysadmin_headers, make_admin):
        make_admin("victim", "sysadmin")
        r = controller.delete("/admin/administrators/victim", headers=sysadmin_headers)
        assert r.status_code == 403

    def test_sysadmin_cannot_read_admin_audit_log(self, controller, sysadmin_headers):
        r = controller.get("/admin/audit-log", headers=sysadmin_headers)
        assert r.status_code == 403

    def test_sysadmin_cannot_change_controller_config(self, controller, sysadmin_headers):
        r = controller.post("/controller-config", headers=sysadmin_headers,
                            json={"hostname": "c", "ip": "1.2.3.4", "address_mode": "hostname"})
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Auditor is read-only: may VIEW oversight data but never ACT
# ---------------------------------------------------------------------------
class TestAuditorReadOnly:
    def test_auditor_can_read_activity_log(self, controller, auditor_headers):
        r = controller.get("/activity-log", headers=auditor_headers)
        assert r.status_code == 200

    def test_sysadmin_cannot_read_activity_log(self, controller, sysadmin_headers):
        # activity feed is superuser or auditor only — a plain operator is refused.
        r = controller.get("/activity-log", headers=sysadmin_headers)
        assert r.status_code == 403

    def test_auditor_cannot_dispatch_task(self, controller, auditor_headers, agent):
        host_id, _ = agent()
        r = controller.post(f"/agents/{host_id}/tasks", headers=auditor_headers,
                            json={"command": "id", "kind": "command"})
        assert r.status_code == 403

    def test_auditor_cannot_exec_on_ssh_host(self, controller, auditor_headers, ssh_host):
        name = ssh_host()
        # log flag must NOT be able to bypass the auditor block (regression guard).
        r = controller.post(f"/remote/hosts/{name}/exec", headers=auditor_headers,
                            json={"cmd": "id", "log": False})
        assert r.status_code == 403

    def test_auditor_cannot_revoke_agent(self, controller, auditor_headers, agent):
        host_id, _ = agent()
        r = controller.post(f"/agents/{host_id}/revoke", headers=auditor_headers)
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Positive controls: an operator/superuser CAN do the operator actions
# ---------------------------------------------------------------------------
class TestOperatorAllowed:
    def test_sysadmin_can_dispatch_task(self, controller, sysadmin_headers, agent):
        host_id, _ = agent()
        r = controller.post(f"/agents/{host_id}/tasks", headers=sysadmin_headers,
                            json={"command": "id", "kind": "command"})
        assert r.status_code == 200
        assert r.json().get("task_id") is not None

    def test_last_superuser_cannot_be_deleted(self, controller, superuser_headers, make_admin):
        # Extra non-superusers exist, but the last superuser is still protected.
        make_admin("ops2", "sysadmin")
        make_admin("audit2", "auditor")
        r = controller.delete("/admin/administrators/super", headers=superuser_headers)
        assert r.status_code == 400
