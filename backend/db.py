import contextlib
import hashlib
import ipaddress
import json
import os
import socket
import sqlite3
import time
from pathlib import Path


def _token_at_rest(token):
    """Key session/bearer tokens by their SHA-256 at rest so the DB never holds a
    directly-replayable credential. The raw token is returned to the client once
    (admin: Fernet-encrypted in the cookie; portal: the session cookie) and only its
    hash is stored/looked-up here. A leaked DB snapshot (backup, file disclosure, a SQL
    read primitive) then yields no usable live sessions. Upgrade note: tokens minted
    before this change no longer resolve, so existing sessions must re-login once."""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()

# =========================================================
# DATABASE LOCATION
# =========================================================
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "sysible.db"

# How many activity-log rows to retain (0 = never trim; let a SIEM own
# retention). Default ~months of history rather than the old 5000-row cap that
# silently dropped recent activity on a busy fleet.
try:
    _ACTIVITY_LOG_MAX_ROWS = int(os.getenv("SYSIBLE_ACTIVITY_LOG_MAX_ROWS", "500000"))
except ValueError:
    _ACTIVITY_LOG_MAX_ROWS = 500000


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
    # The DB holds administrator password hashes, agent bearer secrets, and live
    # admin tokens. SQLite creates the file under the process umask (0644 under
    # systemd's default), leaving it world-readable so any local account could
    # copy it and impersonate agents/admins. Force owner-only on the DB and its
    # WAL/SHM sidecars. Once per process (not on the per-connection hot path that
    # heartbeats hammer): the mode persists on disk, so a single pass suffices.
    global _db_perms_done
    if not _db_perms_done:
        _restrict_db_permissions()
        _db_perms_done = True
    return conn


_db_perms_done = False


def _restrict_db_permissions():
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        try:
            if p.exists():
                os.chmod(p, 0o600)
        except OSError:
            pass


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

    # Migration: agent secret revocation. 0 = active; 1 = revoked, so every
    # authenticated agent request (heartbeat/poll/result) is rejected until an
    # admin re-enrolls the host (which mints a fresh secret and clears this).
    # The hard "lock this host out" control — see revoke_agent / verify_agent.
    try:
        cur.execute("ALTER TABLE agents ADD COLUMN revoked INTEGER DEFAULT 0")
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

    # A reissue token is an admin-authorized credential to RE-BIND an already-enrolled
    # host_id (bound at generation). Ordinary enroll tokens can only ever create a NEW
    # host; re-binding an existing record additionally requires proof of possession
    # (the current agent_secret) or one of these reissue tokens. See the enroll handler.
    try:
        cur.execute("ALTER TABLE enroll_tokens ADD COLUMN reissue INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    # -----------------------------------------------------
    # Enrollment control (single row, id=1) — a kill-switch for /agents/enroll.
    # When paused, the controller refuses all new enrollments. This is the
    # operator's emergency brake on a RUNAWAY: a broken agent (or a re-provision
    # loop) that keeps enrolling fresh host_ids faster than they can be deleted.
    # Pause, clear the flood (it stays gone because nothing new is accepted), fix
    # the source host, then resume.
    # -----------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS enrollment_control (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        paused INTEGER DEFAULT 0,
        updated REAL,
        actor TEXT
    )
    """)

    # -----------------------------------------------------
    # Enrollment IP allowlist (source-CIDR gate for /agents/enroll)
    # A NON-EMPTY list restricts which source networks may enroll a new
    # host — the token is still required on top, this just narrows WHERE
    # a valid token may be presented from (a leaked bundle can't enroll
    # from an off-subnet source). EMPTY == allow all (backward compatible),
    # and loopback is always allowed (self-enroll / the console BFF).
    # Managed from Settings → Enrollment Access. Steady-state agent traffic
    # (heartbeat/tasks/results) is deliberately NOT gated by this — only the
    # open, token-gated enroll path is.
    # -----------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS enroll_allowlist (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cidr TEXT UNIQUE,
        note TEXT,
        created REAL,
        actor TEXT
    )
    """)

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
    # Gates the web console itself - separate from portal_credentials
    # above (that's for a remote host operator in a browser) and from
    # the admin API key in backend/auth.py (that's the console *process*
    # proving it's a trusted installation, not a human typing a
    # password).
    #
    # must_change_password forces the forced-password-change flow
    # on next login - set for the auto-seeded default
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

    # Indexes for the agent task-queue hot paths. fetch_pending_tasks (every agent
    # poll) filters host_id+status; reclaim_stale_tasks scans status+dispatched; the
    # prune and per-host result lookups filter status/host_id. Without these the most
    # frequent query in the system is a full-table scan that grows with every command
    # ever queued (the tasks tables, unlike metric_samples, are age-pruned).
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_tasks_host_status "
        "ON agent_tasks(host_id, status)")
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_tasks_status_dispatched "
        "ON agent_tasks(status, dispatched)")
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_results_host "
        "ON agent_results(host_id)")

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
    # Login throttle (durable brute-force lockout). Persisted so a process
    # restart / crash-loop does NOT wipe accumulated failures or an active
    # lockout (the previous in-memory dict reset on every restart). `key` is
    # the throttle bucket (per-username for admin login); `fails` is a JSON
    # array of recent failure timestamps; `until` is the lockout expiry.
    # -----------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS login_throttle (
        key TEXT PRIMARY KEY,
        fails TEXT,
        until REAL
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
  with contextlib.closing(_connect()) as conn:  # close even if the write raises
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
        ip=excluded.ip,
        -- A genuine re-enroll (valid single-use token, admin-authorized) clears
        -- a prior revocation: the host is being deliberately let back in.
        revoked=0
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


