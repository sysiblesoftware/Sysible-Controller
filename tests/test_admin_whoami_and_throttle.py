"""
Security-audit regressions (controller side):
  1. GET /admin/whoami resolves a live token to {username, role} and 401s a
     revoked one. The web console calls it to drop a demoted/removed admin's
     session promptly instead of trusting the signed cookie until token expiry.
  2. The admin-login failure throttle is keyed PER USERNAME, not per client IP —
     one account's failures must not lock out another (all console logins share
     the BFF's IP, so an IP key would lump every admin into one bucket).
"""
import backend.db as db
from conftest import key_headers


class TestWhoami:
    def test_valid_token_returns_identity(self, controller, make_admin):
        tok = make_admin("who-1", "sysadmin")
        r = controller.get("/admin/whoami",
                           headers=key_headers({"X-Sysible-Admin-Token": tok}))
        assert r.status_code == 200
        body = r.json()
        assert body["username"] == "who-1"
        assert body["role"] == "sysadmin"

    def test_missing_token_401(self, controller):
        r = controller.get("/admin/whoami", headers=key_headers())
        assert r.status_code == 401

    def test_revoked_after_role_change_401(self, controller, make_admin):
        # resolve_admin_token rejects a token whose account role changed — the
        # basis for the console's session revalidation dropping a demoted admin.
        tok = make_admin("who-2", "superuser")
        assert controller.get("/admin/whoami",
                              headers=key_headers({"X-Sysible-Admin-Token": tok})).status_code == 200
        db.set_administrator_role("who-2", "sysadmin")
        r = controller.get("/admin/whoami",
                           headers=key_headers({"X-Sysible-Admin-Token": tok}))
        assert r.status_code == 401

    def test_removed_admin_token_401(self, controller, make_admin):
        tok = make_admin("who-3", "sysadmin")
        db.remove_administrator("who-3")
        r = controller.get("/admin/whoami",
                           headers=key_headers({"X-Sysible-Admin-Token": tok}))
        assert r.status_code == 401


class TestLoginThrottlePerUsername:
    def _fail_login(self, controller, username):
        return controller.post("/admin/login", headers=key_headers(),
                               json={"username": username, "password": "wrong-pw"})

    def test_lockout_is_per_username_not_shared(self, controller, make_admin):
        make_admin("victim", "sysadmin", password="Correct-Horse-1!")
        make_admin("bystander", "sysadmin", password="Correct-Horse-2!")
        # Hammer 'victim' past the failure threshold (10).
        codes = [self._fail_login(controller, "victim").status_code for _ in range(12)]
        assert 429 in codes, "victim should eventually be throttled"
        # 'bystander' must still be able to attempt (not collateral-locked).
        r = controller.post("/admin/login", headers=key_headers(),
                            json={"username": "bystander", "password": "Correct-Horse-2!"})
        assert r.status_code == 200, r.text

    def test_nonexistent_user_still_401(self, controller):
        # The decoy-verify path (closing the timing oracle) must not change the
        # externally-visible outcome: an unknown user is a normal 401.
        r = self._fail_login(controller, "no-such-admin-xyz")
        assert r.status_code == 401
