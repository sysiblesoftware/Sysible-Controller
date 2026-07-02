"""
Controller-side agent integrity: seal a baseline manifest per host, compare
each heartbeat's self-measurement against it, and quarantine on mismatch
(PROTOTYPE / RFC). See docs/PRIVILEGE_DISPATCHER_RFC.md.

Enforcement lives HERE, on the controller, not on the agent - the controller is
the trust anchor the attacker doesn't control. A quarantined host keeps
heartbeating but is handed no tasks (see poll_agent_tasks), and its secret can
be revoked to lock it out entirely.

Sealing model (prototype): trust-on-first-use - the first measurement a host
reports becomes its baseline. Simple and non-disruptive for existing hosts, but
weaker than production should be: a host already tampered at first contact seals
a bad baseline. The stronger seal is to compute the expected manifest from the
exact agent files the controller SHIPS in the bundle (backend/agent_bundle.py)
and pin it at enroll; that's noted as the production refinement.

State is a JSON side-store keyed by host_id, mirroring agent_ssh_state.json.
"""
import json
import os
import time

_STATE_FILE = os.getenv(
    "SYSIBLE_INTEGRITY_STATE",
    os.path.join(os.getenv("SYSIBLE_DATA_DIR", "/opt/sysible"), "agent_integrity_state.json"),
)


def _load():
    try:
        with open(_STATE_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data):
    os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
    tmp = _STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, _STATE_FILE)


def compare(reported, baseline):
    """Return a list of human-readable mismatch strings (empty == matches)."""
    out = []
    if not isinstance(reported, dict) or not isinstance(baseline, dict):
        return ["malformed measurement"]
    if reported.get("version") != baseline.get("version"):
        out.append(f"version changed: {baseline.get('version')!r} -> {reported.get('version')!r}")
    rfiles = reported.get("files", {}) or {}
    bfiles = baseline.get("files", {}) or {}
    for path, expected in bfiles.items():
        got = rfiles.get(path)
        if got is None:
            out.append(f"{path}: no longer reported")
        elif got != expected:
            exp_s = expected[:12] if isinstance(expected, str) else expected
            got_s = got[:12] if isinstance(got, str) else got
            out.append(f"{path}: hash changed ({exp_s}... -> {got_s}...)")
    for path in rfiles:
        if path not in bfiles:
            out.append(f"{path}: new file not in baseline")
    return out


def evaluate(host_id, measurements):
    """Compare a heartbeat's measurements against the host's sealed baseline.
    Trust-on-first-use seals the baseline. Returns the new status dict. Never
    raises - integrity must not be able to break the heartbeat path."""
    try:
        data = _load()
        rec = data.get(host_id)
        if rec is None or "baseline" not in rec:
            # First sighting: seal it.
            data[host_id] = {"baseline": measurements, "status": "ok",
                             "sealed_at": time.time(), "mismatches": []}
            _save(data)
            return {"status": "ok", "sealed": True}

        mismatches = compare(measurements, rec["baseline"])
        if mismatches:
            rec["status"] = "quarantined"
            rec["mismatches"] = mismatches
            rec["flagged_at"] = time.time()
        else:
            rec["status"] = "ok"
            rec["mismatches"] = []
        data[host_id] = rec
        _save(data)
        return {"status": rec["status"], "mismatches": rec.get("mismatches", [])}
    except Exception as e:  # pragma: no cover
        return {"status": "error", "error": str(e)}


def is_quarantined(host_id):
    rec = _load().get(host_id) or {}
    return rec.get("status") == "quarantined"


def status(host_id):
    return _load().get(host_id) or {"status": "unknown"}


def rebaseline(host_id):
    """Admin action after a legitimate upgrade: drop the sealed baseline so the
    next heartbeat re-seals and the host is no longer quarantined."""
    data = _load()
    if host_id in data:
        del data[host_id]
        _save(data)
