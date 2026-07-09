"""
Regression: installing updates on an integrity-quarantined host must FAIL FAST
with an actionable reason, not queue a task that never runs and then spin the
BFF's 900s poll deadline (a silent 15-minute spinner — the "install security
updates just sits there" bug).
"""
import webgui.server as srv


def test_install_one_fails_fast_on_quarantined_host(monkeypatch):
    # If _install_one ever reached dispatch for a quarantined host it would try to
    # talk to the controller; make that explode so the test proves we return
    # BEFORE dispatching.
    monkeypatch.setattr(srv.dispatch, "run_on_entry",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("dispatched to a quarantined host")))
    entry = {"id": "h1", "label": "rocky-1", "integrity_quarantined": True}
    res = srv._install_one(entry, "zypper --non-interactive patch --category security")
    assert res["ok"] is False
    assert "quarantin" in res["output"].lower()


def test_install_one_not_short_circuited_for_healthy_host(monkeypatch):
    # A healthy host still dispatches (sync result path returns cleanly here).
    monkeypatch.setattr(srv.dispatch, "run_on_entry",
                        lambda *a, **k: {"sync": True, "code": 0, "stdout": "ok", "stderr": ""})
    entry = {"id": "h2", "label": "web-1", "integrity_quarantined": False}
    res = srv._install_one(entry, "true", token=None)
    assert res["ok"] is True