def update_agent_heartbeat(host_id, ip=None, hostname=None, agent_version=None):
  # closing(): guarantee the connection is released even if the UPDATE raises
  # (e.g. OperationalError "database is locked"). A leaked connection holds its
  # WAL read/write reservation until GC, which blocks checkpoint truncation and
  # the single writer — compounding the very lock contention on the once-per-
  # heartbeat hot path. Applied to the highest-frequency DB calls.
  with contextlib.closing(_connect()) as conn:
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


def list_agents():
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
    SELECT host_id, hostname, platform, kernel, status, last_seen, environment, ip,
           requires_sudo_password, agent_version, revoked
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
    # closing(): four writes under the single WAL writer during the busy
    # enroll/disenroll path — release the connection even if one raises "database
    # is locked", so a leaked writer can't stall every later write (see the
    # set_agent_environment/heartbeat fixes).
    with contextlib.closing(_connect()) as conn:
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
    # closing(): release the connection even if the write raises, so a leaked
    # WAL reservation can't compound lock contention on the heartbeat path.
    with contextlib.closing(_connect()) as conn:
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


def upsert_host_snapshot(host_id, ts, data_json):
    """Store the latest rich detail snapshot (JSON string) for a host,
    overwriting any previous one. One row per host - never grows with time."""
    with contextlib.closing(_connect()) as conn:  # close even if the write raises
        cur = conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO host_snapshot (host_id, ts, data) VALUES (?, ?, ?)",
            (host_id, float(ts), data_json),
        )
        conn.commit()


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


def revoke_agent(host_id):
    """Revoke a host's agent secret — the hard lock-out. Every authenticated
    request (verify_agent) then fails until the host is re-enrolled with a fresh
    single-use token. Returns True if a row was affected."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("UPDATE agents SET revoked=1 WHERE host_id=?", (host_id,))
    conn.commit()
    changed = cur.rowcount
    conn.close()
    return changed > 0


def unrevoke_agent(host_id):
    """Undo a revocation, KEEPING the existing agent secret so a still-installed
    agent resumes talking to the controller immediately (no re-enroll needed). The
    deliberate inverse of revoke_agent, for a superuser who revoked a host in error
    or has cleared whatever prompted the lock-out. Returns True if a row changed."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("UPDATE agents SET revoked=0 WHERE host_id=?", (host_id,))
    conn.commit()
    changed = cur.rowcount
    conn.close()
    return changed > 0


def is_agent_revoked(host_id):
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT revoked FROM agents WHERE host_id=?", (host_id,))
    row = cur.fetchone()
    conn.close()
    return bool(row and row[0])


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
    # closing(): release the connection (and its single WAL writer reservation)
    # even if a write raises "database is locked". A bare _connect()/close()
    # here leaks the writer on any exception, which then blocks EVERY later write
    # on the single-process controller — surfacing to the operator as the console
    # giving up at its 15s read timeout when they Set Environment. See the same
    # fix on update_agent_heartbeat.
    with contextlib.closing(_connect()) as conn:
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

