"""Authentication failures: API key, admin login tokens, and per-host agent
secrets. Every protected surface must reject a missing/invalid/expired
credential and must not leak information about why."""
import time

from conftest import key_headers


# ---------------------------------------------------------------------------
# Controller API key (X-API-Key) — the human-facing surface
# ---------------------------------------------------------------------------
class TestApiKey:
    def test_missing_api_key_rejected(self, controller):
        r = controller.get("/agents")
        assert r.status_code == 401

    def test_wrong_api_key_rejected(self, controller):
        r = controller.get("/agents", headers={"X-API-Key": "not-the-key"})
        assert r.status_code == 401

    def test_empty_api_key_rejected(self, controller):
        r = controller.get("/agents", headers={"X-API-Key": ""})
        assert r.status_code == 401

    def test_valid_api_key_accepted(self, controller):
        r = controller.get("/agents", headers=key_headers())
        assert r.status_code == 200

    def test_key_is_constant_time_compared_length_mismatch(self, controller):
        # A near-miss must be rejected exactly like a total miss (no oracle).
        r = controller.get("/agents", headers={"X-API-Key": "test-api-key-abcde"})  # 1 char short
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Admin login tokens (X-Sysible-Admin-Token) — the RBAC surface
# ---------------------------------------------------------------------------
class TestAdminToken:
    def test_superuser_route_no_token_after_admins_exist(self, controller, make_admin):
        make_admin("someone", "superuser")  # now at least one admin exists
        # No admin token → 401 even with a valid API key.
        r = controller.post("/environments", headers=key_headers(), json={"name": "prod"})
        assert r.status_code == 401

    def test_superuser_route_invalid_token(self, controller, make_admin):
        make_admin("someone", "superuser")
        r = controller.post("/environments",
                            headers=key_headers({"X-Sysible-Admin-Token": "bogus"}),
                            json={"name": "prod"})
        assert r.status_code == 401

    def test_superuser_route_expired_token(self, controller, make_admin):
        # Token that expired an hour ago.
        token = make_admin("someone", "superuser", expiry_offset=-3600)
        r = controller.post("/environments",
                            headers=key_headers({"X-Sysible-Admin-Token": token}),
                            json={"name": "prod"})
        assert r.status_code == 401

    def test_superuser_route_valid_token(self, controller, superuser_headers):
        r = controller.post("/environments", headers=superuser_headers, json={"name": "prod"})
        assert r.status_code == 200

    def test_token_of_deleted_admin_stops_resolving(self, controller, make_admin):
        token = make_admin("gone", "superuser")
        import backend.db as db
        db.remove_administrator("gone")
        r = controller.post("/environments",
                            headers=key_headers({"X-Sysible-Admin-Token": token}),
                            json={"name": "prod"})
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Admin login (/admin/login) — password verification
# ---------------------------------------------------------------------------
class TestAdminLogin:
    def test_login_wrong_password(self, controller, make_admin):
        make_admin("alice", "superuser", password="RightPass1!")
        r = controller.post("/admin/login", headers=key_headers(),
                            json={"username": "alice", "password": "WrongPass"})
        assert r.status_code == 401

    def test_login_unknown_user(self, controller, make_admin):
        make_admin("alice", "superuser")
        r = controller.post("/admin/login", headers=key_headers(),
                            json={"username": "nobody", "password": "whatever"})
        assert r.status_code == 401

    def test_login_success_issues_token(self, controller, make_admin):
        make_admin("alice", "superuser", password="RightPass1!")
        r = controller.post("/admin/login", headers=key_headers(),
                            json={"username": "alice", "password": "RightPass1!"})
        assert r.status_code == 200
        assert r.json().get("token")

    def test_login_requires_api_key(self, controller, make_admin):
        make_admin("alice", "superuser", password="RightPass1!")
        r = controller.post("/admin/login",
                            json={"username": "alice", "password": "RightPass1!"})
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Per-host agent secret — the machine surface (heartbeat / results / tasks poll)
# ---------------------------------------------------------------------------
class TestAgentSecret:
    def test_heartbeat_wrong_secret(self, controller, agent):
        host_id, _secret = agent()
        r = controller.post("/agents/heartbeat",
                            json={"host_id": host_id, "agent_secret": "wrong"})
        assert r.status_code in (401, 403)

    def test_heartbeat_unknown_host(self, controller):
        r = controller.post("/agents/heartbeat",
                            json={"host_id": "ghost", "agent_secret": "whatever"})
        assert r.status_code in (401, 403, 404)

    def test_heartbeat_correct_secret(self, controller, agent):
        host_id, secret = agent()
        r = controller.post("/agents/heartbeat",
                            json={"host_id": host_id, "agent_secret": secret})
        assert r.status_code == 200

    def test_one_host_secret_cannot_act_for_another(self, controller, agent):
        # host A's secret must not authenticate as host B (cross-host spoofing).
        a_id, a_secret = agent("hostA", "secretA", "A", "10.0.0.1")
        b_id, b_secret = agent("hostB", "secretB", "B", "10.0.0.2")
        r = controller.post("/agents/heartbeat",
                            json={"host_id": b_id, "agent_secret": a_secret})
        assert r.status_code in (401, 403)

    def test_task_result_requires_valid_secret(self, controller, agent):
        host_id, _ = agent()
        r = controller.post(f"/agents/{host_id}/tasks/result",
                            json={"host_id": host_id, "agent_secret": "nope",
                                  "task_id": 1, "result": "{}"})
        assert r.status_code in (401, 403)
