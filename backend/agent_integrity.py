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
import tempfile
import threading
import time

_STATE_FILE = os.getenv(
    "SYSIBLE_INTEGRITY_STATE",
    os.path.join(os.getenv("SYSIBLE_DATA_DIR", "/opt/sysible"), "agent_integrity_state.json"),
)

# evaluate()/mark_updating() are load->mutate->save on the whole dict and are
# reached from the heartbeat handler, which FastAPI runs in a threadpool — so
# they execute concurrently across hosts. Without a lock two heartbeats both
# read the pre-mutation snapshot and the second _save() clobbers the first
# (a lost update: e.g. host A's just-written quarantine is silently erased).
# One process, so a plain Lock held across the whole load->mutate->save is
# enough. Every public mutator below takes it.
_LOCK = threading.Lock()

# How long a host that once reported an integrity manifest may go WITHOUT one
# before it is quarantined. A measuring agent attaches its manifest to every
# heartbeat, so a compromised agent could otherwise evade the whole control
# simply by dropping the `measurements` field. A short grace absorbs a transient
# measure() hiccup (the next heartbeat ~1.5s later clears it) without letting a
# sustained omission slip through. Tune with SYSIBLE_INTEGRITY_MISSING_GRACE_S.
try:
    _MISSING_GRACE_S = int(os.getenv("SYSIBLE_INTEGRITY_MISSING_GRACE_S", "300"))
except ValueError:
    _MISSING_GRACE_S = 300

# How long after a controller-initiated agent update the host's changed
# self-measurement is accepted (re-sealed) rather than quarantined. Generous by
# default so an offline host that reconnects the same day still re-seals cleanly
# on the update it applied; tune with SYSIBLE_INTEGRITY_RESEAL_WINDOW_S.
try:
    _RESEAL_WINDOW_S = int(os.getenv("SYSIBLE_INTEGRITY_RESEAL_WINDOW_S", str(24 * 60 * 60)))
except ValueError:
    _RESEAL_WINDOW_S = 24 * 60 * 60


def _load():
    try:
        with open(_STATE_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data):
    """Atomic + private write: a UNIQUE sibling temp file (so two concurrent
    writers can't clobber a shared fixed `.tmp`), fsync'd and chmod 0600 (the
    sealed baselines are integrity-sensitive and were previously left at the
    umask default), then renamed over the target. A failure leaves the previous
    state file intact rather than a half-written one."""
    d = os.path.dirname(_STATE_FILE)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".integrity-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, _STATE_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
      with _LOCK:
        data = _load()
        rec = data.get(host_id)

        # A measurement arrived, so reset any "stopped reporting" evasion timer
        # note_missing() may have started (see below).
        if rec is not None:
            rec.pop("missing_since", None)

        # Controller-initiated update window: when an admin pushed an agent
        # update to this host, its agent files (and version) are EXPECTED to
        # change, so a diverged measurement is legitimate, not tampering. While
        # the window is open we re-seal the baseline to whatever the host now
        # reports (and clear any standing quarantine) instead of flagging it —
        # this survives the offline/poll-ordering race (the host heartbeats its
        # old measurement, gets the update task, restarts, then reports the new
        # one; any heartbeat in the window just re-seals). See mark_updating().
        if rec is not None and rec.get("reseal_until", 0) > time.time():
            rec["baseline"] = measurements
            rec["status"] = "ok"
            rec["mismatches"] = []
            rec["sealed_at"] = time.time()
            data[host_id] = rec
            _save(data)
            return {"status": "ok", "resealed": True}

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


def note_missing(host_id):
    """Called on a heartbeat that carried NO integrity manifest. A host that has
    never sealed a baseline (an older agent that predates integrity, or one
    still in the never-measured state) is left alone — there is nothing to
    enforce. But a host that DID seal a baseline and has now stopped reporting
    is treated as tampering: a compromised agent could otherwise defeat the
    whole control by dropping one JSON key. We arm a timer on the first miss and
    only quarantine once the omission persists past the grace window, so a
    single transient measure() failure doesn't flap a healthy host. Never raises
    — like evaluate(), integrity must not break the heartbeat path."""
    try:
      with _LOCK:
        data = _load()
        rec = data.get(host_id)
        # Never measured, or an update is legitimately in flight → ignore.
        if rec is None or "baseline" not in rec:
            return {"status": "unknown"}
        if rec.get("reseal_until", 0) > time.time():
            return {"status": rec.get("status", "ok")}
        if rec.get("status") == "quarantined":
            return {"status": "quarantined"}
        now = time.time()
        started = rec.get("missing_since")
        if not started:
            rec["missing_since"] = now
            data[host_id] = rec
            _save(data)
            return {"status": rec.get("status", "ok"), "missing": True}
        if now - started >= _MISSING_GRACE_S:
            rec["status"] = "quarantined"
            rec["mismatches"] = ["stopped reporting integrity measurements "
                                 "(possible tampering/evasion)"]
            rec["flagged_at"] = now
            data[host_id] = rec
            _save(data)
            return {"status": "quarantined", "mismatches": rec["mismatches"]}
        return {"status": rec.get("status", "ok"), "missing": True}
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
    with _LOCK:
        data = _load()
        if host_id in data:
            del data[host_id]
            _save(data)


def mark_updating(host_id, window_s=None):
    """Called when the controller pushes an agent update to this host: open a
    re-seal window so the host's expected file/version change (from the new
    agent) is accepted and re-sealed rather than quarantined. Also clears any
    standing quarantine immediately so the console reflects the pending update.
    Never raises — a bookkeeping failure must not block the update push."""
    try:
      with _LOCK:
        window = _RESEAL_WINDOW_S if window_s is None else int(window_s)
        data = _load()
        rec = data.get(host_id) or {}
        rec["reseal_until"] = time.time() + window
        # Don't leave a host visibly quarantined for the very change we asked for.
        if rec.get("status") == "quarantined":
            rec["status"] = "ok"
            rec["mismatches"] = []
        data[host_id] = rec
        _save(data)
    except Exception:
        pass
