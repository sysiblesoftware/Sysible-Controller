"""Edition gating for the Community branch.

The Community edition caps the number of *agent hosts* (one per agent
enrollment / host_id) at HOST_LIMIT. SSH-only hosts are a Sysible Connect
concept and don't count. This is an honest-user limit: because
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

# RBAC seat caps for the Community edition. Same honest-user caveat as
# HOST_LIMIT: this is an open-source build, so these are limits an editor
# could lift - real enforcement lives in Enterprise. None == unlimited.
ROLE_LIMITS = {"superuser": 2, "sysadmin": 5}


def enforce_role_limit(role, current_count):
    """Raise HTTP 403 if adding another `role` would exceed its seat cap.
    `current_count` is how many of that role already exist."""
    limit = ROLE_LIMITS.get(role)
    if limit is None:
        return
    if current_count >= limit:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Community edition allows at most {limit} {role} account(s) "
                f"({current_count} already exist). Remove one first, or use the "
                f"Enterprise edition for more."
            ),
        )


def current_host_names():
    """Distinct AGENT host names right now. Agents are what count toward the host
    cap; the SSH transport (an agent's auto-created SSH mirror, and any
    Connect-only SSH host) lives in Sysible Connect and does not count here. Lazy
    import avoids a cycle (this module is imported by the routers that own the
    stores)."""
    names = set()
    try:
        from backend.db import list_agents
        for a in list_agents():
            names.add(a.get("hostname") or a.get("host_id"))
    except Exception:
        pass
    return {n for n in names if n}


def _distinct_agent_hosts(agent_records):
    """Pure/testable: distinct agent hosts from (host_id, ip) records. An agent
    host is identified by its host_id: two agents are ALWAYS distinct hosts, even
    if they share an IP (e.g. both report a bridge/VPN/NAT address) or a default
    hostname like 'localhost'. Returns the set of host_ids — its length is the
    enrolled-host count. `ip` is accepted for signature stability but not used to
    merge (merging two real agents by IP was hiding a genuinely-enrolled host)."""
    return {host_id for host_id, _ip in agent_records if host_id}


def host_identities():
    """Distinct AGENT hosts under management — the enrolled-host total and the
    license cap. Everything Sysible manages runs the agent; the SSH transport
    (an agent host's auto-created SSH mirror, plus any Connect-only SSH host) is
    a Sysible Connect concept and is NOT counted here. Each agent enrollment
    (host_id) is one host. Lazy import avoids a cycle."""
    agents = []
    try:
        from backend.db import list_agents
        for a in list_agents():
            agents.append((a.get("host_id"), a.get("ip")))
    except Exception:
        pass
    return _distinct_agent_hosts(agents)


def host_count():
    return len(host_identities())


def enforce_host_limit(candidate_name):
    """Raise HTTP 403 if enrolling `candidate_name` would push the managed-host
    count past HOST_LIMIT. Re-enrolling / updating an already-managed host is
    always allowed (it isn't a new host)."""
    if HOST_LIMIT is None:
        return
    # Re-enrolling/updating an already-managed name isn't a new host.
    if candidate_name and candidate_name in current_host_names():
        return
    count = host_count()  # distinct agent hosts (see host_identities)
    if count >= HOST_LIMIT:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Community edition is limited to {HOST_LIMIT} managed hosts "
                f"({count} already enrolled). Remove a host first, or use the "
                f"Enterprise edition to manage more."
            ),
        )


def edition_info():
    """Small dict the GUI shows so the limit is visible, not a surprise."""
    return {
        "edition": EDITION,
        "host_limit": HOST_LIMIT,
        "host_count": host_count(),
        "role_limits": ROLE_LIMITS,
    }
