"""
Browser-driven web-console rebuild (POST /controller/webgui/rebuild).

The console serves a compiled bundle (webgui/frontend/dist, untracked), so source
changes only show after a rebuild. This endpoint runs the same npm build the in-app
update does, on demand, so an admin never needs shell access. Superuser-gated.
"""


def test_rebuild_runs_the_builder(controller, superuser_headers, monkeypatch):
    called = {}

    def _fake_build():
        called["ran"] = True
        return {"ok": True, "steps": [{"name": "npm run build", "ok": True, "output": "built"}]}

    # The route imports backend.webgui_manager lazily; patch the attribute it calls.
    from backend import webgui_manager
    monkeypatch.setattr(webgui_manager, "install_dependencies", _fake_build)
    r = controller.post("/controller/webgui/rebuild", headers=superuser_headers)
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert called.get("ran") is True
    # Audited (reuses the documented controller_update_started event).
    import backend.db as db
    assert "controller_update_started" in [row["event"] for row in db.get_admin_audit_log()]


def test_rebuild_reports_failure(controller, superuser_headers, monkeypatch):
    from backend import webgui_manager
    monkeypatch.setattr(webgui_manager, "install_dependencies",
                        lambda: {"ok": False, "steps": [{"name": "npm run build", "ok": False,
                                                         "output": "boom"}]})
    r = controller.post("/controller/webgui/rebuild", headers=superuser_headers)
    assert r.status_code == 200 and r.json()["ok"] is False


def test_rebuild_requires_superuser(controller, sysadmin_headers):
    assert controller.post("/controller/webgui/rebuild", headers=sysadmin_headers).status_code == 403