# Grace window during which the SAME host may re-present its already-bound token
# (e.g. a disenroll immediately followed by re-running the same bundle) and land
# back on its original inventory row instead of spawning a duplicate. Shortened
# from 7 days to 24h by default and made tunable: a bound token scraped from a
# log/bundle is a bearer credential, so the shorter this window, the less time a
# captured token stays replayable. Re-binding still requires authorization (a
# reissue token or the current agent_secret) — see the enroll handler.
try:
    ENROLL_TOKEN_REUSE_WINDOW = int(
        os.getenv("SYSIBLE_ENROLL_TOKEN_REUSE_HOURS", "24")) * 60 * 60
except ValueError:
    ENROLL_TOKEN_REUSE_WINDOW = 24 * 60 * 60
try:
    ENROLL_TOKEN_VALID_DAYS = int(os.getenv("SYSIBLE_ENROLL_TOKEN_VALID_DAYS", "30"))
except ValueError:
    ENROLL_TOKEN_VALID_DAYS = 30


def get_enrollment_paused():
    """True if new agent enrollment is currently paused (the runaway kill-switch)."""
    conn = _connect()
    cur = conn.cursor()
    try:
        cur.execute("SELECT paused FROM enrollment_control WHERE id=1")
        row = cur.fetchone()
    except sqlite3.Error:
        row = None
    conn.close()
    return bool(row and row[0])


def get_enrollment_control():
    """Full pause state for status display: {paused, updated, actor}."""
    conn = _connect()
    cur = conn.cursor()
    try:
        cur.execute("SELECT paused, updated, actor FROM enrollment_control WHERE id=1")
        row = cur.fetchone()
    except sqlite3.Error:
        row = None
    conn.close()
    if not row:
        return {"paused": False, "updated": None, "actor": None}
    return {"paused": bool(row[0]), "updated": row[1], "actor": row[2]}


def set_enrollment_paused(paused, actor=None):
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO enrollment_control (id, paused, updated, actor) VALUES (1, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET paused=excluded.paused, updated=excluded.updated, "
        "actor=excluded.actor",
        (1 if paused else 0, time.time(), actor))
    conn.commit()
    conn.close()
    return bool(paused)


def _normalize_cidr(value):
    """Validate and canonicalize a CIDR or bare IP (v4 or v6). A bare address
    becomes a host route (/32 or /128). Raises ValueError on anything unparseable
    so the caller can return a 400 rather than storing junk that never matches."""
    v = (value or "").strip()
    if not v:
        raise ValueError("empty CIDR")
    return str(ipaddress.ip_network(v, strict=False))


def list_enroll_allowlist():
    """All enrollment-allowlist entries: [{id, cidr, note, created, actor}]."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, cidr, note, created, actor FROM enroll_allowlist ORDER BY id")
        rows = [dict(r) for r in cur.fetchall()]
    except sqlite3.Error:
        rows = []
    conn.close()
    return rows


def add_enroll_allowlist_cidr(cidr, note=None, actor=None):
    """Add (or update the note on) an allowed source CIDR. Returns the normalized
    CIDR string. Raises ValueError for an invalid CIDR/IP."""
    norm = _normalize_cidr(cidr)
    note = (note or "").strip() or None
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO enroll_allowlist (cidr, note, created, actor) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(cidr) DO UPDATE SET note=excluded.note",
        (norm, note, time.time(), actor))
    conn.commit()
    conn.close()
    return norm


def remove_enroll_allowlist_cidr(entry_id, actor=None):
    """Delete an allowlist entry by row id. Returns True if a row was removed."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM enroll_allowlist WHERE id=?", (entry_id,))
    conn.commit()
    changed = cur.rowcount
    conn.close()
    return changed > 0


