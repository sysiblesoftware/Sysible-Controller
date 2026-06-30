import json
import os
import socket
import sqlite3
import time
from pathlib import Path

# =========================================================
# DATABASE LOCATION
# =========================================================
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "sysible.db"


def _connect():
    """Single choke point for every DB connection in this file (was
    `sqlite3.connect(DB_PATH)` repeated ~25 times with no timeout/journal
    settings at all).

    Default SQLite uses a rollback journal: any write holds an exclusive
    lock on the *whole* database file for its transaction, blocking
    every other reader and writer - even ones touching unrelated
    tables. That's a poor fit here, since heartbeats land every ~1.5s
    per enrolled agent (host_agent/agent.py's POLL_INTERVAL) racing
    against task queueing/results and whatever the GUI is doing, all
    against this one file. Under real concurrency that surfaces as
    "database is locked": on the agent side, a heartbeat that hits the
    backend mid-write gets back a 500, which heartbeat() in agent.py
    catches and logs as "[agent] heartbeat failed: ...". That's almost
    certainly what was being reported as "the heartbeat keeps failing" -
    and it would only get worse after speeding up the agent's poll
    interval (more frequent heartbeats = more contention), so fixing
    this here too matters.

    WAL mode (set per-connection below; a no-op once the DB file is
    already in WAL mode, which persists across connections/restarts)
    fixes the bulk of it: readers no longer block writers or vice versa,
    so only writer-vs-writer contention is still serialized - and each
    write here is a single short statement, so that window is brief.
    timeout=30 (and the equivalent busy_timeout pragma) is the remaining
    belt-and-suspenders: if a writer does still have to wait on another
    writer, wait up to 30s before raising "database is locked" instead
    of sqlite3's 5s default.
    """
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


