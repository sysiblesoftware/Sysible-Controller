"""Injection resistance.

SQL: every query is parameterized, so a payload must be stored/compared as an
inert *string* — never executed. We prove the target table survives and the
payload round-trips verbatim.

XSS: the API is JSON. A payload must be stored and returned as data (the
response is application/json, so a browser won't execute it) — not reflected
into an HTML response and not silently stripped. Output-encoding at render time
is the frontend's job; the API's job is to store/return it faithfully and never
let it change control flow.
"""
from conftest import key_headers, SQLI_PAYLOADS, XSS_PAYLOADS

import backend.db as db


# ---------------------------------------------------------------------------
# SQL injection
# ---------------------------------------------------------------------------
class TestSqlInjection:
    def test_environment_name_injection_is_inert(self, controller, superuser_headers):
        for payload in SQLI_PAYLOADS:
            r = controller.post("/environments", headers=superuser_headers, json={"name": payload})
            assert r.status_code in (200, 409)  # accepted-as-data or duplicate — never a 500
        # The table still exists and a normal insert still works afterwards.
        r = controller.post("/environments", headers=superuser_headers, json={"name": "prod-normal"})
        assert r.status_code == 200
        names = controller.get("/environments", headers=key_headers()).json()["environments"]
        assert "prod-normal" in names
        assert SQLI_PAYLOADS[0] in names  # the DROP TABLE payload stored as a literal name

    def test_admin_username_injection_is_inert(self, controller, superuser_headers):
        payload = "robert'); DROP TABLE administrators; --"
        r = controller.post("/admin/administrators", headers=superuser_headers,
                            json={"username": payload, "password": "Password123!", "role": "sysadmin"})
        assert r.status_code in (200, 400, 409)
        # administrators table intact: the acting superuser is still listed.
        admins = controller.get("/admin/administrators", headers=superuser_headers).json()
        blob = str(admins)
        assert "super" in blob  # the superuser fixture account survived

    def test_login_username_injection_no_auth_bypass(self, controller, make_admin):
        make_admin("alice", "superuser", password="Secret123!")
        for payload in ["alice'--", "' OR '1'='1", "alice' OR '1'='1'--"]:
            r = controller.post("/admin/login", headers=key_headers(),
                                json={"username": payload, "password": "anything"})
            assert r.status_code == 401  # never authenticates

    def test_enroll_host_id_injection_is_inert(self, controller, enroll_token):
        payload = "h1'; DELETE FROM agents; --"
        r = controller.post("/agents/enroll",
                            json={"token": enroll_token(), "host_id": payload, "hostname": "x"})
        assert r.status_code == 200
        # agents table intact and the row is keyed by the literal string.
        assert db.agent_exists(payload)

    def test_activity_description_injection_is_inert(self, controller, superuser_headers):
        payload = "ran'; DROP TABLE activity_log; --"
        r = controller.post("/activity", headers=superuser_headers,
                            json={"host": "h", "description": payload})
        assert r.status_code in (200, 201)
        # activity_log table intact and readable.
        r = controller.get("/activity-log", headers=superuser_headers)
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# XSS payloads: stored as inert data, returned as JSON, never reflected as HTML
# ---------------------------------------------------------------------------
class TestXss:
    def test_environment_xss_stored_as_data_and_returned_as_json(self, controller, superuser_headers):
        for payload in XSS_PAYLOADS:
            r = controller.post("/environments", headers=superuser_headers, json={"name": payload})
            assert r.status_code in (200, 409)
        r = controller.get("/environments", headers=key_headers())
        # Response is JSON (a browser won't execute a <script> in an application/json body).
        assert r.headers["content-type"].startswith("application/json")
        names = r.json()["environments"]
        # Stored verbatim, not stripped/mangled.
        for payload in XSS_PAYLOADS:
            assert payload in names

    def test_xss_not_reflected_into_html(self, controller, superuser_headers):
        payload = "<script>alert('pwn')</script>"
        controller.post("/environments", headers=superuser_headers, json={"name": payload})
        r = controller.get("/environments", headers=key_headers())
        # The raw <script> appears only inside a JSON string, and the content-type
        # is application/json — so it is data, not executable markup.
        assert "text/html" not in r.headers.get("content-type", "")

    def test_activity_xss_round_trips_as_data(self, controller, superuser_headers):
        payload = "<img src=x onerror=alert(1)>"
        r = controller.post("/activity", headers=superuser_headers,
                            json={"host": "h", "description": payload})
        assert r.status_code in (200, 201)
        r = controller.get("/activity-log", headers=superuser_headers)
        assert r.headers["content-type"].startswith("application/json")
        assert payload in str(r.json())