def enroll_ip_allowed(ip):
    """Whether `ip` is permitted to enroll a new host. EMPTY allowlist => allow all
    (backward compatible). Loopback is always allowed (self-enroll / the BFF). A
    NON-empty allowlist with an unparseable source IP fails CLOSED (deny)."""
    rows = list_enroll_allowlist()
    if not rows:
        return True
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return False
    if addr.is_loopback:
        return True
    for r in rows:
        try:
            if addr in ipaddress.ip_network(r["cidr"], strict=False):
                return True
        except ValueError:
            continue
    return False


def create_enroll_token(token):
    conn = _connect()
    cur = conn.cursor()

    created = time.time()

    # Validity ceiling for an UNUSED bundle token (once claimed, the bound-host
    # reuse window in ENROLL_TOKEN_REUSE_WINDOW governs). Shortened from a year
    # to 30 days by default: a leaked-but-unused agent bundle shouldn't be able
    # to enroll a rogue host for that long. Tune with SYSIBLE_ENROLL_TOKEN_VALID_DAYS.
    expires = created + (ENROLL_TOKEN_VALID_DAYS * 24 * 60 * 60)

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
        _token_at_rest(token),
        created,
        expires
    ))

    conn.commit()
    conn.close()


def create_reissue_token(token, host_id):
    """Mint an admin-authorized REISSUE token, pre-bound to an existing host_id.

    Ordinary enroll tokens can only create a brand-new host; re-binding an
    already-enrolled host_id (e.g. after a reinstall that wiped the agent's
    saved secret) requires either the current agent_secret or one of these
    tokens. Stored hashed and single-use like any enroll token, but marked
    reissue=1 and bound to host_id at generation so the enroll handler can
    authorize the re-bind of exactly that host and no other."""
    conn = _connect()
    cur = conn.cursor()
    created = time.time()
    expires = created + (ENROLL_TOKEN_VALID_DAYS * 24 * 60 * 60)
    cur.execute("""
    INSERT INTO enroll_tokens (token, created, expires, used, bound_host_id, reissue)
    VALUES (?, ?, ?, 0, ?, 1)
    """, (_token_at_rest(token), created, expires, host_id))
    conn.commit()
    conn.close()


def enroll_token_authorizes_rebind(token, host_id):
    """True if `token` is a valid, unexpired admin REISSUE token bound to `host_id`
    — i.e. an administrator explicitly authorized re-binding this specific existing
    host. Used, unknown, expired, non-reissue, or differently-bound tokens return
    False. This is the authorization gate that lets a reinstalled host reclaim its
    inventory row without letting a bearer-token holder hijack an arbitrary host."""
    if not token or not host_id:
        return False
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT expires, used, bound_host_id, reissue FROM enroll_tokens WHERE token=?",
        (_token_at_rest(token),))
    row = cur.fetchone()
    conn.close()
    if row is None:
        return False
    expires, used, bound_host_id, reissue = row
    if not reissue or used:
        return False
    if time.time() > (expires or 0):
        return False
    return bound_host_id == host_id


def validate_enroll_token(token):
    conn = _connect()
    cur = conn.cursor()

    cur.execute("""
    SELECT expires, used, bound_host_id, last_used
    FROM enroll_tokens
    WHERE token=?
    """,
    (_token_at_rest(token),))

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

    cur.execute("SELECT bound_host_id FROM enroll_tokens WHERE token=?", (_token_at_rest(token),))
    row = cur.fetchone()

    conn.close()

    if row and row[0]:
        return row[0]

    return requested_host_id


def invalidate_enroll_tokens_for_host(host_id):
    """Delete every enrollment token bound to this host_id so it can't be reused to
    re-enroll (within the reuse window). Called on a FORCE removal, which is the
    "make this host gone for good" action: without this, a still-running zombie
    agent whose token is baked into sysible_agent.env just re-enrolls onto the same
    host_id and the record you deleted reappears. Returns the number removed."""
    if not host_id:
        return 0
    conn = _connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM enroll_tokens WHERE bound_host_id=?", (host_id,))
    n = cur.rowcount
    conn.commit()
    conn.close()
    return n


