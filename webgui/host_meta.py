"""
Per-host operator metadata for the web console: tags, owner, notes, and a
criticality level. A JSON side-store keyed by the merged-host NAME (the label
the UI already identifies hosts by), so it applies uniformly to agent and SSH
hosts and is decoupled from the license/identity model. These are purely
operator annotations to help organize the fleet — nothing here affects
enrollment, counting, or dispatch.

Store: run/host_meta.json (mode 0600), mirroring sudo_store / schedules / alerts.
"""
import json
import os
import threading
from pathlib import Path

from webgui._jsonstore import atomic_write_json

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RUN_DIR = Path(os.getenv("SYSIBLE_RUN_DIR") or (_REPO_ROOT / "run"))
_DATA_FILE = _RUN_DIR / "host_meta.json"
_LOCK = threading.Lock()

CRITICALITY = ("low", "normal", "high", "critical")
_DEFAULT = {"tags": [], "owner": "", "notes": "", "criticality": "normal"}


def _load():
    try:
        data = json.loads(_DATA_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data):
    atomic_write_json(_DATA_FILE, data)


def all_meta():
    """{name: {tags, owner, notes, criticality}} for every host that has any."""
    return _load()


def get(name):
    rec = _load().get(name)
    return {**_DEFAULT, **rec} if rec else dict(_DEFAULT)


def set_meta(name, tags=None, owner=None, notes=None, criticality=None):
    """Update the fields that were supplied (None = leave as-is). Values are
    clamped/validated. An entry that ends up all-default is dropped to keep the
    store tidy. Returns the resulting record."""
    if not name:
        raise ValueError("host name required")
    if len(name) > 200:
        raise ValueError("host name too long")
    if tags is not None and not isinstance(tags, (list, tuple)):
        raise ValueError("tags must be a list")   # 400, not a 500 on iterating a non-list
    if criticality is not None and criticality not in CRITICALITY:
        raise ValueError(f"criticality must be one of {CRITICALITY}")
    with _LOCK:
        data = _load()
        cur = {**_DEFAULT, **(data.get(name) or {})}
        if tags is not None:
            seen, clean = set(), []
            for t in tags:
                t = str(t).strip()[:40]
                if t and t.lower() not in seen:
                    seen.add(t.lower()); clean.append(t)
            cur["tags"] = clean[:20]
        if owner is not None:
            cur["owner"] = str(owner).strip()[:120]
        if notes is not None:
            cur["notes"] = str(notes).strip()[:2000]
        if criticality is not None:
            cur["criticality"] = criticality
        if cur == _DEFAULT:
            data.pop(name, None)
        else:
            data[name] = cur
        _save(data)
    return cur
