"""Decommission (CE) — neutralize a controller and return its teardown command,
plus the BFF client's TLS-context refresh after a controller cert reissue."""
import backend.app as app_module
import backend.db as db


def test_decommission_wipes_fleet_and_returns_teardown(controller, superuser_headers, agent, monkeypatch):
    hid, _ = agent(host_id="dc-1", hostname="web1")
    agent(host_id="dc-2", hostname="web2")
    monkeypatch.setattr(app_module, "_is_container", lambda: True)
    r = controller.post("/controller/decommission", headers=superuser_headers,
                        json={"confirm": "DECOMMISSION"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "decommissioned"
    assert body["hosts_removed"] == 2
    assert "docker compose" in body["teardown"] and "down -v" in body["teardown"]
    assert not db.agent_exists(hid)
    assert db.list_agents() == []


def test_decommission_requires_confirmation_phrase(controller, superuser_headers, agent):
    hid, _ = agent(host_id="dc-3")
    assert controller.post("/controller/decommission", headers=superuser_headers,
                           json={"confirm": "yes"}).status_code == 400
    assert db.agent_exists(hid)


def test_decommission_requires_superuser(controller, sysadmin_headers):
    assert controller.post("/controller/decommission", headers=sysadmin_headers,
                           json={"confirm": "DECOMMISSION"}).status_code in (401, 403)


def test_request_rebuilds_session_on_ssl_error(monkeypatch):
    import client.api as capi
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            import requests
            raise requests.exceptions.SSLError("self-signed certificate")
        return "ok"

    rebuilt = {"n": 0}
    monkeypatch.setattr(capi, "_rebuild_session", lambda: rebuilt.__setitem__("n", rebuilt["n"] + 1))
    assert capi._request_with_tls_refresh(flaky) == "ok"
    assert calls["n"] == 2 and rebuilt["n"] == 1