# =========================================================
# DATABASE INIT
# =========================================================
def init_db():
    conn = _connect()
    cur = conn.cursor()

    # -----------------------------------------------------
    # Agents
    # -----------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS agents (
        host_id TEXT PRIMARY KEY,
        hostname TEXT,
        platform TEXT,
        kernel TEXT,
        status TEXT,
        last_seen REAL,
        agent_secret TEXT,
        ip TEXT,
        requires_sudo_password INTEGER DEFAULT 0
    )
    """)

    # Migration for databases created before agent_secret existed.
    try:
        cur.execute("ALTER TABLE agents ADD COLUMN agent_secret TEXT")
    except sqlite3.OperationalError:
        pass

    # Migration for databases created before environment tagging existed.
    # Deliberately NOT touched by create_or_update_agent's upsert (see
    # below) - this is an admin-assigned label, not something the agent
    # itself reports, so a re-enroll/heartbeat must never reset it.
    try:
        cur.execute("ALTER TABLE agents ADD COLUMN environment TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass

    # Migration for databases created before the agent's local IP was
    # reported - used to populate the Address column in Remote
    # Administration for agent-kind hosts (previously just showed the
    # opaque host_id). Reported by the agent itself on enroll/heartbeat
    # (see host_agent/agent.py's _local_ip()), since the controller has
    # no other reliable way to learn a NATed/multi-homed agent's LAN IP.
    try:
        cur.execute("ALTER TABLE agents ADD COLUMN ip TEXT")
    except sqlite3.OperationalError:
        pass

    # Migration: per-host sudo mode. 0 = NOPASSWD (agent uses `sudo -n`,
    # default); 1 = the host forbids passwordless sudo, so the GUI supplies
    # the operator's sudo password for dispatched commands and the agent
    # elevates with `sudo -S`. Admin-set, like environment - never reset by a
    # re-enroll/heartbeat.
    try:
        cur.execute("ALTER TABLE agents ADD COLUMN requires_sudo_password INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    # agent_version: a short hash of the agent's own agent.py, reported on every
    # heartbeat (newer agents). Lets the web console show which hosts are running
    # the current agent and drive the "Update agents" progress bar. Nullable;
    # older agents simply don't report it.
    try:
        cur.execute("ALTER TABLE agents ADD COLUMN agent_version TEXT")
    except sqlite3.OperationalError:
        pass

    # -----------------------------------------------------
    # Environments (dev/stage/prod, etc.)
    # An editable, admin-managed list rather than a fixed enum - used
    # both to group hosts in the GUI and to populate the "assign an
    # environment" dropdowns. Agent hosts (agents.environment) and SSH
    # hosts (hosts.json's per-host "environment" key) both just store
    # a plain string name; this table is the registry of known names,
    # not a foreign key, so deleting one here doesn't touch hosts
    # already tagged with it.
    # -----------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS environments (
        name TEXT PRIMARY KEY,
        created REAL,
        requires_sudo_password INTEGER DEFAULT 0
    )
    """)

    # Migration: per-environment sudo default that hosts inherit when
    # assigned to the environment (see set_agent_environment).
    try:
        cur.execute("ALTER TABLE environments ADD COLUMN requires_sudo_password INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    cur.execute("SELECT COUNT(*) FROM environments")
    if cur.fetchone()[0] == 0:
        now = time.time()
        cur.executemany(
            "INSERT INTO environments (name, created) VALUES (?, ?)",
            [("Dev", now), ("Stage", now), ("Prod", now)]
        )

    # -----------------------------------------------------
    # Metric samples (fleet performance time-series)
    #
    # Lightweight rolling history fed by the agent itself: each agent
    # samples a few cheap numbers (load, memory %, worst-disk %) and
    # piggybacks them on its heartbeat at most every
    # SYSIBLE_METRICS_INTERVAL seconds (NOT every heartbeat - see
    # host_agent/agent.py). The controller appends one row per sample
    # and prunes anything older than the retention window on write, so
    # the table stays bounded (~a couple thousand rows per host/day).
    # Read back by the web console's Performance view, grouped by
    # environment with per-host drill-down.
    #
    # Deliberately not a foreign key to agents: a disenroll deletes the
    # agent row but old samples just age out via the retention prune, so
    # a brief post-removal window can't error on an orphan reference.
    # -----------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS metric_samples (
        host_id TEXT NOT NULL,
        ts REAL NOT NULL,
        load1 REAL,
        cores INTEGER,
        mem INTEGER,
        disk INTEGER,
        PRIMARY KEY (host_id, ts)
    )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_metric_samples_ts ON metric_samples(ts)"
    )
    # Richer scalar time-series (added later): CPU%, load 5/15m, swap%, network
    # throughput (bytes/s), disk I/O (bytes/s), and process count. All nullable
    # so older agents (which omit them) and existing rows keep working.
    for _col, _type in (
        ("load5", "REAL"), ("load15", "REAL"), ("cpu", "REAL"), ("swap", "INTEGER"),
        ("net_rx", "REAL"), ("net_tx", "REAL"), ("io_r", "REAL"), ("io_w", "REAL"),
        ("procs", "INTEGER"),
    ):
        try:
            cur.execute(f"ALTER TABLE metric_samples ADD COLUMN {_col} {_type}")
        except sqlite3.OperationalError:
            pass

    # -----------------------------------------------------
    # Host snapshot (latest rich detail for the per-host drill-down)
    #
    # Unlike metric_samples (a rolling scalar time-series), this holds just the
    # LATEST detailed snapshot per host - per-core CPU, memory breakdown,
    # per-interface network, per-mount disk, and top processes - as a JSON blob
    # the agent attaches alongside its metrics. One row per host, overwritten
    # each interval, so it never grows with time. Powers the per-host metrics
    # drill-down without a separate on-demand probe.
    # -----------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS host_snapshot (
        host_id TEXT PRIMARY KEY,
        ts REAL NOT NULL,
        data TEXT
    )
    """)

    # -----------------------------------------------------
    # Enrollment Tokens
    #
    # bound_host_id/last_used (migrated in below) let an already-used
    # token be reused by the SAME host for a grace window after its
    # last use (see ENROLL_TOKEN_REUSE_WINDOW) - covers a host that
    # was disenrolled and is now re-running the same agent bundle:
    # without this, its local agent_state.json is gone, it mints a
    # fresh random host_id, finds its old token already burned, and
    # silently fails to ever reappear in the enrolled hosts list.
    # -----------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS enroll_tokens (
        token TEXT PRIMARY KEY,
        created REAL,
        expires REAL,
        used INTEGER DEFAULT 0
    )
    """)

    # Migration for databases created before bound_host_id/last_used existed.
    try:
        cur.execute("ALTER TABLE enroll_tokens ADD COLUMN bound_host_id TEXT")
    except sqlite3.OperationalError:
        pass

    try:
        cur.execute("ALTER TABLE enroll_tokens ADD COLUMN last_used REAL")
    except sqlite3.OperationalError:
        pass

    # -----------------------------------------------------
    # Controller Configuration (single row, id=1)
    # The address/port the controller is reachable at - used to bake
    # a working SYSIBLE_CONTROLLER value into agent bundles generated
    # for the Webserver Portal, so a downloaded agent doesn't need
    # SYSIBLE_CONTROLLER set by hand. Not used by anything else.
    #
    # hostname and ip are independent, both-optional fields rather
    # than one combined "hostname/IP" field - address_mode says which
    # one is actually used when building a bundle, so an admin can
    # keep both on record without it being ambiguous which wins.
    # -----------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS controller_config (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        hostname TEXT,
        ip TEXT,
        address_mode TEXT DEFAULT 'hostname',
        port INTEGER,
        configured INTEGER DEFAULT 0
    )
    """)

    # Migration for databases created before ip/address_mode/configured
    # existed. "configured" distinguishes a row an admin actually saved
    # (via set_controller_config) from the auto-seeded default row
    # get_controller_config() inserts on first read - without it, every
    # install silently looks "configured" via this machine's own
    # socket.gethostname(), which is rarely an address other hosts on
    # the network can actually reach.
    for ddl in (
        "ALTER TABLE controller_config ADD COLUMN ip TEXT",
        "ALTER TABLE controller_config ADD COLUMN address_mode TEXT DEFAULT 'hostname'",
        "ALTER TABLE controller_config ADD COLUMN configured INTEGER DEFAULT 0",
    ):
        try:
            cur.execute(ddl)
        except sqlite3.OperationalError:
            pass

    # -----------------------------------------------------
    # License Configuration (single row, id=1)
    # Just a license key an admin has entered, surfaced alongside the
    # installed VERSION (see version.py) in the Sysible Controller
    # Settings page's License & Version section. No licensing model is
    # enforced against this yet - it's stored so an admin has somewhere
    # to put a license key now, ahead of that being built out.
    # -----------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS license_config (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        license_key TEXT
    )
    """)

    # -----------------------------------------------------
    # Portal Configuration (single row, id=1)
    # Which port the Webserver Portal listens on - configurable from
    # the GUI rather than fixed at process start via env var.
    # portal_manager tracks the *running* process's actual bound port
    # separately (a change here only takes effect on the next Start).
    # -----------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS portal_config (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        port INTEGER
    )
    """)

    # -----------------------------------------------------
    # Portal Credentials (single row, id=1)
    # A simple username/password login for the Webserver Portal -
    # deliberately separate from the admin API key (backend/auth.py)
    # and the enrollment-token system (enroll_tokens above): this is
    # what a remote host *operator* types into a browser, not
    # something the admin GUI or an agent ever uses. Password is
    # never stored in plaintext - see backend/portal_auth.py.
    # -----------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS portal_credentials (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        username TEXT,
        password_hash TEXT,
        password_salt TEXT
    )
    """)

    # Migration for databases created before last_changed existed.
    try:
        cur.execute("ALTER TABLE portal_credentials ADD COLUMN last_changed REAL")
    except sqlite3.OperationalError:
        pass

    # -----------------------------------------------------
    # Portal Login History (login successes/failures against the
    # shared portal account, plus credential-change events) and Portal
    # Sessions (one row per active post-login cookie) - together these
    # are what give the admin GUI visibility/control over the portal
    # login that backend/portal_auth.py's old purely-in-memory session
    # dict couldn't: history survives a portal restart, and sessions
    # live here (not just in portal_app.py's process memory) so the
    # admin GUI - a *different* process, talking only to backend/app.py -
    # can actually see and revoke them.
    # -----------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS portal_login_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL,
        event TEXT,
        username TEXT,
        ip TEXT,
        detail TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS portal_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token TEXT UNIQUE,
        created REAL,
        expires REAL,
        ip TEXT
    )
    """)

    # -----------------------------------------------------
    # Admin Credentials (single row, id=1) - SUPERSEDED by the
    # multi-row `administrators` table below. Left here, still
    # created (but never written to again), purely so the one-time
    # migration just below has something to read on an existing
    # install that predates multi-admin support.
    # -----------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS admin_credentials (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        username TEXT,
        password_hash TEXT,
        password_salt TEXT
    )
    """)

    # -----------------------------------------------------
    # Administrators (multiple rows - replaces admin_credentials)
    # Gates the desktop GUI itself - separate from portal_credentials
    # above (that's for a remote host operator in a browser) and from
    # the admin API key in backend/auth.py (that's the GUI *process*
    # proving it's a trusted installation, not a human typing a
    # password).
    #
    # must_change_password forces the forced-password-change flow
    # (client/main.py) on next login - set for the auto-seeded default
    # admin/admin account, and for any admin a fellow admin re-adds
    # with a temporary password, but NOT for an account that already
    # picked its own password.
    # -----------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS administrators (
        username TEXT PRIMARY KEY,
        password_hash TEXT,
        password_salt TEXT,
        must_change_password INTEGER DEFAULT 0,
        created REAL,
        created_by TEXT,
        last_login REAL,
        role TEXT DEFAULT 'superuser',
        sudo_connect INTEGER DEFAULT 0
    )
    """)

    # Migration: add the role column to databases created before RBAC.
    # Default 'superuser' so an existing single admin keeps full access
    # rather than being silently downgraded and locked out of management.
    cur.execute("PRAGMA table_info(administrators)")
    _admin_cols = {c[1] for c in cur.fetchall()}
    if "role" not in _admin_cols:
        cur.execute("ALTER TABLE administrators ADD COLUMN role TEXT DEFAULT 'superuser'")
    # Migration: per-admin opt-in for the Sysible Connect terminal's "Send sudo
    # password" button. Default 0 (off) - it's an opt-in a superuser grants,
    # so existing admins start without it until explicitly enabled.
    if "sudo_connect" not in _admin_cols:
        cur.execute("ALTER TABLE administrators ADD COLUMN sudo_connect INTEGER DEFAULT 0")

    # One-time migration + default-seed, run only while `administrators`
    # is still empty so this never re-fires (e.g. after an admin is
    # later removed, leaving the table momentarily smaller).
    cur.execute("SELECT COUNT(*) FROM administrators")
    if cur.fetchone()[0] == 0:
        cur.execute("SELECT username, password_hash, password_salt FROM admin_credentials WHERE id=1")
        legacy = cur.fetchone()

        if legacy and legacy[0]:
            # Pre-existing custom credentials from before multi-admin
            # support - carry them over as-is. Not forced to change
            # again, since this isn't a default the operator never chose.
            cur.execute("""
            INSERT INTO administrators (username, password_hash, password_salt, must_change_password, created, created_by)
            VALUES (?, ?, ?, 0, ?, 'migration')
            """, (legacy[0], legacy[1], legacy[2], time.time()))
        # Otherwise leave the table EMPTY on a fresh install - there is no
        # built-in default account. The first launch detects the empty
        # table (GET /admin/setup-required) and makes the operator create
        # their own administrator with their own password before the GUI
        # is usable (POST /admin/setup), so there's never a known default
        # password or a redundant default-then-rename step.

    # -----------------------------------------------------
    # Admin Audit Log
    # Login successes/failures and administrator account changes only
    # (added/removed, password changed, forced-change completed) - NOT
    # a general infra-command audit trail, that's covered separately
    # by agent_tasks/agent_results.
    # -----------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS admin_audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL,
        event TEXT,
        username TEXT,
        detail TEXT
    )
    """)

    # -----------------------------------------------------
    # Environmental Policy (single row, id=1)
    # Controller-wide defaults for target-host Linux accounts:
    # password/lockout quality, sudo behavior, default umask. Used
    # both as the baseline the GUI itself enforces when generating or
    # validating a password (client/api.py check_password_strength /
    # generate_strong_password), and as the starting values on the
    # System Administration > Environmental Policies page, which can
    # also push them out live to target hosts. Stored as one JSON
    # blob since the whole shape is only ever read/written together.
    # -----------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS environmental_policy (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        policy_json TEXT
    )
    """)

    # -----------------------------------------------------
    # Administrator Password Policy (single row, id=1)
    # Separate from environmental_policy above - governs the Sysible
    # Controller's own admin (GUI login) accounts, not target hosts'
    # Linux accounts. Enforced in app.py's add_administrator_route /
    # change_admin_credentials / force_admin_password_change.
    # -----------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS admin_password_policy (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        policy_json TEXT
    )
    """)

    # -----------------------------------------------------
    # Agent Tasks (commands queued for an agent to run)
    # -----------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS agent_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        host_id TEXT,
        command TEXT,
        kind TEXT DEFAULT 'command',
        status TEXT DEFAULT 'pending',
        created REAL,
        dispatched REAL,
        run_as TEXT
    )
    """)

    # Migration for databases created before kind existed.
    try:
        cur.execute("ALTER TABLE agent_tasks ADD COLUMN kind TEXT DEFAULT 'command'")
    except sqlite3.OperationalError:
        pass

    # Migration: run_as carries the RBAC local-user a task runs as on the
    # host (None == run as the agent itself, i.e. root / internal tasks).
    try:
        cur.execute("ALTER TABLE agent_tasks ADD COLUMN run_as TEXT")
    except sqlite3.OperationalError:
        pass

    # -----------------------------------------------------
    # Agent Results (output reported back by agents)
    # -----------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS agent_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER,
        host_id TEXT,
        result TEXT,
        completed REAL
    )
    """)

    # -----------------------------------------------------
    # Admin login tokens (RBAC). Issued at /admin/login, bound to a
    # username + role, used to attribute API actions to a specific admin
    # so dispatch can tag tasks with an UNFORGEABLE initiating username
    # (a sysadmin can only hold a token for the identity they logged in
    # as). Short-lived; resolve_admin_token() drops expired ones.
    # -----------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS admin_tokens (
        token TEXT PRIMARY KEY,
        username TEXT,
        role TEXT,
        expiry REAL
    )
    """)

    # -----------------------------------------------------
    # Activity log: a human-readable, attributed feed of actions the
    # controller carried out - "<admin> <description> on <host>" - for the
    # Live Activity & Logs view. username is the UNFORGEABLE initiating
    # admin (from their token, set server-side at dispatch); description is
    # the tool's human label (or a command fallback); command is kept for
    # detail. Distinct from admin_audit_log (admin-account events only).
    # -----------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS activity_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL,
        username TEXT,
        host TEXT,
        description TEXT,
        command TEXT
    )
    """)

    conn.commit()
    conn.close()


