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


def test_the_hint_names_the_cli_the_docker_host_actually_has(monkeypatch):
    """It used to say `sysible_controller update`. That is the NATIVE install's
    self-update: it only exists in /usr/local/bin after install_sysible.sh has run,
    and it works by rsyncing a git checkout into /opt/sysible — neither of which is
    true of a container deployment. A containerized host has `sysible_ctl`.

    Doubly confusing because `sysible_controller` DOES ship inside the image, so an
    operator following the message found the command, ran it, and hit the one
    subcommand that cannot work there."""
    monkeypatch.setattr(app_module, "_is_container", lambda: True)
    hint = app_module._container_update_hint()
    assert "sysible_ctl controller update" in hint
    # Never point a containerized operator at the native CLI. Checked as a word so
    # "sysible_ctl controller update" doesn't count as a match.
    import re
    assert not re.search(r"\bsysible_controller\b", hint), hint

    reason = app_module._controller_update_available()["reason"]
    assert "sysible_ctl controller update" in reason
    assert not re.search(r"\bsysible_controller\b", reason), reason


def test_the_hint_rebuilds_rather_than_only_recreating():
    """The reference stack builds the image from source, so `docker compose pull`
    fetches nothing and a bare `up -d` recreates the SAME image — the update has to
    be a --build or it silently does nothing."""
    hint = app_module._container_update_hint()
    assert "up -d --build" in hint


def test_the_in_image_cli_sends_host_only_verbs_to_the_right_tool():
    """The CLI inside the container refuses start/stop/restart/update/destroy and
    names the host tool. Pinned here because the console message and the CLI are
    two places an operator reads the same instruction from."""
    import os
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "sysible_controller"), encoding="utf-8") as fh:
        src = fh.read()
    guard = src[src.index('if [[ "$IN_CONTAINER" == "1" ]]; then'):]
    guard = guard[:guard.index("\nfi\n")]
    assert "start|stop|restart|update|destroy" in guard
    assert "sysible_ctl $1" in guard, "the guard must name the host-side manager"
    # Same rebuild requirement as the console hint.
    assert "up -d --build" in guard