def consume_enroll_token(token, host_id):
    """Atomically claim an enroll token for host_id. Returns True exactly once for a
    valid, still-claimable token; False if it was already consumed by a concurrent
    request (a different host_id). Single-use is enforced by the DB with a conditional
    UPDATE — NOT merely by a process-local lock — so two replicas racing the same
    token can't both enroll (mirrors consume_relay_token)."""
    # closing(): release the writer even if the UPDATE raises, so a locked-DB
    # error on the enroll path can't leak the single WAL writer.
    with contextlib.closing(_connect()) as conn:
        cur = conn.cursor()

        # used=0 → a fresh first claim; bound_host_id=host_id → the same host
        # re-presenting its own token within the reuse window (idempotent re-bind).
        # Any other case (used=1 bound to a DIFFERENT host) claims 0 rows → False.
        cur.execute("""
        UPDATE enroll_tokens
        SET used=1, bound_host_id=?, last_used=?
        WHERE token=? AND (used=0 OR bound_host_id=?)
        """,
        (host_id, time.time(), _token_at_rest(token), host_id))

        claimed = cur.rowcount == 1
        conn.commit()
        return claimed


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
        # First read - seed a sane IP-based default rather than leaving the admin
        # staring at blank fields. IP-only by design: a bundle must never bake in a
        # hostname (that assumes DNS is set up on every managed host). Seed the
        # first detected NIC IP in "ip" mode, or "all" if none is detectable yet.
        # Left unconfigured (configured=0) since the admin hasn't saved anything.
        hostname = ""
        ip = ""
        try:
            from backend.agent_bundle import detect_local_ips
            _detected = detect_local_ips()
        except Exception:
            _detected = []
        if _detected:
            ip = _detected[0]
            address_mode = "ip"
        else:
            address_mode = "all"
        port = 9000
        configured = 0

        cur.execute(
            "INSERT INTO controller_config (id, hostname, ip, address_mode, port, configured) VALUES (1, ?, ?, ?, ?, ?)",
            (hostname, ip, address_mode, port, configured)
        )
        conn.commit()
    else:
        hostname, ip, address_mode, port, configured = row
        # Legacy "hostname" mode is migrated to IP on read — never surface a
        # hostname as the baked-in bundle address.
        address_mode = address_mode or ("ip" if ip else "all")

    conn.close()

    # The single value agent bundles actually get baked in with -
    # whichever of hostname/ip address_mode points at. "all" mode has
    # no single stored address - the real list is computed live from
    # this controller's current NICs (see backend/agent_bundle.py's
    # resolve_controller_addresses), so there's nothing meaningful to
    # put here.
    # Never surface a hostname as the bundle address — IP-only by design. A legacy
    # "hostname" config falls back to the stored IP (blank until re-saved).
    if address_mode == "all":
        address = ""
    else:
        address = ip

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
    """, (_token_at_rest(token), time.time(), expires, ip))

    conn.commit()
    conn.close()


def get_portal_session(token):
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT * FROM portal_sessions WHERE token=?", (_token_at_rest(token),))
    row = cur.fetchone()
    conn.close()

    return dict(row) if row else None


def delete_portal_session_by_token(token):
    conn = _connect()
    cur = conn.cursor()

    cur.execute("DELETE FROM portal_sessions WHERE token=?", (_token_at_rest(token),))
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
# ADMINISTRATORS (multiple rows - gates the web console itself)
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
        (_token_at_rest(token), username, role, expiry),
    )
    conn.commit()
    conn.close()


def resolve_admin_token(token):
    """Return {'username','role'} for a valid, unexpired token, else None.
    Invalid/expired/stale tokens are deleted as a side effect.

    Beyond the 12h expiry, the token is cross-checked against the LIVE account:
    it's rejected (and deleted) if the administrator no longer exists or no
    longer holds the role the token was minted with. Without this a removed
    admin keeps full API access, and a demoted superuser keeps superuser
    powers, until their token happens to expire — a revocation control silently
    deferred up to 12h. (Password resets can't be caught this way since the
    username/role are unchanged, so those call delete_admin_tokens_for_user.)"""
    if not token:
        return None
    tk = _token_at_rest(token)
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT username, role, expiry FROM admin_tokens WHERE token=?", (tk,))
    row = cur.fetchone()
    result = None
    if row:
        stale = (row["expiry"] or 0) < time.time()
        if not stale:
            cur.execute("SELECT role FROM administrators WHERE username=?", (row["username"],))
            acct = cur.fetchone()
            # Account gone, or role changed since the token was minted → stale.
            if acct is None or (acct["role"] or "superuser") != (row["role"] or "superuser"):
                stale = True
        if stale:
            cur.execute("DELETE FROM admin_tokens WHERE token=?", (tk,))
            conn.commit()
        else:
            result = {"username": row["username"], "role": row["role"]}
    conn.close()
    return result


def delete_admin_token(token):
    if not token:
        return
    conn = _connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM admin_tokens WHERE token=?", (_token_at_rest(token),))
    conn.commit()
    conn.close()


def delete_admin_tokens_for_user(username):
    """Revoke ALL live login tokens for one administrator — used when their
    account is removed, their role changes, or their password is reset, so an
    existing session can't outlive the change (up to the 12h token TTL)."""
    if not username:
        return
    conn = _connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM admin_tokens WHERE username=?", (username,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Durable login throttle — persisted brute-force lockout that survives a
