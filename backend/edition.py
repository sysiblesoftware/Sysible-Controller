"""Edition gating for the Community branch.

The Community edition caps the number of *managed hosts* (agent + SSH,
de-duplicated per physical host) at HOST_LIMIT. This is an honest-user limit: because
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


def _distinct_hosts(agent_records, ssh_records):
    """Group agent + SSH records into distinct PHYSICAL hosts; return the set of
    component roots (its length is the host count). Pure and side-effect free so
    it can be unit-tested without a database.

    `agent_records` is an iterable of (host_id, hostname, ip); `ssh_records` an
    iterable of (name, ip). The merge rules are deliberately NOT a blanket union
    over name-OR-ip (which over-merged — see below):

      * Each agent enrollment is its own host, keyed by host_id. Two DISTINCT
        agents are never merged just because they share a hostname: a default
        name like 'localhost'/'ubuntu' is not evidence of the same machine.
      * An agent and an SSH record that share a hostname are the same machine
        reached two ways  ->  one host.
      * Any records that share an IP are the same physical machine  ->  one host.

    This mirrors how the web console collapses its host list
    (client/_api_dispatch.list_merged_hosts: merge agent+SSH by name, then
    dedupe by IP), so the licensed 'Hosts enrolled' count agrees with the
    Online/health counts on the dashboard. The old code unioned any two records
    sharing a hostname OR an IP, transitively, so two different machines that
    happened to share a default hostname collapsed into one — which made 'Hosts
    enrolled' read LOWER than 'Online' (impossible) on such a fleet.
    """
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    # (key, name, ip, kind) — key uniquely identifies the record's own component.
    # Names are compared RAW (no case/whitespace folding) so this matches the web
    # console's merge exactly (client/_api_dispatch.merge_duplicate_host_entries
    # groups by the raw label) and edition.current_host_names (also raw). If these
    # three ever normalized differently, the licensed count could disagree with
    # the displayed/probed host list again — the whole bug this function fixes.
    recs = []
    for host_id, hostname, ip in agent_records:
        recs.append((("agent", host_id),
                     hostname or host_id or "",
                     (ip or "").strip(), "agent"))
    for name, ip in ssh_records:
        nm = name or ""
        recs.append((("ssh", nm), nm, (ip or "").strip(), "ssh"))

    recs = [r for r in recs if r[1] or r[2]]  # drop wholly-empty records
    for key, *_ in recs:
        find(key)

    # An agent and an SSH record sharing a hostname are one host (same machine
    # reached two ways). Note: only agent<->ssh, never agent<->agent.
    agents_by_name, ssh_by_name = {}, {}
    for key, name, ip, kind in recs:
        if name:
            (agents_by_name if kind == "agent" else ssh_by_name).setdefault(name, []).append(key)
    for name, ssh_keys in ssh_by_name.items():
        for a_key in agents_by_name.get(name, []):
            for s_key in ssh_keys:
                union(a_key, s_key)

    # Any records sharing an IP are the same physical machine.
    by_ip = {}
    for key, name, ip, kind in recs:
        if ip:
            by_ip.setdefault(ip, []).append(key)
    for ip, keys in by_ip.items():
        for k in keys[1:]:
            union(keys[0], k)

    return {find(key) for key, *_ in recs}


def host_identities():
    """Distinct PHYSICAL hosts under management (agent + SSH). Lazy imports
    avoid an import cycle (this module is imported by the routers that own those
    stores). See _distinct_hosts for the merge rules."""
    agents = []
    try:
        from backend.db import list_agents
        for a in list_agents():
            agents.append((a.get("host_id"), a.get("hostname"), a.get("ip")))
    except Exception:
        pass
    ssh = []
    try:
        from backend.remote_routes import load_hosts
        for name, h in (load_hosts() or {}).items():
            ssh.append((name, h.get("ip")))
    except Exception:
        pass
    return _distinct_hosts(agents, ssh)


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
    count = host_count()  # distinct physical hosts (see _distinct_hosts)
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