# =========================================================
# AGENTS
# =========================================================
def create_or_update_agent(
    host_id,
    hostname,
    platform,
    kernel,
    status,
    last_seen,
    agent_secret=None,
    ip=None
):
    conn = _connect()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO agents (
        host_id,
        hostname,
        platform,
        kernel,
        status,
        last_seen,
        agent_secret,
        ip
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(host_id) DO UPDATE SET
        hostname=excluded.hostname,
        platform=excluded.platform,
        kernel=excluded.kernel,
        status=excluded.status,
        last_seen=excluded.last_seen,
        agent_secret=excluded.agent_secret,
        ip=excluded.ip
    """,
    (
        host_id,
        hostname,
        platform,
        kernel,
        status,
        last_seen,
        agent_secret,
        ip
    ))

    conn.commit()
    conn.close()


def update_agent_heartbeat(host_id, ip=None, hostname=None, agent_version=None):
    conn = _connect()
    cur = conn.cursor()

    # ip/hostname are optional on heartbeat (older agent builds won't send
    # them) - COALESCE keeps whatever was last reported instead of blanking
    # it when an old agent's heartbeat omits the field. A newer agent re-sends
    # both every heartbeat, so a changed hostname (Set Hostname) or a
    # DHCP-reassigned IP updates the inventory without a re-enroll.
    #
    # NOTE (rename caveats - REJOIN required for these): this only updates
    # the AGENT inventory row. Two things do NOT follow a hostname change and
    # must be re-done after a rename:
    #   * SSH enrollment - the SSH/merged host record is keyed by the old
    #     hostname (see backend/remote_routes.py), so re-enroll the host over
    #     SSH to pick up the new name.
    #   * AD/realm membership - the host's AD computer account is the old
    #     name; rejoin the domain (realm leave + Join again, ideally set the
    #     hostname BEFORE joining).
    # Automating these on rename is a future improvement.
    cur.execute("""
    UPDATE agents
    SET
        status=?,
        last_seen=?,
        ip=COALESCE(?, ip),
        hostname=COALESCE(?, hostname),
        agent_version=COALESCE(?, agent_version)
    WHERE host_id=?
    """,
    (
        "online",
        time.time(),
        ip,
        hostname,
        agent_version,
        host_id
    ))

    conn.commit()
    conn.close()


def list_agents():
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
    SELECT host_id, hostname, platform, kernel, status, last_seen, environment, ip,
           requires_sudo_password, agent_version
    FROM agents
    ORDER BY hostname
    """)

    rows = cur.fetchall()

    conn.close()

    return [dict(row) for row in rows]