# controller restart / crash-loop (the previous in-memory dict did not).
# ---------------------------------------------------------------------------
def login_throttle_locked_for(key):
    """Seconds remaining on `key`'s lockout, or 0 if not locked."""
    if not key:
        return 0
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT until FROM login_throttle WHERE key=?", (key,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return 0
    remaining = (row[0] or 0) - time.time()
    return int(remaining) if remaining > 0 else 0


def login_throttle_record_failure(key, window_s, max_failures, lockout_s):
    """Record one failed attempt for `key`; if it reaches `max_failures` within
    `window_s`, set a `lockout_s` lockout. Returns the lockout seconds now in
    effect (0 if not yet locked). Atomic under the connection."""
    if not key:
        return 0
    now = time.time()
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT fails, until FROM login_throttle WHERE key=?", (key,))
    row = cur.fetchone()
    try:
        fails = json.loads(row[0]) if row and row[0] else []
    except (ValueError, TypeError):
        fails = []
    until = (row[1] if row else 0) or 0
    fails = [t for t in fails if isinstance(t, (int, float)) and now - t < window_s]
    fails.append(now)
    locked = 0
    if len(fails) >= max_failures:
        until = now + lockout_s
        fails = []
        locked = lockout_s
    elif until > now:
        locked = int(until - now)
    cur.execute(
        "INSERT INTO login_throttle (key, fails, until) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET fails=excluded.fails, until=excluded.until",
        (key, json.dumps(fails), until))
    conn.commit()
    conn.close()
    return locked


def login_throttle_clear(key):
    """Clear a key's throttle state — called on a successful login."""
    if not key:
        return
    conn = _connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM login_throttle WHERE key=?", (key,))
    conn.commit()
    conn.close()


# Redact secret-bearing arguments before a command string is persisted to the
# audit log. The agent deliberately refuses to log command text because it can
# carry passwords/tokens/keys passed as args; the controller stores it for
# accountability, so scrub the well-known secret-carrying forms (value replaced
# with ***) rather than keeping them in cleartext at rest. Conservative on
# purpose — it targets known flags/patterns and leaves the rest of the command
# readable so the audit trail stays useful.
import re as _re

_SECRET_PATTERNS = [
    # --password=xxx / --token xxx / -p xxx  (long or short opts, = or space)
    _re.compile(
        r'(?i)(--?(?:password|passwd|pass|token|secret|api[-_]?key|apikey|auth[-_]?token|'
        r'access[-_]?key|private[-_]?key|client[-_]?secret)[=\s]+)(\S+)'),
    # KEY=value env-style assignments for the same sensitive names
    _re.compile(
        r'(?i)\b((?:password|passwd|token|secret|api[-_]?key|apikey|access[-_]?key|'
        r'client[-_]?secret)\s*=\s*)(\S+)'),
    # Authorization: Bearer xxx  /  Authorization: Basic xxx
    _re.compile(r'(?i)(authorization:\s*(?:bearer|basic)\s+)(\S+)'),
]


def _redact_secrets(command):
    if not command:
        return command
    out = command
    for pat in _SECRET_PATTERNS:
        out = pat.sub(lambda m: m.group(1) + "***", out)
    return out


