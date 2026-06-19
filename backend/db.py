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
        ip TEXT
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
        created REAL
    )
    """)

    cur.execute("SELECT COUNT(*) FROM environments")
    if cur.fetchone()[0] == 0:
        now = time.time()
        cur.executemany(
            "INSERT INTO environments (name, created) VALUES (?, ?)",
            [("Dev", now), ("Stage", now), ("Prod", now)]
        )

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
        last_login REAL
    )
    """)

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
            # again, since this isn't the untouched admin/admin default.
            cur.execute("""
            INSERT INTO administrators (username, password_hash, password_salt, must_change_password, created, created_by)
            VALUES (?, ?, ?, 0, ?, 'migration')
            """, (legacy[0], legacy[1], legacy[2], time.time()))
        else:
            # Fresh install - seed the default admin/admin, forced to
            # change on first login.
            #
            # Imported here rather than at module level to avoid a
            # backend.db <-> backend.portal_auth import cycle if
            # portal_auth ever needs db.py in the future.
            from backend import portal_auth

            salt, pw_hash = portal_auth.hash_password("admin")
            cur.execute("""
            INSERT INTO administrators (username, password_hash, password_salt, must_change_password, created, created_by)
            VALUES ('admin', ?, ?, 1, ?, 'system')
            """, (pw_hash, salt, time.time()))

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
        dispatched REAL
    )
    """)

    # Migration for databases created before kind existed.
    try:
        cur.execute("ALTER TABLE agent_tasks ADD COLUMN kind TEXT DEFAULT 'command'")
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


def update_agent_heartbeat(host_id, ip=None):
    conn = _connect()
    cur = conn.cursor()

    # ip is optional on heartbeat (older agent builds won't send it) -
    # COALESCE keeps whatever IP was last reported instead of blanking
    # it out when an old agent's heartbeat omits the field.
    cur.execute("""
    UPDATE agents
    SET
        status=?,
        last_seen=?,
        ip=COALESCE(?, ip)
    WHERE host_id=?
    """,
    (
        "online",
        time.time(),
        ip,
        host_id
    ))

    conn.commit()
    conn.close()


def list_agents():
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
    SELECT host_id, hostname, platform, kernel, status, last_seen, environment, ip
    FROM agents
    ORDER BY hostname
    """)

    rows = cur.fetchall()

    conn.close()

    return [dict(row) for row in rows]


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

    conn.commit()
    conn.close()


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
    SELECT username, must_change_password, created, created_by, last_login
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


def add_administrator(username, password_hash, password_salt, must_change_password=1, created_by=None):
    """Returns True on success, False if the username is already
    taken."""
    conn = _connect()
    cur = conn.cursor()

    try:
        cur.execute("""
        INSERT INTO administrators (username, password_hash, password_salt, must_change_password, created, created_by)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (username, password_hash, password_salt, int(must_change_password), time.time(), created_by))
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
def queue_task(host_id, command, kind="command"):
    conn = _connect()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO agent_tasks (host_id, command, kind, status, created)
    VALUES (?, ?, ?, 'pending', ?)
    """,
    (host_id, command, kind, time.time()))

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
    SELECT id, command, kind
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
