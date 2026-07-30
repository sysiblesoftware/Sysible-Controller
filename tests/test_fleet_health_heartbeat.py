"""Fleet-health served from heartbeat data (the speedup path).

Agents now report failed-units/systemd/OOM alongside disk/mem/load on their
periodic heartbeat; the controller stores the latest reading per host, and the
dashboard sweep grades a host from that stored reading instead of dispatching a
live probe to every host on every load. These tests pin the correctness-critical
pieces: the DB roundtrip, the verdict thresholds (identical to the on-host shell),
the stored→reading builder, and that the agent's metrics payload carries the
health signals.
"""
import os
import tempfile

import pytest


@pytest.fixture()
def db():
    os.environ["SYSIBLE_DB_PATH"] = tempfile.mktemp(suffix=".db")
    import importlib
    import backend.db as _db
    importlib.reload(_db)
    _db.init_db()
    return _db


def test_host_health_roundtrip(db):
    db.upsert_host_health("h1", 1000.0, 92, 40, 1.2, 4, 0, 0, 3600, "running", "/", [])
    db.upsert_host_health("h2", 1001.0, 50, 30, 0.5, 2, 2, 1, 100, "degraded", "/var",
                          ["a.service", "b.service"])
    db.upsert_host_health("h3", 1002.0, 10, 10, 0.1, 1, 0, 0, 5, None, None, [])  # old agent
    allh = db.get_all_host_health()
    assert set(allh) == {"h1", "h2", "h3"}
    assert allh["h2"]["units"] == ["a.service", "b.service"]
    assert allh["h2"]["sysd"] == "degraded"
    assert allh["h3"]["sysd"] is None            # pre-health agent → caller live-probes it


def test_upsert_overwrites_one_row_per_host(db):
    db.upsert_host_health("h1", 1.0, 10, 10, 0.1, 1, 0, 0, 1, "running", "/", [])
    db.upsert_host_health("h1", 2.0, 95, 10, 0.1, 1, 0, 0, 1, "running", "/", [])
    allh = db.get_all_host_health()
    assert len(allh) == 1 and allh["h1"]["disk"] == 95 and allh["h1"]["ts"] == 2.0


@pytest.mark.parametrize("disk,failed,sysd,oom,expected", [
    (10, 0, "running", 0, "OK"),
    (80, 0, "running", 0, "WARNING"),      # disk >= 80
    (50, 1, "running", 0, "WARNING"),      # failed >= 1
    (50, 0, "degraded", 0, "WARNING"),     # systemd degraded
    (50, 0, "running", 1, "WARNING"),      # OOM kill
    (90, 0, "running", 0, "CRITICAL"),     # disk >= 90
    (50, 3, "running", 0, "CRITICAL"),     # failed >= 3
    (95, 5, "degraded", 2, "CRITICAL"),    # worst wins
    (None, None, "", None, "OK"),          # missing → safe default
])
def test_verdict_matches_shell_thresholds(disk, failed, sysd, oom, expected):
    import webgui.server as s
    assert s._health_verdict(disk, failed, sysd, oom) == expected


def test_health_from_stored_builds_reading():
    import webgui.server as s
    hr = {"ts": 1.0, "disk": 50, "mem": 30, "load1": 0.5, "cores": 2, "failed": 2,
          "oom": 1, "uptime": 100, "sysd": "degraded", "mount": "/var",
          "units": ["a.service", "b.service"]}
    r = s._health_from_stored({"id": "h2", "host": "h2", "environment": "prod"}, hr)
    assert r["verdict"] == "WARNING"       # failed>=1 and oom>=1
    assert r["from"] == "heartbeat"        # marks it as served without a probe
    assert r["online"] is True and r["ok"] is True and r["error"] is None
    assert r["units"] == ["a.service", "b.service"]
    assert r["disk"] == 50 and r["mount"] == "/var" and r["failed"] == 2


def test_agent_metrics_payload_carries_health_signals():
    import host_agent.agent as a
    hs = a._collect_health_signals([{"mount": "/", "pct": 42}, {"mount": "/var", "pct": 88}])
    assert set(hs) == {"failed", "units", "sysd", "oom", "mount"}
    assert hs["mount"] == "/var"           # worst-disk mount
    m = a._collect_metrics()
    if m:   # None only if /proc is unreadable (not on a normal host/CI)
        for k in ("failed", "units", "sysd", "oom", "mount", "uptime"):
            assert k in m["metrics"], k