def set_agent_sudo_password_required(host_id, required):
    conn = _connect()
    cur = conn.cursor()
    cur.execute("UPDATE agents SET requires_sudo_password=? WHERE host_id=?",
                (1 if required else 0, host_id))
    conn.commit()
    changed = cur.rowcount
    conn.close()
    return changed > 0


def delete_agent(host_id):
    conn = _connect()
    cur = conn.cursor()

    cur.execute(
        "DELETE FROM agents WHERE host_id=?",
        (host_id,)
    )
    cur.execute(
        "DELETE FROM agent_tasks WHERE host_id=?",
        (host_id,)
    )
    cur.execute(
        "DELETE FROM agent_results WHERE host_id=?",
        (host_id,)
    )
    cur.execute(
        "DELETE FROM metric_samples WHERE host_id=?",
        (host_id,)
    )

    conn.commit()
    conn.close()


# =========================================================
# METRIC SAMPLES (fleet performance time-series)
# =========================================================
# Keep ~26h of history so the web console can offer up to a 24h window
# with a little headroom; everything older is pruned on write.
METRIC_RETENTION_S = 26 * 3600


def insert_metric_sample(host_id, ts, load1, cores, mem, disk,
                         load5=None, load15=None, cpu=None, swap=None,
                         net_rx=None, net_tx=None, io_r=None, io_w=None, procs=None):
    """Append one performance sample for a host and prune anything past the
    retention window. Called from the heartbeat path (only when the agent
    actually attached metrics, i.e. at most once per SYSIBLE_METRICS_INTERVAL),
    so the write rate is low enough not to add meaningful heartbeat contention.
    The trailing args are the richer scalars added later (CPU%, load 5/15m,
    swap%, network/disk throughput, process count); older agents omit them."""
    conn = _connect()
    cur = conn.cursor()
    # INSERT OR REPLACE: the (host_id, ts) PK makes a duplicate timestamp
    # (e.g. a retried heartbeat) idempotent rather than an error.
    cur.execute(
        "INSERT OR REPLACE INTO metric_samples "
        "(host_id, ts, load1, cores, mem, disk, load5, load15, cpu, swap, "
        " net_rx, net_tx, io_r, io_w, procs) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (host_id, float(ts), load1, cores, mem, disk, load5, load15, cpu, swap,
         net_rx, net_tx, io_r, io_w, procs),
    )
    cur.execute(
        "DELETE FROM metric_samples WHERE ts < ?",
        (float(ts) - METRIC_RETENTION_S,),
    )
    conn.commit()
    conn.close()


def upsert_host_snapshot(host_id, ts, data_json):
    """Store the latest rich detail snapshot (JSON string) for a host,
    overwriting any previous one. One row per host - never grows with time."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO host_snapshot (host_id, ts, data) VALUES (?, ?, ?)",
        (host_id, float(ts), data_json),
    )
    conn.commit()
    conn.close()


def get_host_snapshot(host_id):
    """Return {ts, data} for a host's latest snapshot (data is the raw JSON
    string the agent sent), or None if there isn't one."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT ts, data FROM host_snapshot WHERE host_id = ?", (host_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {"ts": row["ts"], "data": row["data"]}


def get_metric_samples(window_s=3600):
    """Return per-host performance time-series within the last `window_s`
    seconds, joined to the agent inventory for hostname/environment. Shape:
    [{host_id, hostname, environment, samples: [{t, load1, cores, mem, disk}, ...]}]
    with samples in ascending time order. Hosts with no samples in the window
    are omitted."""
    window_s = max(60, min(int(window_s or 3600), METRIC_RETENTION_S))
    cutoff = time.time() - window_s
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """
        SELECT s.host_id, s.ts, s.load1, s.cores, s.mem, s.disk,
               s.load5, s.load15, s.cpu, s.swap, s.net_rx, s.net_tx,
               s.io_r, s.io_w, s.procs,
               a.hostname, a.environment
        FROM metric_samples s
        LEFT JOIN agents a ON a.host_id = s.host_id
        WHERE s.ts >= ?
        ORDER BY s.host_id, s.ts
        """,
        (cutoff,),
    )
    rows = cur.fetchall()
    conn.close()

    by_host = {}
    for r in rows:
        h = by_host.get(r["host_id"])
        if h is None:
            h = {
                "host_id": r["host_id"],
                "hostname": r["hostname"] or r["host_id"],
                "environment": r["environment"] or "Unassigned",
                "samples": [],
            }
            by_host[r["host_id"]] = h
        h["samples"].append({
            "t": r["ts"], "load1": r["load1"], "cores": r["cores"],
            "mem": r["mem"], "disk": r["disk"],
            "load5": r["load5"], "load15": r["load15"], "cpu": r["cpu"],
            "swap": r["swap"], "net_rx": r["net_rx"], "net_tx": r["net_tx"],
            "io_r": r["io_r"], "io_w": r["io_w"], "procs": r["procs"],
        })
    return list(by_host.values())


