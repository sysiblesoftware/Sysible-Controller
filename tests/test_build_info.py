"""
Deployment self-awareness (backend/build_info.py + GET /version): the controller
reports which directory/commit it's actually running, so a wrong-directory deploy
can't stay silent.
"""
from backend import build_info
from conftest import key_headers


def test_info_reports_running_dir():
    """running_dir is the directory that actually holds the code running.

    Asserted by what is IN it, not by what it is CALLED. The check used to be
    that the path ended in "Sysible-Controller" or contained "sysible", which
    was never an invariant -- nothing stops an operator cloning to /srv/ctl --
    and it broke for real the moment this source was vendored into the platform
    tree at .../slop/apps/controller. A deploy from a surprising directory is
    exactly what this field exists to reveal, so the test cannot require the
    directory to be unsurprising.
    """
    import os

    i = build_info.info()
    d = i["running_dir"]
    assert os.path.isabs(d), f"running_dir is not an absolute path: {d!r}"
    assert os.path.isdir(d), f"running_dir does not exist: {d!r}"
    # It is the root that holds backend/build_info.py -- the module that just
    # reported it. That is the claim, and it is checkable.
    assert os.path.isfile(os.path.join(d, "backend", "build_info.py")), (
        f"running_dir {d!r} does not contain backend/build_info.py, so it is "
        f"not the root of the code that is running")
    assert os.path.samefile(
        d, os.path.dirname(os.path.dirname(os.path.abspath(build_info.__file__)))), (
        f"running_dir {d!r} is not where the imported build_info actually lives")
    assert "restart_needed" in i and "commit" in i and "branch" in i
    # The released build version (from version.py) is surfaced for the console's
    # "Current build" line — best-effort, so it may be None if version.py is absent.
    assert "version" in i


def test_restart_needed_flags_moved_head(monkeypatch):
    # Simulate the on-disk HEAD moving after the process started.
    monkeypatch.setattr(build_info, "_STARTED_COMMIT", "aaaaaaaaaaaa")
    monkeypatch.setattr(build_info, "_head_commit", lambda cwd=None: "bbbbbbbbbbbb")
    assert build_info.info()["restart_needed"] is True


def test_no_restart_needed_when_head_unchanged(monkeypatch):
    monkeypatch.setattr(build_info, "_STARTED_COMMIT", "cccccccccccc")
    monkeypatch.setattr(build_info, "_head_commit", lambda cwd=None: "cccccccccccc")
    assert build_info.info()["restart_needed"] is False


def test_version_endpoint(controller):
    r = controller.get("/version", headers=key_headers())
    assert r.status_code == 200
    body = r.json()
    assert "running_dir" in body and "restart_needed" in body


def test_version_endpoint_requires_api_key(controller):
    assert controller.get("/version").status_code == 401
