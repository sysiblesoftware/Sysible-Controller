"""Relaying a machine image, and the boundaries that hold while it moves.

Hosts never talk to Flashback. The agent posts to this controller over the
channel it already has — its own agent_secret, TLS-pinned, outbound-only — and
the controller, which has just authenticated it, stamps the host_id and
relays. Flashback's token lives in exactly one place and a host cannot name
another host.

That rule earns its keep twice over here, because these routes move whole
filesystems rather than one config file:

  * A host cannot capture, read or be handed ANOTHER machine's image. The
    host_id is taken from the URL that authenticated, never from the body.

  * A manifest is a list of every path and permission on a machine, and it is
    readable before any restore job exists. Reading another host's is refused.

  * Blob content is SHARED across the whole fleet by design — that is what
    makes capturing whole systems affordable. So an agent that could ask for
    any hash at any time could read any file any host has ever captured.
    Fetching is allowed only while that host has a restore in flight.

This drives the real relay against a real Flashback, the way the existing
relay tests do: a stub would prove the routes exist and nothing about whether
the boundary holds.
"""
import base64
import hashlib
import sys

import pytest

# `controller` and `agent` are conftest fixtures and arrive by themselves;
# only the Flashback-specific ones come from the relay test's module.
from tests.test_flashback_relay import flashback, hdr  # noqa: F401


def _ent(path, body=None, kind="f", mode=0o644, target=""):
    e = {"path": path, "kind": kind, "mode": mode, "uid": 0, "gid": 0}
    if kind == "f":
        e["sha256"] = hashlib.sha256(body).hexdigest()
        e["size"] = len(body)
    if kind == "l":
        e["target"] = target
    return e


def _capture(controller, host, secret, label="a point", bodies=None):
    bodies = bodies or [b"one\n", b"two\n"]
    pid = controller.post(f"/agents/{host}/restore-point/open",
                          json={"label": label, "roots": "/etc"},
                          headers=hdr(secret)).json()["point_id"]
    ents = [_ent("/etc", kind="d", mode=0o755)] + [
        _ent(f"/etc/f{i}", b) for i, b in enumerate(bodies)]
    missing = controller.post(f"/agents/{host}/restore-point/manifest",
                              json={"point_id": pid, "entries": ents},
                              headers=hdr(secret)).json()["missing"]
    by = {e["sha256"]: b for e, b in zip(ents[1:], bodies)}
    if missing:
        controller.post(f"/agents/{host}/restore-point/blobs", json={
            "point_id": pid,
            "blobs": [{"sha256": s, "content_b64": base64.b64encode(by[s]).decode()}
                      for s in missing]}, headers=hdr(secret))
    r = controller.post(f"/agents/{host}/restore-point/seal",
                        json={"point_id": pid}, headers=hdr(secret))
    assert r.status_code == 200, r.text
    return pid


# ---------------------------------------------------------------------------
# it works at all
# ---------------------------------------------------------------------------
def test_an_agent_can_capture_a_restore_point(controller, agent, flashback):
    host, secret = agent()
    pid = _capture(controller, host, secret)
    pts = flashback.list_restore_points(host, sealed_only=True)
    assert [p["id"] for p in pts] == [pid]
    assert pts[0]["files"] == 3


def test_a_second_capture_uploads_only_what_changed(controller, agent, flashback):
    """The economy of the whole feature, through the real relay."""
    host, secret = agent()
    _capture(controller, host, secret, bodies=[b"one\n", b"two\n"])
    pid = controller.post(f"/agents/{host}/restore-point/open",
                          json={"label": "again"}, headers=hdr(secret)).json()["point_id"]
    ents = [_ent("/etc", kind="d")] + [_ent("/etc/f0", b"one\n"),
                                       _ent("/etc/f1", b"CHANGED\n")]
    missing = controller.post(f"/agents/{host}/restore-point/manifest",
                              json={"point_id": pid, "entries": ents},
                              headers=hdr(secret)).json()["missing"]
    assert missing == [hashlib.sha256(b"CHANGED\n").hexdigest()], missing


# ---------------------------------------------------------------------------
# ...and one host cannot reach another
# ---------------------------------------------------------------------------
def test_the_host_id_comes_from_the_url_not_the_body(controller, agent, flashback):
    """An agent naming a host would be an agent capturing, or becoming, any
    machine in the fleet."""
    a_host, a_secret = agent("host-a", "secret-a", "web-a")
    agent("host-b", "secret-b", "web-b")
    pid = controller.post(f"/agents/{a_host}/restore-point/open",
                          json={"label": "mine", "host_id": "host-b"},
                          headers=hdr(a_secret)).json()["point_id"]
    assert flashback.restore_point(pid)["host_id"] == "host-a", \
        "the agent chose which host the point belongs to"


def test_another_hosts_manifest_cannot_be_read(controller, agent, flashback):
    """A manifest is every path and permission on a machine."""
    _a, a_secret = agent("host-a", "secret-a", "web-a")
    b_host, b_secret = agent("host-b", "secret-b", "web-b")
    pid = _capture(controller, b_host, b_secret)
    r = controller.get("/agents/host-a/restore-point/manifest",
                       params={"point_id": pid}, headers=hdr(a_secret))
    assert r.status_code == 403, r.text


