"""
Scheduled fleet jobs for the web console — recurring maintenance the operator
sets once and forgets: patch scans, security/all updates, posture scans, reboots.

This module owns STORAGE + the next-run math only (pure, testable). The runner
thread and the executor (which needs the dispatch/actions machinery) live in
webgui/server.py to avoid an import cycle.

Jobs are a JSON list at run/webgui_schedules.json, mirroring sudo_store's model.
A job:
  {id, name, action, targets:[host_id]|[]=all, cadence:hourly|daily|weekly,
   at:"HH:MM", weekday:0-6 (weekly), enabled, created_by, created_ts,
   last_run, last_status:ok|error|running, last_detail, next_run}
"""
import datetime
import json
import os
import uuid
from pathlib import Path

# action -> human label; the executor in server.py maps these to real dispatch.
ACTIONS = {
    "patch_scan": "Rescan patch status (live)",
    "posture_scan": "Rescan compliance posture",
    "security_updates": "Install security updates",
    "all_updates": "Install all updates",
    "clean_pkg_cache": "Clean package cache",
    "vacuum_journal": "Vacuum journal logs (7 days)",
    "fstrim": "Trim filesystems (fstrim)",
    "clear_failed_units": "Clear failed units",
    "sync_time": "Sync clock now",
    "restart_service": "Restart a service",
    "run_command": "Run a shell command",
    "reboot": "Reboot",
}
# actions that need a free-text argument -> the field's label.
ARG_ACTIONS = {"restart_service": "Service name", "run_command": "Command"}
CADENCES = ("hourly", "daily", "weekly")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RUN_DIR = Path(os.getenv("SYSIBLE_RUN_DIR") or (_REPO_ROOT / "run"))
_DATA_FILE = _RUN_DIR / "webgui_schedules.json"


def _load():
    try:
        return json.loads(_DATA_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return []


def _save(jobs):
    _RUN_DIR.mkdir(parents=True, exist_ok=True)
    _DATA_FILE.write_text(json.dumps(jobs, indent=2))
    try:
        os.chmod(_DATA_FILE, 0o600)
    except OSError:
        pass


def _parse_hhmm(at):
    try:
        h, m = str(at or "02:00").split(":")
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (ValueError, TypeError):
        pass
    return 2, 0


def compute_next_run(job, from_ts=None):
    """Next epoch this job should fire, strictly after `from_ts` (now)."""
    now = datetime.datetime.fromtimestamp(from_ts if from_ts is not None else _now())
    cadence = job.get("cadence", "daily")
    h, m = _parse_hhmm(job.get("at"))
    if cadence == "hourly":
        nxt = now.replace(minute=m, second=0, microsecond=0)
        if nxt <= now:
            nxt += datetime.timedelta(hours=1)
        return nxt.timestamp()
    if cadence == "weekly":
        wd = int(job.get("weekday", 0)) % 7          # 0=Mon .. 6=Sun (Python weekday)
        nxt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        delta = (wd - nxt.weekday()) % 7
        nxt += datetime.timedelta(days=delta)
        if nxt <= now:
            nxt += datetime.timedelta(days=7)
        return nxt.timestamp()
    # daily (default)
    nxt = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if nxt <= now:
        nxt += datetime.timedelta(days=1)
    return nxt.timestamp()


def _now():
    import time
    return time.time()


def validate(action, cadence, arg=""):
    if action not in ACTIONS:
        raise ValueError(f"Unknown action: {action}")
    if cadence not in CADENCES:
        raise ValueError(f"Unknown cadence: {cadence}")
    if action in ARG_ACTIONS and not (arg or "").strip():
        raise ValueError(f"{ARG_ACTIONS[action]} is required for this action.")


def list_jobs():
    return _load()


def get_job(job_id):
    return next((j for j in _load() if j.get("id") == job_id), None)


def create_job(name, action, targets, cadence, at, weekday, created_by, arg=""):
    validate(action, cadence, arg)
    job = {
        "id": uuid.uuid4().hex[:12],
        "name": (name or ACTIONS[action]).strip(),
        "action": action,
        "arg": (arg or "").strip(),
        "targets": list(targets or []),
        "cadence": cadence,
        "at": at or "02:00",
        "weekday": int(weekday or 0),
        "enabled": True,
        "created_by": created_by,
        "created_ts": _now(),
        "last_run": None,
        "last_status": None,
        "last_detail": "",
    }
    job["next_run"] = compute_next_run(job)
    jobs = _load()
    jobs.append(job)
    _save(jobs)
    return job


def update_job(job_id, **fields):
    jobs = _load()
    for j in jobs:
        if j.get("id") == job_id:
            for k in ("name", "action", "arg", "targets", "cadence", "at", "weekday", "enabled"):
                if k in fields and fields[k] is not None:
                    j[k] = fields[k]
            validate(j["action"], j["cadence"], j.get("arg", ""))
            j["next_run"] = compute_next_run(j)
            _save(jobs)
            return j
    return None


def delete_job(job_id):
    jobs = _load()
    new = [j for j in jobs if j.get("id") != job_id]
    _save(new)
    return len(new) != len(jobs)


def record_run(job_id, status, detail):
    jobs = _load()
    for j in jobs:
        if j.get("id") == job_id:
            j["last_run"] = _now()
            j["last_status"] = status
            j["last_detail"] = (detail or "")[:500]
            j["next_run"] = compute_next_run(j)
            _save(jobs)
            return j
    return None


def due_jobs(now_ts=None):
    now = now_ts if now_ts is not None else _now()
    out = []
    for j in _load():
        if not j.get("enabled"):
            continue
        nr = j.get("next_run")
        if nr is None:
            # never computed (legacy) — schedule it forward, don't fire immediately
            update_job(j["id"], name=j.get("name"))
            continue
        if nr <= now:
            out.append(j)
    return out