# --- Activity log (Live Activity & Logs feed) ---
def log_activity(username, host, description, command=""):
  with contextlib.closing(_connect()) as conn:  # close even if the write raises
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO activity_log (timestamp, username, host, description, command) "
        "VALUES (?, ?, ?, ?, ?)",
        (time.time(), username or "(unknown)", host or "", description or "",
         _redact_secrets(command or "")),
    )
    conn.commit()
    # Cap the table so it can't grow unbounded, but keep enough history to be
    # useful as an audit record. The old 5000-row cap silently discarded
    # hours-to-days of activity on a busy fleet (a compliance red flag).
    # SYSIBLE_ACTIVITY_LOG_MAX_ROWS (default 500000, ~months of history) tunes
    # it; set 0 to disable trimming entirely when an external SIEM/export owns
    # retention. Compliance note: this local log is NOT a system of record —
    # forward it to a SIEM for durable, tamper-evident retention.
    #
    # Trim by id window off the just-inserted rowid: an indexed range delete of
    # only the rows that fell out of the window (usually one), NOT a full
    # `NOT IN (SELECT ... LIMIT cap)` anti-join that would rescan up to `cap`
    # rows on every insert — at cap=500k that ran a 500k-row scan per log write,
    # holding the single WAL writer each time. ids are monotonic (INTEGER PRIMARY
    # KEY), so `id <= lastrowid - cap` keeps the most recent ~cap rows.
    if _ACTIVITY_LOG_MAX_ROWS > 0:
        cutoff = cur.lastrowid - _ACTIVITY_LOG_MAX_ROWS
        if cutoff > 0:
            cur.execute("DELETE FROM activity_log WHERE id <= ?", (cutoff,))
            conn.commit()


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
    # Kill any live sessions for the removed account in the same breath, so its
    # token stops resolving immediately (resolve_admin_token also cross-checks).
    cur.execute("DELETE FROM admin_tokens WHERE username=?", (username,))
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
    # closing() so a 'database is locked' between INSERT and close can't leak the
    # connection (and its WAL reservation) on this hot enqueue path.
    with contextlib.closing(_connect()) as conn:
        cur = conn.cursor()
        cur.execute("""
        INSERT INTO agent_tasks (host_id, command, kind, status, created, run_as)
        VALUES (?, ?, ?, 'pending', ?, ?)
        """,
        (host_id, command, kind, time.time(), run_as))
        task_id = cur.lastrowid
        conn.commit()
    return task_id


def fetch_pending_tasks(host_id):
    """Atomically claim this host's pending tasks and return them.

    A single `UPDATE ... WHERE status='pending' RETURNING` claims and reads in one
    statement, so two concurrent polls for the same host can't both hand out the
    same command: only the rows THIS statement flips to 'dispatched' are returned.
    (The previous SELECT-then-UPDATE could double-dispatch — both readers saw the
    same pending rows before either updated.) Rows are ordered by id client-side
    (autoincrement == queue order) since UPDATE...RETURNING has no ORDER BY.
    Wrapped in closing() so a 'database is locked' error can't leak the connection."""
    with contextlib.closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "UPDATE agent_tasks SET status='dispatched', dispatched=? "
            "WHERE host_id=? AND status='pending' "
            "RETURNING id, command, kind, run_as",
            (time.time(), host_id),
        )
        rows = [dict(row) for row in cur.fetchall()]
        conn.commit()
    rows.sort(key=lambda r: r["id"])
    return rows


def submit_task_result(task_id, host_id, result):
    """Record a result and mark the task done — but ONLY if it is currently
    'dispatched'. Returns True if it applied, False otherwise. Guarding on the
    status makes result submission idempotent: a duplicate/retried result for an
    already-'done' task is a no-op (no second agent_results row, no re-run of
    side effects like ssh_enable), and an agent cannot mark its own still-
    'pending' task done without it ever being delivered/run.
    Wrapped in closing() so a locked-DB error can't leak the connection."""
    with contextlib.closing(_connect()) as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE agent_tasks SET status='done' WHERE id=? AND status='dispatched'",
            (task_id,),
        )
        applied = cur.rowcount == 1
        if applied:
            cur.execute("""
            INSERT INTO agent_results (task_id, host_id, result, completed)
            VALUES (?, ?, ?, ?)
            """,
            (task_id, host_id, result, time.time()))
        conn.commit()
    return applied


