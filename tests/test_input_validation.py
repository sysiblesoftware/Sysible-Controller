"""Input validation & robustness: malformed JSON, missing/typed fields, the
agent-channel size caps, and Unicode round-tripping. The API must reject bad
shapes with a 4xx (never a 500 / stack trace) and must accept legitimate
Unicode without corruption."""
import json

from conftest import key_headers, UNICODE_PAYLOADS


# ---------------------------------------------------------------------------
# Malformed JSON bodies
# ---------------------------------------------------------------------------
class TestInvalidJson:
    def test_truncated_json(self, controller):
        r = controller.post("/agents/enroll",
                            headers={"Content-Type": "application/json"},
                            content=b'{"token": "x", "host_id":')
        assert r.status_code in (400, 422)

    def test_not_json_at_all(self, controller):
        r = controller.post("/agents/enroll",
                            headers={"Content-Type": "application/json"},
                            content=b"this is not json <<<>>>")
        assert r.status_code in (400, 422)

    def test_json_array_instead_of_object(self, controller):
        r = controller.post("/agents/enroll",
                            headers={"Content-Type": "application/json"},
                            content=b'[1,2,3]')
        assert r.status_code in (400, 422)

    def test_null_body(self, controller):
        r = controller.post("/agents/heartbeat",
                            headers={"Content-Type": "application/json"},
                            content=b'null')
        assert r.status_code in (400, 422)


# ---------------------------------------------------------------------------
# Missing required fields
# ---------------------------------------------------------------------------
class TestMissingFields:
    def test_enroll_missing_token(self, controller):
        r = controller.post("/agents/enroll", json={"host_id": "h1"})
        assert r.status_code == 422

    def test_enroll_missing_host_id(self, controller, enroll_token):
        r = controller.post("/agents/enroll", json={"token": enroll_token()})
        assert r.status_code == 422

    def test_heartbeat_missing_secret(self, controller):
        r = controller.post("/agents/heartbeat", json={"host_id": "h1"})
        assert r.status_code == 422

    def test_task_result_missing_task_id(self, controller, agent):
        host_id, secret = agent()
        r = controller.post(f"/agents/{host_id}/tasks/result",
                            json={"host_id": host_id, "agent_secret": secret, "result": "{}"})
        assert r.status_code == 422

    def test_empty_object_body(self, controller):
        r = controller.post("/agents/enroll", json={})
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Wrong data types
# ---------------------------------------------------------------------------
class TestWrongTypes:
    def test_host_id_as_int(self, controller, enroll_token):
        r = controller.post("/agents/enroll", json={"token": enroll_token(), "host_id": 12345})
        assert r.status_code == 422

    def test_measurements_as_string(self, controller, agent):
        host_id, secret = agent()
        r = controller.post("/agents/heartbeat",
                            json={"host_id": host_id, "agent_secret": secret,
                                  "measurements": "not-a-dict"})
        assert r.status_code == 422

    def test_task_id_non_numeric(self, controller, agent):
        host_id, secret = agent()
        r = controller.post(f"/agents/{host_id}/tasks/result",
                            json={"host_id": host_id, "agent_secret": secret,
                                  "task_id": "abc", "result": "{}"})
        assert r.status_code == 422

    def test_resize_cols_non_numeric(self, controller):
        # Body validation (cols must be int) fires before the session lookup.
        r = controller.post("/remote/terminal/nope/resize",
                            headers=key_headers(), json={"cols": "wide", "rows": 24})
        assert r.status_code == 422

    def test_log_flag_wrong_type(self, controller, ssh_host, superuser_headers):
        name = ssh_host()
        r = controller.post(f"/remote/hosts/{name}/exec", headers=superuser_headers,
                            json={"cmd": "id", "log": "maybe"})
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Very long strings / payload size caps (DoS resistance)
# ---------------------------------------------------------------------------
class TestSizeCaps:
    def test_enroll_token_over_cap_rejected(self, controller):
        r = controller.post("/agents/enroll",
                            json={"token": "x" * 600, "host_id": "h1"})  # cap 512
        assert r.status_code == 422

    def test_oversized_measurements_rejected(self, controller, agent):
        host_id, secret = agent()
        big = {"files": {f"/f{i}": "a" * 100 for i in range(6000)}}  # > 512 KiB
        r = controller.post("/agents/heartbeat",
                            json={"host_id": host_id, "agent_secret": secret, "measurements": big})
        assert r.status_code == 422

    def test_oversized_task_result_rejected(self, controller, agent):
        host_id, secret = agent()
        r = controller.post(f"/agents/{host_id}/tasks/result",
                            json={"host_id": host_id, "agent_secret": secret,
                                  "task_id": 1, "result": "z" * (5 * 1024 * 1024)})  # > 4 MiB
        assert r.status_code == 422

    def test_oversized_command_rejected(self, controller, agent, superuser_headers):
        host_id, _ = agent()
        r = controller.post(f"/agents/{host_id}/tasks", headers=superuser_headers,
                            json={"command": "y" * (2 * 1024 * 1024), "kind": "command"})  # > 1 MiB
        assert r.status_code == 422

    def test_long_but_under_cap_hostname_accepted(self, controller, enroll_token):
        r = controller.post("/agents/enroll",
                            json={"token": enroll_token(), "host_id": "h-long",
                                  "hostname": "n" * 200})  # under 256
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Unicode must round-trip, not corrupt or crash
# ---------------------------------------------------------------------------
class TestUnicode:
    def test_unicode_hostname_round_trips(self, controller, enroll_token):
        name = UNICODE_PAYLOADS[1]  # 服务器-01
        r = controller.post("/agents/enroll",
                            json={"token": enroll_token(), "host_id": "u-host", "hostname": name})
        assert r.status_code == 200
        agents = controller.get("/agents", headers=key_headers()).json()["agents"]
        assert any(a.get("hostname") == name for a in agents)

    def test_unicode_environment_name_round_trips(self, controller, superuser_headers):
        for name in UNICODE_PAYLOADS[:4]:
            r = controller.post("/environments", headers=superuser_headers, json={"name": name})
            assert r.status_code == 200
        envs = controller.get("/environments", headers=key_headers()).json()
        names = json.dumps(envs, ensure_ascii=False)
        for name in UNICODE_PAYLOADS[:4]:
            assert name in names

    def test_emoji_hostname_accepted(self, controller, enroll_token):
        r = controller.post("/agents/enroll",
                            json={"token": enroll_token(), "host_id": "emoji-host",
                                  "hostname": UNICODE_PAYLOADS[4]})
        assert r.status_code == 200