def get_agent_secret(host_id):
    conn = _connect()
    cur = conn.cursor()

    cur.execute(
        "SELECT agent_secret FROM agents WHERE host_id=?",
        (host_id,)
    )

    row = cur.fetchone()
    conn.close()

    return row[0] if row else None


def agent_exists(host_id):
    conn = _connect()
    cur = conn.cursor()

    cur.execute(
        "SELECT 1 FROM agents WHERE host_id=?",
        (host_id,)
    )

    row = cur.fetchone()
    conn.close()

    return row is not None


def set_agent_environment(host_id, environment):
    conn = _connect()
    cur = conn.cursor()

    cur.execute(
        "UPDATE agents SET environment=? WHERE host_id=?",
        (environment, host_id)
    )

    updated = cur.rowcount > 0

    # Inherit the environment's sudo default: assigning a host to an
    # environment applies that environment's requires_sudo_password so new
    # hosts dropped into a password-sudo environment pick it up automatically.
    # (Per-host can still be overridden afterward.)
    if environment:
        cur.execute("SELECT requires_sudo_password FROM environments WHERE name=?", (environment,))
        row = cur.fetchone()
        if row is not None:
            cur.execute("UPDATE agents SET requires_sudo_password=? WHERE host_id=?",
                        (1 if row[0] else 0, host_id))

    conn.commit()
    conn.close()

    return updated


# =========================================================
# ENVIRONMENTS (dev/stage/prod, etc. - editable registry)
# =========================================================
def list_environments():
    conn = _connect()
    cur = conn.cursor()

    cur.execute("SELECT name FROM environments ORDER BY created")

    names = [row[0] for row in cur.fetchall()]

    conn.close()

    return names


def list_environment_sudo_defaults():
    """{environment name: bool} - the per-environment 'requires password
    sudo' default that hosts inherit on assignment."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT name, requires_sudo_password FROM environments")
    out = {name: bool(flag) for name, flag in cur.fetchall()}
    conn.close()
    return out


def set_environment_sudo_default(name, required):
    conn = _connect()
    cur = conn.cursor()
    cur.execute("UPDATE environments SET requires_sudo_password=? WHERE name=?",
                (1 if required else 0, name))
    conn.commit()
    changed = cur.rowcount
    conn.close()
    return changed > 0


def create_environment(name):
    conn = _connect()
    cur = conn.cursor()

    cur.execute(
        "INSERT OR IGNORE INTO environments (name, created) VALUES (?, ?)",
        (name, time.time())
    )

    conn.commit()
    conn.close()


def delete_environment(name):
    conn = _connect()
    cur = conn.cursor()

    cur.execute("DELETE FROM environments WHERE name=?", (name,))

    conn.commit()
    conn.close()


# =========================================================
# ENROLLMENT TOKENS
#
# A token is single-use FOREVER as far as a *different* host is
# concerned, but the one host that actually claimed it (bound_host_id)
# may reuse it for ENROLL_TOKEN_REUSE_WINDOW after its last use - this
# is what lets a disenrolled-then-reenrolled host come back without a
# fresh token, even though its local agent_state.json (and therefore
# its host_id) was wiped by the disenroll teardown and a brand new
# random host_id gets minted on the next registration attempt. See
# resolve_enroll_token_host() below for how that new random id gets
# corrected back to the original one on a within-window reuse.
# =========================================================

# 7 days - matches the user-facing "...unless it's been 7 days or
# more" requirement for re-using a token tied to the same host.
ENROLL_TOKEN_REUSE_WINDOW = 7 * 24 * 60 * 60


def create_enroll_token(token):
    conn = _connect()
    cur = conn.cursor()

    created = time.time()

    # One year - hard ceiling, not affected by reuse.
    expires = created + (365 * 24 * 60 * 60)

    cur.execute("""
    INSERT INTO enroll_tokens (
        token,
        created,
        expires,
        used
    )
    VALUES (?, ?, ?, 0)
    """,
    (
        token,
        created,
        expires
    ))

    conn.commit()
    conn.close()


def validate_enroll_token(token):
    conn = _connect()
    cur = conn.cursor()

    cur.execute("""
    SELECT expires, used, bound_host_id, last_used
    FROM enroll_tokens
    WHERE token=?
    """,
    (token,))

    row = cur.fetchone()

    conn.close()

    if row is None:
        return False

    expires, used, bound_host_id, last_used = row

    # The 1-year ceiling is unconditional, even for a same-host reuse.
    if time.time() > expires:
        return False

    if not used:
        return True

    # Already claimed - only the originally-bound host gets a grace
    # window to reuse it (e.g. disenroll immediately followed by
    # re-running the same bundle). Any other claimant is rejected.
    if bound_host_id is None or last_used is None:
        return False

    return (time.time() - last_used) <= ENROLL_TOKEN_REUSE_WINDOW


def resolve_enroll_token_host(token, requested_host_id):
    """On a fresh (first-ever) claim, the host's own reported host_id
    is used as-is. On a within-window reuse, the agent has no memory
    of its old host_id (its agent_state.json was wiped), so it always
    reports a brand-new random one - this returns the ORIGINAL
    bound_host_id instead, so the reenrolling host lands on its old
    inventory entry rather than creating a duplicate."""
    conn = _connect()
    cur = conn.cursor()

    cur.execute("SELECT bound_host_id FROM enroll_tokens WHERE token=?", (token,))
    row = cur.fetchone()

    conn.close()

    if row and row[0]:
        return row[0]

    return requested_host_id


def consume_enroll_token(token, host_id):
    conn = _connect()
    cur = conn.cursor()

    cur.execute("""
    UPDATE enroll_tokens
    SET used=1, bound_host_id=?, last_used=?
    WHERE token=?
    """,
    (host_id, time.time(), token))

    conn.commit()
    conn.close()


# =========================================================
# CONTROLLER CONFIGURATION (single row - hostname/IP/port for agent
# bundle generation; see Controller Configuration in the GUI)
# =========================================================
def get_controller_config():
    conn = _connect()
    cur = conn.cursor()

    cur.execute("SELECT hostname, ip, address_mode, port, configured FROM controller_config WHERE id=1")
    row = cur.fetchone()

    if row is None:
        # First read - seed a sane default (this machine's own
        # hostname, the controller's standard port) rather than
        # leaving the admin staring at blank fields. Left unconfigured
        # (configured=0) since the admin hasn't actually saved
        # anything yet - this hostname may not even be reachable from
        # other hosts on the network.
        hostname = socket.gethostname()
        ip = ""
        address_mode = "hostname"
        port = 9000
        configured = 0

        cur.execute(
            "INSERT INTO controller_config (id, hostname, ip, address_mode, port, configured) VALUES (1, ?, ?, ?, ?, ?)",
            (hostname, ip, address_mode, port, configured)
        )
        conn.commit()
    else:
        hostname, ip, address_mode, port, configured = row
        address_mode = address_mode or "hostname"

    conn.close()

    # The single value agent bundles actually get baked in with -
    # whichever of hostname/ip address_mode points at. "all" mode has
    # no single stored address - the real list is computed live from
    # this controller's current NICs (see backend/agent_bundle.py's
    # resolve_controller_addresses), so there's nothing meaningful to
    # put here.
    if address_mode == "all":
        address = ""
    else:
        address = ip if address_mode == "ip" else hostname

    return {
        "hostname": hostname or "",
        "ip": ip or "",
        "address_mode": address_mode,
        "port": port,
        "address": address or "",
        # True only once an admin has actually saved this page (see
        # set_controller_config) - false for the auto-seeded default
        # above, even though "address" is non-empty in that case.
        "configured": bool(configured),
    }


def get_license_config():
    conn = _connect()
    cur = conn.cursor()

    cur.execute("SELECT license_key FROM license_config WHERE id=1")
    row = cur.fetchone()
    conn.close()

    return {"license_key": (row[0] if row and row[0] else "")}


def set_license_config(license_key):
    conn = _connect()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO license_config (id, license_key)
    VALUES (1, ?)
    ON CONFLICT(id) DO UPDATE SET license_key=excluded.license_key
    """, (license_key,))

    conn.commit()
    conn.close()

    return get_license_config()


