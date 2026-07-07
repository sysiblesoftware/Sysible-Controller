"""
Finding suppressions for the web console: silence a specific dashboard finding
(a compliance/posture signal or a health signal) on a host or a whole
environment, with a reason and an expiry policy. Lets an operator say "SSH root
login is required on this box" or "remind me about this reboot after the host
actually reboots" instead of staring at a permanent amber chip.

A JSON side-store at run/webgui_suppressions.json (mode 0600), mirroring
host_meta / schedules / sudo_store.

One suppression:
  {id, scope:"host"|"env", target:<host name>|<env name>, key:<finding key>,
   type:"env_necessity"|"acknowledged"|"snooze"|"reboot",
   reason, until:<epoch|None>, boot_epoch:<baseline|None>,
   created_by, created_ts}

Expiry is split by where the needed data lives:
  - "snooze" is time-based (until) -> pruned HERE on read (no host data needed).
  - "reboot" clears once the host's boot time advances past `boot_epoch`; only
    the console knows the live boot time (from the posture sweep), so that
    evaluation happens client-side and the console removes it when it clears.
  - "env_necessity"/"acknowledged" are indefinite (manual removal only).
"""
import json
import os
import time
import uuid
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
import threading
from webgui._jsonstore import atomic_write_json

_RUN_DIR = Path(os.getenv("SYSIBLE_RUN_DIR") or (_REPO_ROOT / "run"))
_DATA_FILE = _RUN_DIR / "webgui_suppressions.json"
_LOCK = threading.RLock()  # serialize load->mutate->save (list_all prunes on read)

SCOPES = ("host", "env")
TYPES = ("env_necessity", "acknowledged", "snooze", "reboot")

# Curated finding keys the dashboard can suppress (the compliance strip signals
# plus the two health-derived ones). Kept in sync with POSTURE_SIGNALS + the
# health signals in the SPA. Validation is lenient on unknown keys (bounded
# string) so the front end can evolve without a lockstep backend change.
KNOWN_KEYS = {
    "reboot_required", "ssh_root_login", "firewall_disabled", "mac_not_enforcing",
    "eol_os", "risky_accounts", "cert_expiring", "time_unsynced",
    "disk_critical", "failed_units",
}


def _load():
    try:
        rows = json.loads(_DATA_FILE.read_text())
        return rows if isinstance(rows, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save(rows):
    atomic_write_json(_DATA_FILE, rows)


def _clean_str(v, cap):
    return str(v or "").strip()[:cap]


def list_all():
    """Every non-expired suppression. Time-based snoozes whose `until` has passed
    are pruned here (they need no host data); reboot/indefinite ones are kept for
    the console to evaluate/remove."""
    now = time.time()
    with _LOCK:
        rows = _load()
        kept = [r for r in rows if not (r.get("type") == "snooze"
                                        and r.get("until") and r["until"] <= now)]
        if len(kept) != len(rows):
            _save(kept)
    return kept


# Snooze presets the UI offers -> seconds.
SNOOZE_SECONDS = {"1h": 3600, "1d": 86400, "7d": 7 * 86400, "30d": 30 * 86400}


def add(scope, target, key, stype, created_by, reason="", snooze=None, boot_epoch=None):
    """Create a suppression. `snooze` is one of SNOOZE_SECONDS keys (for
    type="snooze"); `boot_epoch` is the host's current boot time baseline (for
    type="reboot"). Returns the stored record. Raises ValueError on bad input."""
    scope = (scope or "").strip().lower()
    stype = (stype or "").strip().lower()
    key = _clean_str(key, 64)
    target = _clean_str(target, 200)
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    if stype not in TYPES:
        raise ValueError(f"type must be one of {TYPES}")
    if not target:
        raise ValueError("target is required")
    if not key:
        raise ValueError("finding key is required")
    if stype == "reboot" and scope != "host":
        raise ValueError("'remind after next reboot' only applies to a single host")

    rec = {
        "id": uuid.uuid4().hex[:12],
        "scope": scope,
        "target": target,
        "key": key,
        "type": stype,
        "reason": _clean_str(reason, 300),
        "until": None,
        "boot_epoch": None,
        "created_by": created_by,
        "created_ts": time.time(),
    }
    if stype == "snooze":
        secs = SNOOZE_SECONDS.get(str(snooze))
        if not secs:
            raise ValueError(f"snooze must be one of {sorted(SNOOZE_SECONDS)}")
        rec["until"] = time.time() + secs
    if stype == "reboot":
        try:
            rec["boot_epoch"] = int(boot_epoch)
        except (TypeError, ValueError):
            rec["boot_epoch"] = 0   # unknown baseline -> clears on any observed boot time

    # Replace any existing suppression with the same scope/target/key so toggling
    # doesn't stack duplicates.
    with _LOCK:
        rows = [r for r in _load()
                if not (r.get("scope") == scope and r.get("target") == target and r.get("key") == key)]
        rows.append(rec)
        _save(rows)
    return rec


def remove(supp_id):
    with _LOCK:
        rows = _load()
        new = [r for r in rows if r.get("id") != supp_id]
        _save(new)
    return len(new) != len(rows)
