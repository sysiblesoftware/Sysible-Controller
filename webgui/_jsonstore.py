"""Shared robustness helper for the small JSON side-stores (sudo_store, alerts,
suppressions, host_meta, schedules).

Two defects these stores all shared before this helper existed:
  1. Non-atomic writes — `Path.write_text(json.dumps(...))` opens the LIVE file
     with O_TRUNC, so a crash / disk-full / concurrent writer between truncate
     and flush left a truncated, invalid-JSON file. Every later `_load()` then
     hit its `except json.JSONDecodeError` and returned empty — the whole store
     silently read as blank, permanently, until hand-repaired.
  2. No serialization — load -> mutate -> save with no lock, so two concurrent
     writers (or a background thread + a request handler) lost one update.

`atomic_write_json` writes a temp file in the same directory then `os.replace`s
it over the target (atomic rename on POSIX), mirroring backend/agent_integrity.py.
Pair it with a module-level `threading.Lock` held across the whole
load->mutate->save in each store. Single process, so a plain Lock suffices.
"""
import json
import os
import tempfile
from pathlib import Path


def atomic_write_json(path, data, mode=0o600, indent=2):
    """Serialize `data` to `path` atomically: write a sibling temp file, fsync
    it closed, then rename over the target. A failure leaves the previous file
    intact rather than a half-written one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)  # atomic on the same filesystem
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