def set_controller_config(hostname, ip, address_mode, port):
    conn = _connect()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO controller_config (id, hostname, ip, address_mode, port, configured)
    VALUES (1, ?, ?, ?, ?, 1)
    ON CONFLICT(id) DO UPDATE SET
        hostname=excluded.hostname,
        ip=excluded.ip,
        address_mode=excluded.address_mode,
        port=excluded.port,
        configured=1
    """,
    (hostname, ip, address_mode, port))

    conn.commit()
    conn.close()


# =========================================================
# PORTAL CONFIGURATION (single row - which port the Webserver
# Portal listens on; see Webserver Portal Configuration in the GUI)
# =========================================================
def get_portal_config():
    conn = _connect()
    cur = conn.cursor()

    cur.execute("SELECT port FROM portal_config WHERE id=1")
    row = cur.fetchone()

    if row is None:
        port = int(os.getenv("SYSIBLE_PORTAL_PORT", "8090"))
        cur.execute("INSERT INTO portal_config (id, port) VALUES (1, ?)", (port,))
        conn.commit()
    else:
        port = row[0]

    conn.close()

    return {"port": port}


def set_portal_port(port):
    conn = _connect()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO portal_config (id, port)
    VALUES (1, ?)
    ON CONFLICT(id) DO UPDATE SET port=excluded.port
    """, (port,))

    conn.commit()
    conn.close()


# =========================================================
# ENVIRONMENTAL POLICY (single row - target-host password/lockout/
# sudo/umask defaults; see Environmental Policies in the GUI)
# =========================================================
DEFAULT_ENVIRONMENTAL_POLICY = {
    "password": {"minlen": 12, "retry": 3, "dcredit": -1, "ucredit": -1, "lcredit": -1, "ocredit": -1},
    "lockout": {"deny": 5, "unlock_time": 900},
    "sudo": {"timestamp_timeout": 15, "require_password": True},
    "umask": "027",
}


def get_environmental_policy():
    conn = _connect()
    cur = conn.cursor()

    cur.execute("SELECT policy_json FROM environmental_policy WHERE id=1")
    row = cur.fetchone()

    if row is None or not row[0]:
        policy = dict(DEFAULT_ENVIRONMENTAL_POLICY)
        cur.execute("""
        INSERT INTO environmental_policy (id, policy_json) VALUES (1, ?)
        ON CONFLICT(id) DO UPDATE SET policy_json=excluded.policy_json
        """, (json.dumps(policy),))
        conn.commit()
    else:
        try:
            policy = json.loads(row[0])
        except (TypeError, ValueError):
            policy = dict(DEFAULT_ENVIRONMENTAL_POLICY)

    conn.close()
    return policy


def set_environmental_policy(policy: dict):
    conn = _connect()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO environmental_policy (id, policy_json) VALUES (1, ?)
    ON CONFLICT(id) DO UPDATE SET policy_json=excluded.policy_json
    """, (json.dumps(policy),))

    conn.commit()
    conn.close()


# =========================================================
# ADMINISTRATOR PASSWORD POLICY (single row - governs Sysible
# Controller admin/GUI-login accounts, not target-host Linux accounts)
# =========================================================
DEFAULT_ADMIN_PASSWORD_POLICY = {
    "minlen": 12, "dcredit": -1, "ucredit": -1, "lcredit": -1, "ocredit": -1,
}


def get_admin_password_policy():
    conn = _connect()
    cur = conn.cursor()

    cur.execute("SELECT policy_json FROM admin_password_policy WHERE id=1")
    row = cur.fetchone()

    if row is None or not row[0]:
        policy = dict(DEFAULT_ADMIN_PASSWORD_POLICY)
        cur.execute("""
        INSERT INTO admin_password_policy (id, policy_json) VALUES (1, ?)
        ON CONFLICT(id) DO UPDATE SET policy_json=excluded.policy_json
        """, (json.dumps(policy),))
        conn.commit()
    else:
        try:
            policy = json.loads(row[0])
        except (TypeError, ValueError):
            policy = dict(DEFAULT_ADMIN_PASSWORD_POLICY)

    conn.close()
    return policy


def set_admin_password_policy(policy: dict):
    conn = _connect()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO admin_password_policy (id, policy_json) VALUES (1, ?)
    ON CONFLICT(id) DO UPDATE SET policy_json=excluded.policy_json
    """, (json.dumps(policy),))

    conn.commit()
    conn.close()


