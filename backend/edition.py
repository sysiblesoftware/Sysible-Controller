"""Edition gating for the Community branch.

The Community edition caps the number of *managed hosts* (agent + SSH,
de-duplicated by name) at HOST_LIMIT. This is an honest-user limit: because
this branch is open source, the check below can be removed by anyone editing
the source - genuine, tamper-resistant enforcement belongs in the Enterprise
edition (a separate, license-gated build), not here. Set HOST_LIMIT to None
to lift the cap, which is exactly what an Enterprise build does.

Keeping it in one tiny module means there's a single, clearly-labelled place
that defines the edition, rather than the limit being smeared across the
codebase.
"""
from fastapi import HTTPException

EDITION = "community"
HOST_LIMIT = 10  # None == unlimited (Enterprise)


def current_host_names():
    """The set of distinct managed-host names right now - agent hostnames
    plus SSH host names, so a host enrolled both ways counts once. Imports
    are lazy to avoid an import cycle (this module is imported by the routers
    that own those stores)."""
    names = set()
    try:
        from backend.db import list_agents
        for a in list_agents():
            names.add(a.get("hostname") or a.get("host_id"))
    except Exception:
        pass
    try:
        from backend.remote_routes import load_hosts
        names |= set((load_hosts() or {}).keys())
    except Exception:
        pass
    return {n for n in names if n}


def host_count():
    return len(current_host_names())


def enforce_host_limit(candidate_name):
    """Raise HTTP 403 if enrolling `candidate_name` would push the managed-host
    count past HOST_LIMIT. Re-enrolling / updating an already-managed host is
    always allowed (it isn't a new host)."""
    if HOST_LIMIT is None:
        return
    names = current_host_names()
    if candidate_name and candidate_name in names:
        return
    if len(names) >= HOST_LIMIT:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Community edition is limited to {HOST_LIMIT} managed hosts "
                f"({len(names)} already enrolled). Remove a host first, or use the "
                f"Enterprise edition to manage more."
            ),
        )


def edition_info():
    """Small dict the GUI shows so the limit is visible, not a surprise."""
    return {
        "edition": EDITION,
        "host_limit": HOST_LIMIT,
        "host_count": host_count(),
    }
