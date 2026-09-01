"""A bundle can be minted FOR a Controller environment (SLEP building VMs "into" an
environment): the environment is stamped on the enroll token and applied to the host
the moment it self-enrolls, so agent-enrolled VMs arrive already grouped instead of
landing in "Unassigned". These lock in that behavior end-to-end on the enroll path."""
import secrets

import backend.db as db


def _enroll(controller, token, host_id, hostname, ip):
    return controller.post("/agents/enroll", json={
        "token": token, "host_id": host_id, "hostname": hostname,
        "platform": "linux", "kernel": "6.1", "ip": ip,
    })


def _mk_token(environment=""):
    tok = "enroll-" + secrets.token_hex(12)
    db.create_enroll_token(tok, environment=environment)
    return tok


def _env_of(host_id):
    a = next((x for x in db.list_agents() if x.get("host_id") == host_id), None)
    return (a or {}).get("environment") or ""


def test_token_environment_round_trips_and_survives_consume():
    tok = _mk_token("Web-Test")
    assert db.enroll_token_environment(tok) == "Web-Test"
    # The row persists after the single-use claim, so the enroll handler can still
    # read the environment once the token has been consumed.
    assert db.consume_enroll_token(tok, "host-x") is True
    assert db.enroll_token_environment(tok) == "Web-Test"
    assert db.enroll_token_environment("does-not-exist") == ""


def test_plain_token_carries_no_environment():
    assert db.enroll_token_environment(_mk_token()) == ""


def test_fresh_enroll_lands_in_the_bundle_environment(controller):
    db.create_environment("Web-Test")
    r = _enroll(controller, _mk_token("Web-Test"), "web-1-id", "web-1", "192.168.100.51")
    assert r.status_code == 200
    assert _env_of(r.json()["host_id"]) == "Web-Test"


def test_plain_bundle_enrolls_unassigned(controller):
    r = _enroll(controller, _mk_token(), "plain-1-id", "plain-1", "192.168.100.52")
    assert r.status_code == 200
    assert _env_of(r.json()["host_id"]) == ""
