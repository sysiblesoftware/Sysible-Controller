"""
Deployment self-awareness (backend/build_info.py + GET /version): the controller
reports which directory/commit it's actually running, so a wrong-directory deploy
can't stay silent.
"""
from backend import build_info
from conftest import key_headers


def test_info_reports_running_dir():
    i = build_info.info()
    # running_dir is the repo root that actually holds backend/build_info.py.
    assert i["running_dir"].endswith("Sysible-Controller") or "sysible" in i["running_dir"].lower()
    assert "restart_needed" in i and "commit" in i and "branch" in i


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
