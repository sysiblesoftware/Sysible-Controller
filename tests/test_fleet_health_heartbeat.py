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
    import importlib
    from pathlib import Path
    import backend.db as _db
    importlib.reload(_db)
    # DB_PATH is a module constant (not read from env), so point it at a fresh
    # temp file AFTER reload — otherwise every test shares the dev DB and leftover
    # host_health rows make set-equality assertions flaky.
    _db.DB_PATH = Path(tempfile.mktemp(suffix=".db"))
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


def test_host_health_stores_hypervisor_fields(db):
    """The heartbeat carries a hypervisor role + running-guest count so the
    console can warn before rebooting a VM host; they round-trip through the DB,
    and default to None for plain servers / older agents that omit them."""
    db.upsert_host_health("vmhost", 1.0, 20, 20, 0.2, 8, 0, 0, 3600, "running", "/",
                          [], hyp="kvm", vms=4)
    db.upsert_host_health("plain", 1.0, 20, 20, 0.2, 2, 0, 0, 3600, "running", "/", [])
    allh = db.get_all_host_health()
    assert allh["vmhost"]["hyp"] == "kvm" and allh["vmhost"]["vms"] == 4
    assert allh["plain"]["hyp"] is None and allh["plain"]["vms"] is None


def test_agent_detect_hypervisor_shape():
    """_detect_hypervisor must always return (role|None, int, list) and never
    raise, whatever /proc looks like on the host running the test."""
    import host_agent.agent as a
    role, vms, vm_names = a._detect_hypervisor()
    assert role is None or isinstance(role, str)
    assert isinstance(vms, int) and vms >= 0
    assert isinstance(vm_names, list)


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


def test_health_from_stored_partial_when_no_sysd():
    """A pre-upgrade agent reports disk/mem/load but not sysd/failed/oom. The
    reading must still be served from heartbeat (graded on disk), marked
    'heartbeat-partial' so it can be told apart from a full-fidelity reading."""
    import webgui.server as s
    hr = {"ts": 1.0, "disk": 92, "mem": 40, "load1": 1.0, "cores": 4}
    r = s._health_from_stored({"id": "h3", "host": "h3", "environment": "prod"}, hr)
    assert r["from"] == "heartbeat-partial"
    assert r["verdict"] == "CRITICAL"      # disk>=90 alone drives the verdict
    assert r["online"] is True and r["ok"] is True
    assert r["failed"] == 0 and r["units"] == []   # unknown until agent upgrade


def test_metric_samples_downsampled_to_budget():
    """Wide windows must not return unbounded points: get_metric_samples caps
    per-host output at _METRIC_MAX_POINTS regardless of raw sample volume."""
    import backend.db as d
    raw = [{"t": float(i)} for i in range(5000)]
    out = d._downsample(raw)
    assert len(out) <= d._METRIC_MAX_POINTS + 1
    assert out[-1]["t"] == 4999.0          # newest point preserved
    assert out[0]["t"] == 0.0              # oldest anchor preserved
    # A list already within budget is returned unchanged (identity).
    small = [{"t": float(i)} for i in range(50)]
    assert d._downsample(small) is small


def test_host_bounded_passes_normal_and_kills_hang():
    """Read-only recon dispatches are wrapped so a wedged probe can't occupy the
    agent's serial task loop for the full 30-min agent command timeout. A normal
    command must pass through unchanged; a hang must be killed near the cap."""
    import subprocess
    import time
    import webgui.server as s
    ok = subprocess.run(s._host_bounded("echo hi", 60), shell=True,
                        capture_output=True, text=True)
    assert ok.stdout.strip() == "hi"
    t0 = time.time()
    subprocess.run(s._host_bounded("sleep 30", 1), shell=True, capture_output=True)
    assert (time.time() - t0) < 8, "hanging recon command was not bounded"


def test_posture_probes_are_tmo_wrapped():
    """The historically-unbounded posture probes (sshd -T, du /var/log, docker,
    pkg-manager reboot checks) must run under the $TMO timeout guard."""
    import client._api_dispatch as d
    sh = d._POSTURE_SH
    for probe in ("$TMO sshd -T", "$TMO du -sm /var/log", "$TMO docker ps -q",
                  "$TMO docker inspect", "$TMO needs-restarting",
                  "$TMO zypper needs-rebooting"):
        assert probe in sh, f"posture probe not $TMO-guarded: {probe}"


def test_agent_metrics_payload_carries_health_signals():
    import host_agent.agent as a
    hs = a._collect_health_signals([{"mount": "/", "pct": 42}, {"mount": "/var", "pct": 88}])
    assert set(hs) == {"failed", "units", "sysd", "oom", "mount"}
    assert hs["mount"] == "/var"           # worst-disk mount
    m = a._collect_metrics()
    if m:   # None only if /proc is unreadable (not on a normal host/CI)
        for k in ("failed", "units", "sysd", "oom", "mount", "uptime"):
            assert k in m["metrics"], k
