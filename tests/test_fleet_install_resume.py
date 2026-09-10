"""An install you walk away from is still findable when you come back.

A fleet update install runs on the CONTROLLER, in a background thread — not in
the operator's browser. The Update Hosts page held the job id in component state
only, so navigating away and returning lost the progress panel entirely while the
install carried on running: no progress, no per-host output, no way to learn how
it ended. These pin the endpoint the page re-attaches through.
"""
import os
import tempfile

os.environ.setdefault("SYSIBLE_API_KEY", "test-install-resume-key")
os.environ.setdefault("SYSIBLE_DATA_DIR", tempfile.mkdtemp(prefix="sysible-installres-"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import webgui.server as w  # noqa: E402

_ORIGIN = {"origin": "https://testserver", "host": "testserver"}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(w.api, "admin_login",
                        lambda u, p: {"role": "superuser", "token": "tok",
                                      "must_change_password": False, "sudo_connect": False})
    monkeypatch.setattr(w.api, "whoami", lambda: {"username": "op", "role": "superuser"})
    c = TestClient(w.app, base_url="https://testserver")
    assert c.post("/api/login", json={"username": "op", "password": "pw"},
                  headers=_ORIGIN).status_code == 200
    return c


@pytest.fixture(autouse=True)
def _empty_registry():
    with w._INSTALL_LOCK:
        w._INSTALL_JOBS.clear()
    yield
    with w._INSTALL_LOCK:
        w._INSTALL_JOBS.clear()


def _seed(job_id, kind="all", done=False, statuses=("running",), started=1000.0,
          finished=None):
    with w._INSTALL_LOCK:
        w._INSTALL_JOBS[job_id] = {
            "id": job_id, "kind": kind, "started": started, "done": done,
            "finished": finished,
            "hosts": [{"id": f"h{i}", "host": f"host-{i}", "environment": "Dev",
                       "status": s, "code": None, "output": "x" * 4096}
                      for i, s in enumerate(statuses)],
        }


def test_a_running_install_is_listed_so_the_page_can_re_attach(client):
    _seed("job-live", statuses=("done", "running", "queued"))
    jobs = client.get("/api/fleet-updates/install-jobs").json()["jobs"]
    assert [j["id"] for j in jobs] == ["job-live"]
    j = jobs[0]
    assert j["done"] is False and j["total"] == 3 and j["complete"] == 1 and j["failed"] == 0
    assert j["kind"] == "all"


def test_failures_are_counted_so_a_finished_job_reads_at_a_glance(client):
    _seed("job-x", done=True, statuses=("done", "failed", "failed"), finished=1500.0)
    j = client.get("/api/fleet-updates/install-jobs").json()["jobs"][0]
    assert j["done"] is True and j["complete"] == 3 and j["failed"] == 2


def test_newest_job_comes_first(client):
    _seed("older", started=100.0, done=True, finished=200.0)
    _seed("newer", started=900.0)
    ids = [j["id"] for j in client.get("/api/fleet-updates/install-jobs").json()["jobs"]]
    assert ids == ["newer", "older"]


def test_the_listing_carries_no_command_output(client):
    """The page fetches the ONE job it will show through install-status. Twenty
    jobs' worth of package-manager output on every page load would be absurd —
    and it is the kind of thing that quietly turns into a megabyte response."""
    _seed("job-x", statuses=("done", "done", "done"))
    body = client.get("/api/fleet-updates/install-jobs").text
    assert "xxxx" not in body
    assert len(body) < 2000, len(body)


def test_ages_are_computed_server_side(monkeypatch, client):
    """Handing out raw epochs would make the browser subtract them from ITS clock;
    a workstation an hour fast would decide a just-finished install was stale and
    refuse to restore it."""
    import time as _t
    _seed("job-x", done=True, started=_t.time() - 600, finished=_t.time() - 60)
    j = client.get("/api/fleet-updates/install-jobs").json()["jobs"][0]
    assert 0 <= j["finished_ago"] <= 90
    assert 550 <= j["age"] <= 700


def test_a_running_job_has_no_finished_age(client):
    _seed("job-x")
    assert client.get("/api/fleet-updates/install-jobs").json()["jobs"][0]["finished_ago"] is None


def test_nothing_in_flight_is_an_empty_list_not_an_error(client):
    r = client.get("/api/fleet-updates/install-jobs")
    assert r.status_code == 200 and r.json() == {"jobs": []}


def test_an_anonymous_caller_cannot_enumerate_installs():
    """The listing names hosts and what is being installed on them."""
    _seed("job-x")
    anon = TestClient(w.app, base_url="https://testserver")
    assert anon.get("/api/fleet-updates/install-jobs").status_code == 401


def test_the_registry_records_when_a_job_finished():
    """finished_ago is only meaningful if the finish is actually stamped — the
    background worker sets it in the same place it flips `done`."""
    import inspect
    src = inspect.getsource(w.fleet_updates_install)
    assert '["done"] = True' in src
    assert '["finished"] = _t.time()' in src


def test_a_fresh_job_starts_with_an_unset_finish():
    import inspect
    src = inspect.getsource(w.fleet_updates_install)
    assert '"finished": None' in src
