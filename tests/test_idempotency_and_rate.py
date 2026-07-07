"""Duplicate/replayed requests and rate limiting.

Covers which operations are idempotent (heartbeat), which are single-shot and
must reject a replay (enroll-token reuse, duplicate admin/environment), which
are intentionally NOT deduped (task dispatch), and the login brute-force
throttle on the web console.
"""
import time

import pytest

from conftest import key_headers


# ---------------------------------------------------------------------------
# Duplicate / replayed requests
# ---------------------------------------------------------------------------
class TestDuplicates:
    def test_enroll_token_replay_cannot_take_over_live_host(self, controller, enroll_token):
        tok = enroll_token()
        r1 = controller.post("/agents/enroll",
                             json={"token": tok, "host_id": "hostA", "hostname": "A", "ip": "10.0.0.1"})
        assert r1.status_code == 200
        # Replaying the (now used) token against the live host is refused: the
        # reuse window resolves back to the bound host, which is still online.
        r2 = controller.post("/agents/enroll",
                             json={"token": tok, "host_id": "attacker", "hostname": "evil", "ip": "10.0.0.9"})
        assert r2.status_code == 409

    def test_enroll_token_replay_on_revoked_host_refused(self, controller, enroll_token):
        import backend.db as db
        tok = enroll_token()
        r1 = controller.post("/agents/enroll",
                             json={"token": tok, "host_id": "hostR", "hostname": "R", "ip": "10.0.0.2"})
        assert r1.status_code == 200
        db.revoke_agent("hostR")
        r2 = controller.post("/agents/enroll",
                             json={"token": tok, "host_id": "x", "hostname": "R", "ip": "10.0.0.2"})
        assert r2.status_code == 403  # a revoked host is not resurrected by a token replay

    def test_heartbeat_is_idempotent(self, controller, agent):
        host_id, secret = agent()
        body = {"host_id": host_id, "agent_secret": secret}
        assert controller.post("/agents/heartbeat", json=body).status_code == 200
        assert controller.post("/agents/heartbeat", json=body).status_code == 200

    def test_duplicate_task_dispatch_creates_distinct_tasks(self, controller, superuser_headers, agent):
        # Dispatch is intentionally NOT idempotent — two identical requests are
        # two real commands and get distinct task ids.
        host_id, _ = agent()
        body = {"command": "id", "kind": "command"}
        r1 = controller.post(f"/agents/{host_id}/tasks", headers=superuser_headers, json=body)
        r2 = controller.post(f"/agents/{host_id}/tasks", headers=superuser_headers, json=body)
        assert r1.status_code == r2.status_code == 200
        assert r1.json()["task_id"] != r2.json()["task_id"]

    def test_duplicate_admin_username_rejected(self, controller, superuser_headers):
        body = {"username": "dupe", "password": "Password123!", "role": "sysadmin"}
        r1 = controller.post("/admin/administrators", headers=superuser_headers, json=body)
        assert r1.status_code == 200
        r2 = controller.post("/admin/administrators", headers=superuser_headers, json=body)
        assert r2.status_code in (400, 409)

    def test_duplicate_environment_rejected(self, controller, superuser_headers):
        r1 = controller.post("/environments", headers=superuser_headers, json={"name": "staging"})
        assert r1.status_code == 200
        r2 = controller.post("/environments", headers=superuser_headers, json={"name": "staging"})
        assert r2.status_code == 409


# ---------------------------------------------------------------------------
# Rate limiting — the web console login throttle (brute-force resistance)
# ---------------------------------------------------------------------------
class TestRateLimit:
    def test_login_throttle_returns_429_after_max_attempts(self, bff, monkeypatch):
        import webgui.server as srv
        import requests

        srv._login_attempts.clear()  # isolate this test's IP bucket

        def _fail(username, password):
            resp = requests.models.Response()
            resp.status_code = 401
            raise requests.exceptions.HTTPError(response=resp)

        monkeypatch.setattr(srv.api, "admin_login", _fail)

        max_attempts = srv._LOGIN_MAX_ATTEMPTS
        codes = []
        for _ in range(max_attempts + 2):
            r = bff.post("/api/login", json={"username": "admin", "password": "wrong"})
            codes.append(r.status_code)

        # The first `max_attempts` are rejected as bad creds (401); once the
        # window fills, further attempts are throttled with 429.
        assert codes[:max_attempts] == [401] * max_attempts
        assert 429 in codes[max_attempts:]

    def test_successful_login_clears_throttle_counter(self, bff, monkeypatch):
        import webgui.server as srv
        import requests

        srv._login_attempts.clear()

        def _fail(username, password):
            resp = requests.models.Response()
            resp.status_code = 401
            raise requests.exceptions.HTTPError(response=resp)

        monkeypatch.setattr(srv.api, "admin_login", _fail)
        for _ in range(3):
            bff.post("/api/login", json={"username": "admin", "password": "wrong"})

        # A success resets the per-IP counter.
        def _ok(username, password):
            return {"role": "superuser", "sudo_connect": False,
                    "must_change_password": False, "token": "t"}

        monkeypatch.setattr(srv.api, "admin_login", _ok)
        r = bff.post("/api/login", json={"username": "admin", "password": "right"})
        assert r.status_code == 200
        assert srv._login_attempts.get("testclient") in (None, [])
