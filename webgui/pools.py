"""Pools: a named, ordered set of fleet hosts that serve the same thing.

A pool is what makes rolling maintenance possible — it says which hosts must not
all go down together, how to take one out of rotation, and how to tell whether one
is fit to take traffic.

Storage and validation only, in the same shape as schedules.py: a JSON list at
run/webgui_pools.json, pure and testable. Everything that touches a host lives in
webgui/server.py, which owns dispatch.

A pool:
  {id, name, members:[host_id] (ORDERED — the roll order),
   provider: none|command|haproxy|keepalived,
   drain_cmd, undrain_cmd,          # provider=command
   health: {kind: tcp|http|command|agent, target, expect},
   max_unavailable, min_healthy, settle_secs,
   created_by, created_ts}

PROVIDERS. The drain half is the part that differs per balancer, so it is named
rather than assumed:

  none       — no balancer. Rolling still gives you one host at a time and a
               health gate between them, which is most of the value and needs no
               integration at all. This is the default deliberately: a pool you
               can define in ten seconds is a pool that gets defined.
  command    — operator-supplied drain/undrain commands. The escape hatch for
               anything we do not speak, and enough to drive HAProxy or keepalived
               today. Free-form root commands, so it carries the same superuser
               restriction as the run_command action.

Native haproxy and keepalived providers are NOT listed here on purpose. Both need
one thing this model does not answer yet: WHERE the balancer runs. `disable server
be/srv` goes to HAProxy's runtime socket, which may be on the member, on a pair of
dedicated balancers, or on every member of an active/active pair — and with more
than one balancer a drain has to reach all of them or the node keeps taking
traffic from the ones that were missed. Offering a provider that quietly drains
one of three balancers would be worse than offering none. Until that is settled,
`command` does the same job with the topology in the operator's hands.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

from webgui._jsonstore import atomic_write_json

PROVIDERS = {
    "none": "No balancer — one host at a time, health-gated",
    "command": "Custom drain / undrain commands",
}
# Providers whose configuration is a root command an operator typed. Same rule as
# the free-form scheduled actions: superuser only, because it sidesteps the sudo
# policy that constrains a sysadmin everywhere else.
FREEFORM_PROVIDERS = {"command"}

HEALTH_KINDS = {
    "agent": "The host is checking in",
    "tcp": "A TCP port accepts a connection",
    "http": "An HTTP URL answers 2xx/3xx",
    "command": "A command exits 0 on the host",
}
FREEFORM_HEALTH = {"command"}

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RUN_DIR = Path(os.getenv("SYSIBLE_RUN_DIR") or (_REPO_ROOT / "run"))
_DATA_FILE = _RUN_DIR / "webgui_pools.json"
_LOCK = threading.RLock()

# A pool that can lose an unbounded number of members at once is not a pool.
MAX_UNAVAILABLE_CAP = 32
SETTLE_CAP = 3600


def _load():
    try:
        rows = json.loads(_DATA_FILE.read_text())
        return rows if isinstance(rows, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save(rows):
    atomic_write_json(_DATA_FILE, rows)


def list_pools():
    with _LOCK:
        return _load()


def get_pool(pool_id):
    with _LOCK:
        for p in _load():
            if p.get("id") == pool_id:
                return p
    return None


_NAME_RE = re.compile(r"\A[\w .:/+-]{1,64}\Z")


def validate(pool: dict) -> None:
    """Raise ValueError on anything that would make a run unsafe or meaningless.

    The checks that matter are the two that a UI cannot be trusted to enforce,
    because a pool can also arrive from a PATCH: that the roll order has no
    duplicates, and that max_unavailable leaves something serving.
    """
    name = (pool.get("name") or "").strip()
    if not name or not _NAME_RE.match(name):
        raise ValueError("a pool needs a name (1-64 chars: letters, digits, "
                         "space . : / + - _)")

    members = pool.get("members") or []
    if not isinstance(members, list) or not all(isinstance(m, str) and m for m in members):
        raise ValueError("members must be a list of host ids")
    # A host listed twice would be drained twice in one run — and, with
    # max_unavailable > 1, could be counted as two members of serving capacity
    # that is really one machine.
    if len(set(members)) != len(members):
        dupes = sorted({m for m in members if members.count(m) > 1})
        raise ValueError("a host appears more than once in the roll order: "
                         + ", ".join(dupes))
    if len(members) < 2:
        raise ValueError("a pool needs at least two members — rolling through one "
                         "host means taking the service down, which is what this "
                         "exists to avoid")

    provider = pool.get("provider") or "none"
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider '{provider}'")
    if provider == "command":
        if not (pool.get("drain_cmd") or "").strip() or not (pool.get("undrain_cmd") or "").strip():
            raise ValueError("a custom provider needs both a drain and an undrain command")

    health = pool.get("health") or {}
    kind = health.get("kind") or "agent"
    if kind not in HEALTH_KINDS:
        raise ValueError(f"unknown health check '{kind}'")
    if kind in ("tcp", "http", "command") and not (health.get("target") or "").strip():
        raise ValueError(f"a {kind} health check needs a target")
    if kind == "tcp":
        try:
            port = int(str(health.get("target")).strip())
        except ValueError:
            raise ValueError("a tcp health check's target is a port number")
        if not (1 <= port <= 65535):
            raise ValueError("a tcp health check's port must be 1-65535")

    mu = _as_int(pool.get("max_unavailable"), 1)
    if mu < 1 or mu > MAX_UNAVAILABLE_CAP:
        raise ValueError(f"max_unavailable must be 1-{MAX_UNAVAILABLE_CAP}")
    mh = _as_int(pool.get("min_healthy"), 1)
    if mh < 1:
        raise ValueError("a pool must keep at least one member serving")
    # The check the whole feature rests on. Refuse it here, at the point it is
    # written down, rather than at 02:00 when the run finds out.
    if mu > len(members) - mh:
        raise ValueError(
            f"taking {mu} member(s) out of a {len(members)}-member pool would leave "
            f"{len(members) - mu} serving, below the {mh} this pool must keep. "
            f"Lower max_unavailable, or add members.")
    settle = _as_int(pool.get("settle_secs"), 0)
    if settle < 0 or settle > SETTLE_CAP:
        raise ValueError(f"settle_secs must be 0-{SETTLE_CAP}")


def _as_int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


_FIELDS = ("name", "members", "provider", "drain_cmd", "undrain_cmd", "health",
           "max_unavailable", "min_healthy", "settle_secs")


def create_pool(data: dict, created_by: str) -> dict:
    pool = {k: data.get(k) for k in _FIELDS}
    pool["provider"] = pool.get("provider") or "none"
    pool["health"] = pool.get("health") or {"kind": "agent"}
    pool["max_unavailable"] = _as_int(pool.get("max_unavailable"), 1)
    pool["min_healthy"] = _as_int(pool.get("min_healthy"), 1)
    pool["settle_secs"] = _as_int(pool.get("settle_secs"), 0)
    validate(pool)
    pool["id"] = uuid.uuid4().hex[:12]
    pool["created_by"] = created_by
    pool["created_ts"] = time.time()
    with _LOCK:
        rows = _load()
        rows.append(pool)
        _save(rows)
    return pool


def update_pool(pool_id: str, data: dict) -> dict | None:
    with _LOCK:
        rows = _load()
        for p in rows:
            if p.get("id") != pool_id:
                continue
            merged = dict(p)
            for k in _FIELDS:
                if k in data and data[k] is not None:
                    merged[k] = data[k]
            merged["max_unavailable"] = _as_int(merged.get("max_unavailable"), 1)
            merged["min_healthy"] = _as_int(merged.get("min_healthy"), 1)
            merged["settle_secs"] = _as_int(merged.get("settle_secs"), 0)
            # Validated BEFORE it replaces the stored one, so a rejected edit
            # leaves the working pool in place rather than a half-applied one.
            validate(merged)
            rows[rows.index(p)] = merged
            _save(rows)
            return merged
    return None


def delete_pool(pool_id: str) -> bool:
    with _LOCK:
        rows = _load()
        new = [p for p in rows if p.get("id") != pool_id]
        _save(new)
    return len(new) != len(rows)


def needs_superuser(pool: dict) -> bool:
    """Whether this pool's configuration runs operator-supplied root commands."""
    if (pool.get("provider") or "none") in FREEFORM_PROVIDERS:
        return True
    return ((pool.get("health") or {}).get("kind") or "agent") in FREEFORM_HEALTH