def reclaim_stale_tasks(timeout_seconds):
    """Surface tasks stuck in 'dispatched' longer than `timeout_seconds`: move
    them to the terminal 'timed_out' state and record a synthetic result, so a
    lost delivery (the host was handed the command but its result never came
    back) reaches closure and shows up as timed-out instead of silently living
    forever and accumulating.

    It does NOT re-queue the command: the host may have actually run it and only
    the RESULT was lost, so silently re-delivering a privileged command could
    double-execute it (at-most-once by design). An operator re-queues manually if
    they want. Returns the number reclaimed. `dispatched` is stamped by
    fetch_pending_tasks when a task is handed out.

    Also opportunistically prunes terminal task/result rows older than the
    retention window (see _prune_terminal_tasks) — this loop is the natural place
    to keep agent_tasks/agent_results from growing unboundedly with every command
    ever run. Wrapped in closing() so a locked-DB error can't leak the connection."""
    import json as _json
    with contextlib.closing(_connect()) as conn:
        cur = conn.cursor()
        cutoff = time.time() - float(timeout_seconds)
        cur.execute(
            "SELECT id, host_id FROM agent_tasks "
            "WHERE status='dispatched' AND dispatched IS NOT NULL AND dispatched < ?",
            (cutoff,),
        )
        stale = cur.fetchall()
        msg = _json.dumps({
            "stdout": "",
            "stderr": ("[sysible] Task timed out: the host was handed this command but never "
                       "reported a result. It may or may not have run — re-queue it if needed."),
            "returncode": -1,
        })
        now = time.time()
        reclaimed = 0
        for tid, host_id in stale:
            # Guard on status again: if a real result won the race between the SELECT
            # and here, the row is already 'done' -> 0 rows -> don't add a synthetic one.
            cur.execute("UPDATE agent_tasks SET status='timed_out' WHERE id=? AND status='dispatched'", (tid,))
            if cur.rowcount == 1:
                cur.execute(
                    "INSERT INTO agent_results (task_id, host_id, result, completed) VALUES (?, ?, ?, ?)",
                    (tid, host_id, msg, now),
                )
                reclaimed += 1
        _prune_terminal_tasks(cur, now)
        conn.commit()
    return reclaimed


# Retention for finished task/result rows. Terminal tasks (done/timed_out) and
# their results are kept this long for the activity feed, then pruned so the
# agent_tasks/agent_results tables stay bounded. Tunable; 0 disables pruning.
try:
    TASK_RETENTION_DAYS = int(os.getenv("SYSIBLE_TASK_RETENTION_DAYS", "30"))
except ValueError:
    TASK_RETENTION_DAYS = 30


def _prune_terminal_tasks(cur, now):
    """Delete terminal (done/timed_out) tasks and their results older than the
    retention window. Takes an existing cursor so it runs inside the caller's
    transaction. No-op when retention is disabled (<= 0)."""
    if TASK_RETENTION_DAYS <= 0:
        return
    cutoff = now - TASK_RETENTION_DAYS * 24 * 60 * 60
    cur.execute(
        "DELETE FROM agent_results WHERE task_id IN ("
        "  SELECT id FROM agent_tasks "
        "  WHERE status IN ('done','timed_out') AND created IS NOT NULL AND created < ?)",
        (cutoff,),
    )
    cur.execute(
        "DELETE FROM agent_tasks "
        "WHERE status IN ('done','timed_out') AND created IS NOT NULL AND created < ?",
        (cutoff,),
    )


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


def get_task_result(task_id):
    """Current status of a task plus its stored result text once it's terminal.
    Returns {"status": <str>, "result": <str|None>}, or None if the task is
    unknown. Lets a synchronous caller queue a task and wait for the agent to run
    it (e.g. the terminal open path installing a per-session SSH key)."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT status FROM agent_tasks WHERE id=?", (task_id,))
    row = cur.fetchone()
    if row is None:
        conn.close()
        return None
    status = row["status"]
    result = None
    if status in ("done", "timed_out"):
        cur.execute(
            "SELECT result FROM agent_results WHERE task_id=? ORDER BY completed DESC LIMIT 1",
            (task_id,),
        )
        rr = cur.fetchone()
        result = rr["result"] if rr else None
    conn.close()
    return {"status": status, "result": result}


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