def test_blobs_cannot_be_fetched_without_a_restore_in_flight(controller, agent,
                                                             flashback):
    """Content is shared across the fleet by design. An agent free to ask for
    any hash could read any file any host has ever captured."""
    host, secret = agent()
    _capture(controller, host, secret, bodies=[b"a secret config\n"])
    sha = hashlib.sha256(b"a secret config\n").hexdigest()
    r = controller.get(f"/agents/{host}/restore-point/blob",
                       params={"sha256": sha}, headers=hdr(secret))
    assert r.status_code == 403, r.text


def test_blobs_can_be_fetched_once_a_restore_is_queued(controller, agent, flashback):
    host, secret = agent()
    body = b"a secret config\n"
    pid = _capture(controller, host, secret, bodies=[body])
    flashback.queue_point_restore(host, pid, "alice")
    sha = hashlib.sha256(body).hexdigest()
    r = controller.get(f"/agents/{host}/restore-point/blob",
                       params={"sha256": sha}, headers=hdr(secret))
    assert r.status_code == 200, r.text
    assert r.content == body


def test_the_manifest_in_flight_is_readable_by_its_own_host(controller, agent,
                                                            flashback):
    host, secret = agent()
    pid = _capture(controller, host, secret)
    flashback.queue_point_restore(host, pid, "alice")
    r = controller.get(f"/agents/{host}/restore-point/manifest",
                       params={"point_id": pid}, headers=hdr(secret))
    assert r.status_code == 200, r.text
    assert len(r.json()["entries"]) == 3


def test_a_wrong_agent_secret_is_refused_everywhere(controller, agent, flashback):
    host, _secret = agent()
    for method, path, body in (
        ("post", "restore-point/open", {"label": "x"}),
        ("post", "restore-point/manifest", {"point_id": 1, "entries": []}),
        ("post", "restore-point/blobs", {"point_id": 1, "blobs": []}),
        ("post", "restore-point/seal", {"point_id": 1}),
        ("post", "restore-point/start", {"job_id": 1}),
        ("post", "restore-point/finish", {"job_id": 1, "ok": True}),
    ):
        r = getattr(controller, method)(f"/agents/{host}/{path}", json=body,
                                        headers=hdr("wrong-secret"))
        assert r.status_code in (401, 403), f"{path}: {r.status_code}"
    for path in ("restore-point/pending", "restore-point/manifest",
                 "restore-point/blob"):
        r = controller.get(f"/agents/{host}/{path}",
                           params={"point_id": 1, "sha256": "x" * 64},
                           headers=hdr("wrong-secret"))
        assert r.status_code in (401, 403), f"{path}: {r.status_code}"


# ---------------------------------------------------------------------------
# bounded, because the relay holds the body
# ---------------------------------------------------------------------------
def test_an_oversized_manifest_chunk_is_refused(controller, agent, flashback):
    from backend import flashback as fbmod
    host, secret = agent()
    pid = controller.post(f"/agents/{host}/restore-point/open",
                          json={"label": "big"}, headers=hdr(secret)).json()["point_id"]
    too_many = [_ent(f"/etc/f{i}", b"x") for i in range(fbmod.MAX_MANIFEST_ENTRIES + 1)]
    r = controller.post(f"/agents/{host}/restore-point/manifest",
                        json={"point_id": pid, "entries": too_many}, headers=hdr(secret))
    assert r.status_code == 413, r.status_code
    assert "smaller pieces" in r.text


def test_an_oversized_blob_batch_is_refused(controller, agent, flashback):
    from backend import flashback as fbmod
    host, secret = agent()
    pid = controller.post(f"/agents/{host}/restore-point/open",
                          json={"label": "big"}, headers=hdr(secret)).json()["point_id"]
    blob = base64.b64encode(b"x" * (fbmod.MAX_BLOB_BATCH_BYTES + 16)).decode()
    r = controller.post(f"/agents/{host}/restore-point/blobs", json={
        "point_id": pid,
        "blobs": [{"sha256": "0" * 64, "content_b64": blob}]}, headers=hdr(secret))
    assert r.status_code == 413, r.status_code


# ---------------------------------------------------------------------------
# the round trip through the relay
# ---------------------------------------------------------------------------
def test_a_queued_restore_reaches_the_agent_and_reports_back(controller, agent,
                                                             flashback):
    host, secret = agent()
    pid = _capture(controller, host, secret)
    flashback.queue_point_restore(host, pid, "alice")

    job = controller.get(f"/agents/{host}/restore-point/pending",
                         headers=hdr(secret)).json()["job"]
    assert job and job["point_id"] == pid
    assert job["reboot"] is True
    controller.post(f"/agents/{host}/restore-point/start",
                    json={"job_id": job["id"]}, headers=hdr(secret))
    r = controller.post(f"/agents/{host}/restore-point/finish",
                        json={"job_id": job["id"], "ok": True, "note": "3 entries"},
                        headers=hdr(secret))
    assert r.status_code == 200, r.text
    jobs = flashback.restore_jobs(host)
    assert jobs[0]["status"] == "done"
    assert "3 entries" in jobs[0]["note"]
    assert controller.get(f"/agents/{host}/restore-point/pending",
                          headers=hdr(secret)).json()["job"] is None
