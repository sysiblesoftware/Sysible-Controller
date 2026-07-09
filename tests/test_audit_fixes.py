"""
Regression tests for the day-1 functionality audit fixes:
- POST /agents/{id}/restore un-revokes a host in place (the Restore button had no route).
- Self-service credential change allows username-only OR password-only (the
  "(optional)" fields used to reject an empty password).
- /api/me fails CLOSED to 'auditor' on a missing role (was fail-open to superuser).
"""
import secrets
import time

import backend.db as db
from conftest import key_headers


def _enroll_agent(host_id="h-restore", secret="sek", hostname="host-x", ip="10.0.0.5"):
    db.create_or_update_agent(host_id, hostname, "linux", "6.1", "online",
                              time.time(), secret, ip)
    return host_id


class TestRestoreRoute:
    def test_revoke_then_restore_unrevokes(self, controller, superuser_headers):
        hid = _enroll_agent()
        assert controller.post(f"/agents/{hid}/revoke", headers=superuser_headers).status_code == 200
        assert any(a["host_id"] == hid and a.get("revoked") for a in db.list_agents())
        r = controller.post(f"/agents/{hid}/restore", headers=superuser_headers)
        assert r.status_code == 200
        assert not any(a["host_id"] == hid and a.get("revoked") for a in db.list_agents())

    def test_restore_unknown_host_404(self, controller, superuser_headers):
        r = controller.post("/agents/nope/restore", headers=superuser_headers)
        assert r.status_code == 404

    def test_restore_requires_superuser(self, controller, sysadmin_headers):
        hid = _enroll_agent(host_id="h-r2")
        r = controller.post(f"/agents/{hid}/restore", headers=sysadmin_headers)
        assert r.status_code == 403


class TestCredentialChange:
    def _admin(self, name="cred-user"):
        from backend import portal_auth
        salt, pw = portal_auth.hash_password("Password123!")
        db.add_administrator(name, pw, salt, must_change_password=0, role="superuser")
        return name

    def test_username_only_change(self, controller):
        self._admin("rename-me")
        r = controller.post("/admin/credentials", headers=key_headers(), json={
            "username": "rename-me", "current_password": "Password123!",
            "new_username": "renamed", "new_password": ""})
        assert r.status_code == 200, r.text
        assert db.get_administrator("renamed") is not None
        assert db.get_administrator("rename-me") is None
        # Old password still works (it wasn't changed).
        from backend import portal_auth
        a = db.get_administrator("renamed")
        assert portal_auth.verify_password("Password123!", a["password_salt"], a["password_hash"])

    def test_password_only_change(self, controller):
        self._admin("pw-only")
        r = controller.post("/admin/credentials", headers=key_headers(), json={
            "username": "pw-only", "current_password": "Password123!",
            "new_username": "", "new_password": "NewPassw0rd!"})
        assert r.status_code == 200, r.text
        a = db.get_administrator("pw-only")
        from backend import portal_auth
        assert portal_auth.verify_password("NewPassw0rd!", a["password_salt"], a["password_hash"])

    def test_no_change_rejected(self, controller):
        self._admin("no-change")
        r = controller.post("/admin/credentials", headers=key_headers(), json={
            "username": "no-change", "current_password": "Password123!",
            "new_username": "", "new_password": ""})
        assert r.status_code == 400

    def test_wrong_current_password_rejected(self, controller):
        self._admin("wrong-pw")
        r = controller.post("/admin/credentials", headers=key_headers(), json={
            "username": "wrong-pw", "current_password": "nope",
            "new_username": "x", "new_password": ""})
        assert r.status_code == 401


class TestMeFailsClosed:
    def test_me_defaults_to_auditor(self, bff):
        # A session with a user but no role must resolve to the least-privileged
        # role, not superuser.
        with bff as client:
            # Seed a session cookie by logging in is heavy; instead assert the code
            # path: /api/me with an authenticated session missing 'role'. We use the
            # test client's session by monkeypatching via a direct call.
            import webgui.server as srv
            from starlette.requests import Request

            class _Req:
                session = {"user": "ghost"}  # no role
            out = srv.me(_Req())
            assert out["role"] == "auditor"
