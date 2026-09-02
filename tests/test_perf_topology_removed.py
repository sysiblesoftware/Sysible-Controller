"""Fleet Performance + Network Topology removal (regression guard).

The Performance view (time-series charts + per-host snapshot drill-down) and the
Topology map were removed, along with ONLY their exclusive plumbing: the
/api/fleet-metrics and /api/host-snapshot console routes, the controller's
/metrics/timeseries and /metrics/snapshot routes, and the metric_samples /
host_snapshot tables. Everything the surviving dashboard fleet-health path uses
(heartbeat `metrics` -> host_health -> /metrics/fleet-health -> /api/fleet-health)
must keep working, host removal must still succeed on a fresh DB, and agents that
predate the removal (still sending the perf-only `snapshot` field) must not be
rejected.
"""
import sqlite3
import time

import backend.db as db

from conftest import key_headers


def _login_override(srv):
    srv.app.dependency_overrides[srv.require_login] = lambda: "tester"


def _table_names():
    conn = sqlite3.connect(str(db.DB_PATH))
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


class TestRemovedRoutes:
    def test_bff_fleet_metrics_is_gone(self, bff):
        import webgui.server as srv
        _login_override(srv)
        try:
            assert bff.get("/api/fleet-metrics").status_code == 404
            assert bff.get("/api/fleet-metrics", params={"window": 3600}).status_code == 404
        finally:
            srv.app.dependency_overrides.clear()

    def test_bff_host_snapshot_is_gone(self, bff):
        import webgui.server as srv
        _login_override(srv)
        try:
            assert bff.get("/api/host-snapshot/x").status_code == 404
        finally:
            srv.app.dependency_overrides.clear()

    def test_controller_timeseries_and_snapshot_are_gone(self, controller):
        assert controller.get("/metrics/timeseries", headers=key_headers()).status_code == 404
        assert controller.get("/metrics/snapshot/x", headers=key_headers()).status_code == 404


class TestSurvivingHealthPath:
    def test_bff_fleet_health_still_served(self, bff, monkeypatch):
        """The dashboard's /api/fleet-health route is untouched (it shares nothing
        with the removed routes but a URL prefix). Stub the sweep so the test needs
        no live controller behind the BFF."""
        import webgui.server as srv
        _login_override(srv)
        canned = {"hosts": [], "cached": False, "ts": time.time()}
        monkeypatch.setattr(srv, "_fleet_health_sweep", lambda force_live=False: canned)
        try:
            r = bff.get("/api/fleet-health", params={"refresh": 1})
            assert r.status_code == 200, r.text
            assert "hosts" in r.json()
        finally:
            srv.app.dependency_overrides.clear()

    def test_heartbeat_metrics_still_land_in_fleet_health(self, controller, agent):
        """A heartbeat carrying `metrics` (with health signals) must still be
        persisted to host_health and served by GET /metrics/fleet-health; the
        perf-only scalars an older agent still attaches are ignored, not stored."""
        host_id, secret = agent()
        r = controller.post("/agents/heartbeat", json={
            "host_id": host_id, "agent_secret": secret,
            "metrics": {"disk": 42, "mem": 55, "load1": 0.5, "cores": 4,
                        "failed": 1, "oom": 0, "uptime": 1000, "sysd": "degraded",
                        "mount": "/", "units": ["foo.service"],
                        # perf-only keys from a pre-removal agent
                        "cpu": 12.5, "swap": 3, "net_rx": 1.0, "procs": 99},
        })
        assert r.status_code == 200, r.text
        r = controller.get("/metrics/fleet-health", headers=key_headers())
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) >= {"hosts", "now"}
        reading = body["hosts"][host_id]
        assert reading["disk"] == 42 and reading["failed"] == 1
        assert reading["sysd"] == "degraded" and reading["units"] == ["foo.service"]
        assert "cpu" not in reading and "procs" not in reading
        assert db.get_all_host_health()[host_id]["disk"] == 42

    def test_heartbeat_with_stray_snapshot_field_still_accepted(self, controller, agent):
        """Agents that predate the removal keep POSTing `snapshot` until they are
        updated; the controller must ignore it (no 422), and the health reading
        alongside it must still be stored."""
        host_id, secret = agent()
        r = controller.post("/agents/heartbeat", json={
            "host_id": host_id, "agent_secret": secret,
            "metrics": {"disk": 10, "mem": 20, "load1": 0.1, "cores": 2, "sysd": "running"},
            "snapshot": {"cpu": {"cores": [1, 2]}, "procs": [{"pid": 1}]},
        })
        assert r.status_code == 200, r.text
        assert db.get_all_host_health()[host_id]["disk"] == 10


class TestDatabase:
    def test_fresh_db_has_no_perf_tables_and_host_delete_works(self):
        names = _table_names()
        assert "metric_samples" not in names and "host_snapshot" not in names
        assert "host_health" in names
        db.create_or_update_agent("h-del", "web1", "linux", "6.1", "online",
                                  time.time(), "s", "10.0.0.1")
        assert db.agent_exists("h-del")
        db.delete_agent("h-del")            # must not raise 'no such table'
        assert not db.agent_exists("h-del")

    def test_init_db_drops_legacy_perf_tables(self):
        """Upgraded databases carried frozen metric_samples/host_snapshot tables;
        init_db drops them (idempotently) so nothing is left orphaned."""
        conn = sqlite3.connect(str(db.DB_PATH))
        conn.execute("CREATE TABLE IF NOT EXISTS metric_samples (host_id TEXT, ts REAL)")
        conn.execute("CREATE TABLE IF NOT EXISTS host_snapshot "
                     "(host_id TEXT PRIMARY KEY, ts REAL, data TEXT)")
        conn.execute("INSERT INTO metric_samples VALUES ('h', 1.0)")
        conn.commit()
        conn.close()
        assert {"metric_samples", "host_snapshot"} <= _table_names()
        db.init_db()
        db.init_db()                        # idempotent
        names = _table_names()
        assert "metric_samples" not in names and "host_snapshot" not in names
        assert "agents" in names and "host_health" in names
