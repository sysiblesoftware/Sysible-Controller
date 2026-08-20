"""A containerized controller updates by pulling a new IMAGE, not in place.

On a host install the console's "Update controller" runs a git-pull/systemd
self-update. In a container there's no git checkout and no systemd, so that path
can only fail. These pin that the container path returns clear image-pull guidance
(HTTP 200) instead of a 500, and that the update-availability check reports the
container state rather than a git error.
"""
import backend.app as app_module


def test_update_route_returns_container_guidance(controller, superuser_headers, monkeypatch):
    monkeypatch.setattr(app_module, "_is_container", lambda: True)
    r = controller.post("/controller/update", headers=superuser_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "container"
    assert "docker compose" in body["message"]
    assert "pull" in body["message"]


def test_update_available_reports_container(monkeypatch):
    monkeypatch.setattr(app_module, "_is_container", lambda: True)
    got = app_module._controller_update_available()
    assert got["container"] is True
    assert got["checked"] is False
    assert "image" in got["reason"].lower()


def test_update_route_requires_superuser(controller, sysadmin_headers, monkeypatch):
    monkeypatch.setattr(app_module, "_is_container", lambda: True)
    r = controller.post("/controller/update", headers=sysadmin_headers)
    assert r.status_code in (401, 403)