# =========================================================
# PORTAL CREDENTIALS (single row - Webserver Portal login)
# =========================================================
def get_portal_credentials():
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute(
        "SELECT username, password_hash, password_salt, last_changed FROM portal_credentials WHERE id=1"
    )
    row = cur.fetchone()

    conn.close()

    return dict(row) if row else None


def set_portal_credentials(username, password_hash, password_salt):
    conn = _connect()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO portal_credentials (id, username, password_hash, password_salt, last_changed)
    VALUES (1, ?, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
        username=excluded.username,
        password_hash=excluded.password_hash,
        password_salt=excluded.password_salt,
        last_changed=excluded.last_changed
    """,
    (username, password_hash, password_salt, time.time()))

    conn.commit()
    conn.close()


def delete_portal_credentials():
    """Wipes the portal login outright - used by "Remove Login Access"
    in the GUI when an admin wants nobody able to log into the portal
    at all until new credentials are set, as opposed to just revoking
    today's sessions (delete_all_portal_sessions) while leaving the
    account itself intact."""
    conn = _connect()
    cur = conn.cursor()

    cur.execute("DELETE FROM portal_credentials WHERE id=1")

    conn.commit()
    conn.close()


# =========================================================
# PORTAL LOGIN HISTORY (login successes/failures + credential
# changes against the shared portal account)
# =========================================================
def log_portal_event(event, username, ip="", detail=""):
    conn = _connect()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO portal_login_history (timestamp, event, username, ip, detail)
    VALUES (?, ?, ?, ?, ?)
    """, (time.time(), event, username, ip, detail))

    conn.commit()
    conn.close()


def get_portal_login_history(limit=200):
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
    SELECT timestamp, event, username, ip, detail
    FROM portal_login_history
    ORDER BY timestamp DESC
    LIMIT ?
    """, (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    return rows


def get_last_portal_login():
    """Most recent successful login, or None if there's never been
    one - used by the Webserver Portal Configuration page to show
    "last successful login" without the GUI having to page through
    the whole history itself."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
    SELECT timestamp, username, ip
    FROM portal_login_history
    WHERE event = 'login_success'
    ORDER BY timestamp DESC
    LIMIT 1
    """)
    row = cur.fetchone()
    conn.close()

    return dict(row) if row else None


# =========================================================
# PORTAL SESSIONS (one row per active post-login cookie - lets the
# admin GUI, a separate process from the portal subprocess that
# actually issues these, list and revoke them)
# =========================================================
def create_portal_session(token, expires, ip=""):
    conn = _connect()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO portal_sessions (token, created, expires, ip)
    VALUES (?, ?, ?, ?)
    """, (token, time.time(), expires, ip))

    conn.commit()
    conn.close()


def get_portal_session(token):
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT * FROM portal_sessions WHERE token=?", (token,))
    row = cur.fetchone()
    conn.close()

    return dict(row) if row else None


def delete_portal_session_by_token(token):
    conn = _connect()
    cur = conn.cursor()

    cur.execute("DELETE FROM portal_sessions WHERE token=?", (token,))
    conn.commit()
    conn.close()


def delete_portal_session(session_id):
    """Same delete as above, but by the auto-increment id shown to the
    admin GUI - list_portal_sessions() below deliberately never sends
    the actual token (a bearer credential, equivalent to a password)
    over the admin API, so revocation has to key off this instead."""
    conn = _connect()
    cur = conn.cursor()

    cur.execute("DELETE FROM portal_sessions WHERE id=?", (session_id,))
    conn.commit()
    conn.close()


def purge_expired_portal_sessions():
    conn = _connect()
    cur = conn.cursor()

    cur.execute("DELETE FROM portal_sessions WHERE expires <= ?", (time.time(),))
    conn.commit()
    conn.close()


def delete_all_portal_sessions():
    """Used when credentials are reset - every existing session was
    issued under the old password, so they're invalidated rather than
    left to linger until their TTL naturally expires."""
    conn = _connect()
    cur = conn.cursor()

    cur.execute("DELETE FROM portal_sessions")
    conn.commit()
    conn.close()


def list_portal_sessions():
    purge_expired_portal_sessions()

    conn = _connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
    SELECT id, created, expires, ip
    FROM portal_sessions
    ORDER BY created DESC
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    return rows


# =========================================================
# ADMINISTRATORS (multiple rows - gates the desktop GUI itself)
# Replaces the old single-row admin_credentials table; see the
# migration in init_db() above.
# =========================================================
def list_administrators():
    """Account list for the Administrators UI - never includes the
    password hash/salt."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
    SELECT username, must_change_password, created, created_by, last_login, role, sudo_connect
    FROM administrators
    ORDER BY created ASC
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    return rows


def count_administrators():
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM administrators")
    count = cur.fetchone()[0]
    conn.close()
    return count


def count_administrators_by_role(role):
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM administrators WHERE role=?", (role,))
    count = cur.fetchone()[0]
    conn.close()
    return count


def set_administrator_role(username, role):
    conn = _connect()
    cur = conn.cursor()
    cur.execute("UPDATE administrators SET role=? WHERE username=?", (role, username))
    conn.commit()
    changed = cur.rowcount
    conn.close()
    return changed > 0


def set_administrator_sudo_connect(username, allowed):
    """Grant/revoke this admin's access to the Sysible Connect terminal's
    "Send sudo password" button. Superuser-gated at the route layer."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("UPDATE administrators SET sudo_connect=? WHERE username=?",
                (1 if allowed else 0, username))
    conn.commit()
    changed = cur.rowcount
    conn.close()
    return changed > 0


# --- Admin login tokens (RBAC identity) ---
def create_admin_token(token, username, role, expiry):
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO admin_tokens (token, username, role, expiry) VALUES (?, ?, ?, ?)",
        (token, username, role, expiry),
    )
    conn.commit()
    conn.close()


def resolve_admin_token(token):
    """Return {'username','role'} for a valid, unexpired token, else None.
    Expired tokens are deleted as a side effect."""
    if not token:
        return None
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT username, role, expiry FROM admin_tokens WHERE token=?", (token,))
    row = cur.fetchone()
    result = None
    if row:
        if (row["expiry"] or 0) >= time.time():
            result = {"username": row["username"], "role": row["role"]}
        else:
            cur.execute("DELETE FROM admin_tokens WHERE token=?", (token,))
            conn.commit()
    conn.close()
    return result


def delete_admin_token(token):
    if not token:
        return
    conn = _connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM admin_tokens WHERE token=?", (token,))
    conn.commit()
    conn.close()


