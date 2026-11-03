"""Asking a host to capture its whole machine, from the console.

An agent is outbound-only: nothing can reach into a host. So "Capture one
now" is a REQUEST parked on the controller and handed to the agent on the
poll it already makes — the same channel "Back up now" has always used.

Sharing that channel is the whole risk, and what these cover:

  * the two requests are NOT one flag. A config snapshot is a few kilobytes
    out of /etc; a restore point is the machine, read file by file, and an
    operator who asked for one must never be given the other;
  * a parked request is handed over exactly once, so a host does not capture
    its whole filesystem on every poll for ever after one click;
  * the controller builds the request itself and refuses a host it has never
    enrolled, like every other agent route here.
"""
import pytest

from tests.conftest import key_headers


def _poll(controller, host, secret):
    return controller.get(f"/agents/{host}/config-restores",
                          headers={"X-Agent-Secret": secret})


@pytest.fixture(autouse=True)
def _flashback_up(monkeypatch):
    """The poll ANSWERS WITH FLASHBACK'S restores, so without a Flashback it
    503s and never reaches the parked request at all. Stub the one call, so
    these test the parking and nothing else."""
    from backend import flashback
    monkeypatch.setattr(flashback, "pending_restores", lambda _h: [])


@pytest.fixture(autouse=True)
def _clear_parked():
    """The parking lots are module-level sets, deliberately in memory. They
    outlive a test unless they are emptied, and a leaked request makes the
    NEXT test's 'nothing was asked for' assertion pass for the wrong reason."""
    from backend import app as m
    m._CAPTURE_REQUESTS.clear()
    m._POINT_REQUESTS.clear()
    yield
    m._CAPTURE_REQUESTS.clear()
    m._POINT_REQUESTS.clear()


def test_a_parked_request_reaches_the_agent(controller, agent):
    host, secret = agent()
    r = controller.post(f"/agents/{host}/request-restore-point", headers=key_headers())
    assert r.status_code == 200, r.text
    assert r.json()["queued"] is True

    body = _poll(controller, host, secret).json()
    assert body["restore_point_requested"] is True


def test_it_is_handed_over_exactly_once(controller, agent):
    """Otherwise one click captures the whole filesystem on every poll, for
    ever — which on a minute-long poll is a host that never stops reading
    itself."""
    host, secret = agent()
    controller.post(f"/agents/{host}/request-restore-point", headers=key_headers())
    assert _poll(controller, host, secret).json()["restore_point_requested"] is True
    assert _poll(controller, host, secret).json()["restore_point_requested"] is False


def test_a_config_snapshot_and_a_restore_point_are_not_one_flag(controller, agent):
    """The two ride the same poll and mean very different things: a few
    kilobytes of /etc, or every file on the box."""
    host, secret = agent()
    controller.post(f"/agents/{host}/request-capture", headers=key_headers())
    body = _poll(controller, host, secret).json()
    assert body["capture_requested"] is True
    assert body["restore_point_requested"] is False, \
        "asking for a config snapshot captured the whole machine"

    controller.post(f"/agents/{host}/request-restore-point", headers=key_headers())
    body = _poll(controller, host, secret).json()
    assert body["restore_point_requested"] is True
    assert body["capture_requested"] is False, \
        "asking for a restore point also fired a config snapshot"


def test_both_can_be_in_flight_at_once(controller, agent):
    host, secret = agent()
    controller.post(f"/agents/{host}/request-capture", headers=key_headers())
    controller.post(f"/agents/{host}/request-restore-point", headers=key_headers())
    body = _poll(controller, host, secret).json()
    assert body["capture_requested"] is True
    assert body["restore_point_requested"] is True


def test_one_hosts_request_does_not_reach_another(controller, agent):
    a, a_secret = agent("host-a", "secret-a", "web-a")
    b, b_secret = agent("host-b", "secret-b", "web-b")
    controller.post(f"/agents/{a}/request-restore-point", headers=key_headers())
    assert _poll(controller, b, b_secret).json()["restore_point_requested"] is False
    assert _poll(controller, a, a_secret).json()["restore_point_requested"] is True


def test_an_unknown_host_cannot_be_asked(controller):
    r = controller.post("/agents/not-a-host/request-restore-point",
                        headers=key_headers())
    assert r.status_code == 404


def test_the_request_needs_the_api_key(controller, agent):
    host, _ = agent()
    r = controller.post(f"/agents/{host}/request-restore-point")
    assert r.status_code in (401, 403), r.text


def test_the_poll_still_needs_the_hosts_own_secret(controller, agent):
    """The parked request is handed over on an AUTHENTICATED poll. A host that
    could poll as another would learn — and act on — its restore request."""
    agent("host-a", "secret-a", "web-a")
    agent("host-b", "secret-b", "web-b")
    r = controller.get("/agents/host-a/config-restores",
                       headers={"X-Agent-Secret": "secret-b"})
    assert r.status_code in (401, 403), r.text


def test_a_flashback_outage_does_not_eat_the_request(controller, agent, monkeypatch):
    """The poll fetches Flashback's restores BEFORE it consumes the parked
    request, so a 503 leaves the request queued. Losing it the other way is
    how an operator's click silently vanishes — and a whole-machine capture is
    not a click anyone makes twice by accident."""
    from backend import app as m
    from backend import flashback
    host, secret = agent()
    controller.post(f"/agents/{host}/request-restore-point", headers=key_headers())

    def _down(_host_id):
        raise flashback.FlashbackUnavailable("Flashback is unreachable.", 503)
    monkeypatch.setattr(flashback, "pending_restores", _down)
    assert _poll(controller, host, secret).status_code == 503
    with m._CAPTURE_LOCK:
        assert host in m._POINT_REQUESTS, "the 503 ate the restore-point request"

    monkeypatch.setattr(flashback, "pending_restores", lambda _h: [])
    assert _poll(controller, host, secret).json()["restore_point_requested"] is True
