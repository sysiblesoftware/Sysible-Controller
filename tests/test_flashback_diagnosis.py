"""Config backup has to name the RIGHT fault.

Three bugs, one symptom. An operator with a perfectly current fleet was told, on
every row, "agent doesn't do config backup — update this host's agent" — and
"Back up now" did nothing no matter how many times they pressed it.

  1. The BFF read the controller's {"hosts": {...}} reply as if it WERE the host
     map, so every last_config_poll lookup missed and every host looked like a
     build from before config backup existed.
  2. Nothing reported whether the CONTROLLER could relay a snapshot at all. On a
     controller with no Flashback wiring no agent ever polls, so the per-agent
     signal says "stale agent" about a fleet whose agents are fine.
  3. The agent poll consumed the queued capture request BEFORE fetching the
     restores that might 503 — so a Flashback that was unconfigured or briefly
     unreachable ate the operator's click silently.
"""
import time

import pytest

import backend.app as A
import backend.db as db


# --- 1. the envelope ---------------------------------------------------------
def test_the_bff_reads_the_poll_times_out_of_their_envelope(controller, agent, monkeypatch):
    import webgui.server as srv
    hid, sec = agent()
    controller.get(f"/agents/{hid}/config-restores", headers={"X-Agent-Secret": sec})
    payload = A.agent_config_poll_times()
    assert payload["hosts"][hid], "the agent's poll was not recorded at all"

    monkeypatch.setattr(srv.api, "get_config_poll_times", lambda: payload)
    monkeypatch.setattr(srv.api, "get_agents", lambda: [])
    monkeypatch.setattr(srv.dispatch, "list_merged_hosts",
                        lambda **kw: [{"id": hid, "label": "web1", "kind": "agent"}])
    rows = {h["id"]: h for h in srv.hosts(user="t")["hosts"]}
    assert rows[hid]["last_config_poll"], \
        "last_config_poll came back None for a host that HAS polled"


def test_an_older_controller_without_the_envelope_still_works(monkeypatch):
    """Belt and braces: a bare mapping is accepted too, so this cannot break
    against a controller that has not been updated yet."""
    import webgui.server as srv
    monkeypatch.setattr(srv.api, "get_config_poll_times", lambda: {"h9": 123.0})
    monkeypatch.setattr(srv.api, "get_agents", lambda: [])
    monkeypatch.setattr(srv.dispatch, "list_merged_hosts",
                        lambda **kw: [{"id": "h9", "label": "h9", "kind": "agent"}])
    rows = {h["id"]: h for h in srv.hosts(user="t")["hosts"]}
    assert rows["h9"]["last_config_poll"] == 123.0


# --- 2. the controller's own wiring -----------------------------------------
def test_the_controller_says_whether_it_can_relay_at_all(controller, superuser_headers,
                                                         monkeypatch):
    from backend import flashback
    monkeypatch.setattr(flashback, "configured", lambda: False)
    d = controller.get("/agents/config-poll-times", headers=superuser_headers).json()
    assert d["config_backup_configured"] is False
    assert "SYSIBLE_FLASHBACK_URL" in (d["config_backup_reason"] or "")

    monkeypatch.setattr(flashback, "configured", lambda: True)
    d = controller.get("/agents/config-poll-times", headers=superuser_headers).json()
    assert d["config_backup_configured"] is True and d["config_backup_reason"] is None


def test_the_reason_never_carries_the_token(controller, superuser_headers, monkeypatch):
    from backend import flashback
    monkeypatch.setattr(flashback, "AGENT_TOKEN", "s3cr3t-agent-token")
    monkeypatch.setattr(flashback, "configured", lambda: False)
    body = controller.get("/agents/config-poll-times", headers=superuser_headers).text
    assert "s3cr3t-agent-token" not in body


# --- 3. the click that vanished ---------------------------------------------
def test_a_queued_capture_survives_an_unreachable_flashback(controller, agent, monkeypatch):
    """The request must still be there when the poll can finally answer. Consuming
    it before the 503 is what made "Back up now" a no-op."""
    from backend import flashback
    hid, sec = agent()
    with A._CAPTURE_LOCK:
        A._CAPTURE_REQUESTS.add(hid)

    def _down(_host_id):
        raise flashback.FlashbackUnavailable("Flashback is unreachable.", 503)
    monkeypatch.setattr(flashback, "pending_restores", _down)

    r = controller.get(f"/agents/{hid}/config-restores", headers={"X-Agent-Secret": sec})
    assert r.status_code == 503
    with A._CAPTURE_LOCK:
        assert hid in A._CAPTURE_REQUESTS, "the capture request was eaten by the 503"

    # ...and it is handed over as soon as the poll can be answered.
    monkeypatch.setattr(flashback, "pending_restores", lambda _h: [])
    r = controller.get(f"/agents/{hid}/config-restores", headers={"X-Agent-Secret": sec})
    assert r.status_code == 200 and r.json()["capture_requested"] is True


def test_a_capture_is_still_handed_over_exactly_once(controller, agent, monkeypatch):
    from backend import flashback
    hid, sec = agent()
    monkeypatch.setattr(flashback, "pending_restores", lambda _h: [])
    with A._CAPTURE_LOCK:
        A._CAPTURE_REQUESTS.add(hid)
    h = {"X-Agent-Secret": sec}
    assert controller.get(f"/agents/{hid}/config-restores", headers=h).json()["capture_requested"]
    assert not controller.get(f"/agents/{hid}/config-restores", headers=h).json()["capture_requested"]


def test_polling_is_recorded_even_when_flashback_is_down(controller, agent, monkeypatch):
    """The poll itself proves the agent's build supports config backup. Losing
    that to an unrelated Flashback outage would put the wrong diagnosis back."""
    from backend import flashback
    hid, sec = agent()

    def _down(_host_id):
        raise flashback.FlashbackUnavailable("nope", 503)
    monkeypatch.setattr(flashback, "pending_restores", _down)
    controller.get(f"/agents/{hid}/config-restores", headers={"X-Agent-Secret": sec})
    assert db.get_config_poll_times().get(hid)