# --- Activity log (Live Activity & Logs feed) ---
def log_activity(username, host, description, command=""):
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO activity_log (timestamp, username, host, description, command) "
        "VALUES (?, ?, ?, ?, ?)",
        (time.time(), username or "(unknown)", host or "", description or "", command or ""),
    )
    conn.commit()
    # Keep the table from growing forever - trim to the most recent 5000 rows.
    cur.execute(
        "DELETE FROM activity_log WHERE id NOT IN "
        "(SELECT id FROM activity_log ORDER BY id DESC LIMIT 5000)"
    )
    conn.commit()
    conn.close()


def get_agent_hostname(host_id):
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT hostname FROM agents WHERE host_id=?", (host_id,))
    row = cur.fetchone()
    conn.close()
    return (row[0] if row else None) or host_id


def get_activity_log(limit=200, since_id=0):
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT id, timestamp, username, host, description, command FROM activity_log "
        "WHERE id > ? ORDER BY id DESC LIMIT ?",
        (since_id, limit),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_administrator(username):
    """Full row including password_hash/password_salt - used for
    login verification. Returns None if no such administrator."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT * FROM administrators WHERE username=?", (username,))
    row = cur.fetchone()
    conn.close()

    return dict(row) if row else None


def add_administrator(username, password_hash, password_salt, must_change_password=1,
                      created_by=None, role="superuser"):
    """Returns True on success, False if the username is already
    taken. role is 'superuser', 'sysadmin', or 'auditor' (read-only)."""
    conn = _connect()
    cur = conn.cursor()

    try:
        cur.execute("""
        INSERT INTO administrators (username, password_hash, password_salt, must_change_password, created, created_by, role)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (username, password_hash, password_salt, int(must_change_password),
              time.time(), created_by, role))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def remove_administrator(username):
    conn = _connect()
    cur = conn.cursor()

    cur.execute("DELETE FROM administrators WHERE username=?", (username,))
    conn.commit()
    conn.close()


def update_administrator_username(old_username, new_username):
    """Returns True on success, False if new_username is already taken
    by a different administrator."""
    if old_username == new_username:
        return True

    conn = _connect()
    cur = conn.cursor()

    try:
        cur.execute(
            "UPDATE administrators SET username=? WHERE username=?",
            (new_username, old_username)
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def update_administrator_password(username, password_hash, password_salt, must_change_password=0):
    conn = _connect()
    cur = conn.cursor()

    cur.execute("""
    UPDATE administrators
    SET password_hash=?, password_salt=?, must_change_password=?
    WHERE username=?
    """, (password_hash, password_salt, int(must_change_password), username))
    conn.commit()
    conn.close()


def record_administrator_login(username):
    conn = _connect()
    cur = conn.cursor()

    cur.execute(
        "UPDATE administrators SET last_login=? WHERE username=?",
        (time.time(), username)
    )
    conn.commit()
    conn.close()


# =========================================================
# ADMIN AUDIT LOG (login + administrator account changes only)
# =========================================================
def log_admin_audit(event, username, detail=""):
    conn = _connect()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO admin_audit_log (timestamp, event, username, detail)
    VALUES (?, ?, ?, ?)
    """, (time.time(), event, username, detail))
    conn.commit()
    conn.close()


def get_admin_audit_log(limit=200):
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
    SELECT timestamp, event, username, detail
    FROM admin_audit_log
    ORDER BY timestamp DESC
    LIMIT ?
    """, (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    return rows


# =========================================================
# AGENT TASKS (command queue)
# =========================================================
def queue_task(host_id, command, kind="command", run_as=None):
    conn = _connect()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO agent_tasks (host_id, command, kind, status, created, run_as)
    VALUES (?, ?, ?, 'pending', ?, ?)
    """,
    (host_id, command, kind, time.time(), run_as))

    task_id = cur.lastrowid

    conn.commit()
    conn.close()

    return task_id


def fetch_pending_tasks(host_id):
    """Return pending tasks for a host and mark them dispatched so a
    slow-polling agent (or a retry) doesn't get the same command twice."""

    conn = _connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
    SELECT id, command, kind, run_as
    FROM agent_tasks
    WHERE host_id=? AND status='pending'
    ORDER BY created
    """,
    (host_id,))

    rows = [dict(row) for row in cur.fetchall()]

    if rows:
        ids = [row["id"] for row in rows]
        cur.execute(
            f"UPDATE agent_tasks SET status='dispatched', dispatched=? "
            f"WHERE id IN ({','.join('?' * len(ids))})",
            (time.time(), *ids)
        )

    conn.commit()
    conn.close()

    return rows


def submit_task_result(task_id, host_id, result):
    conn = _connect()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO agent_results (task_id, host_id, result, completed)
    VALUES (?, ?, ?, ?)
    """,
    (task_id, host_id, result, time.time()))

    cur.execute(
        "UPDATE agent_tasks SET status='done' WHERE id=?",
        (task_id,)
    )

    conn.commit()
    conn.close()


def get_task_kind(task_id):
    """The 'kind' a task was queued with (e.g. 'command', 'ssh_enable'),
    or None if the task no longer exists. Lets the result handler tell
    an ordinary queued command apart from the controller's own
    SSH-terminal auto-enroll task without scanning result text."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT kind FROM agent_tasks WHERE id=?", (task_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def get_task_host(task_id):
    """The host_id a task was queued for, or None if the task doesn't exist.
    Used to confirm a result-reporting agent actually owns the task it's
    reporting on (so one host can't post results against another's task)."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT host_id FROM agent_tasks WHERE id=?", (task_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def list_results(host_id, limit=50, kind=None, task_id=None):
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    query = """
    SELECT r.id, r.task_id, t.command, t.kind, r.result, r.completed
    FROM agent_results r
    LEFT JOIN agent_tasks t ON t.id = r.task_id
    WHERE r.host_id=?
    """
    params = [host_id]

    if kind is not None:
        query += " AND t.kind=?"
        params.append(kind)

    if task_id is not None:
        query += " AND r.task_id=?"
        params.append(task_id)

    query += " ORDER BY r.completed DESC LIMIT ?"
    params.append(limit)

    cur.execute(query, params)

    rows = [dict(row) for row in cur.fetchall()]

    conn.close()

    return rows


# =========================================================
# DATABASE CONNECTION
# =========================================================
def get_db():
    conn = _connect()
    conn.row_factory = sqlite3.Row
    return conn


# =========================================================
# INITIALIZE DATABASE ON IMPORT
# =========================================================
init_db()
