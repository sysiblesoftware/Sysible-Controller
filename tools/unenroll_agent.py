#!/usr/bin/env python3
"""Force-remove an enrolled agent directly from the controller database.

The last-resort cleanup for a "stuck" host the console won't drop (a revoked or
zombie record). Match by host_id, --ip, or --name — you don't need the internal
host_id. Safe to run while the controller is up (SQLite WAL); the deleted record's
secret is invalid immediately, so a still-running agent is locked out on its next
heartbeat.

    python3 tools/unenroll_agent.py --ip 192.168.100.23
    python3 tools/unenroll_agent.py --name rocky-030
    python3 tools/unenroll_agent.py <host_id>
    python3 tools/unenroll_agent.py --ip 10.0.0.5 --dry-run   # preview only

DB path resolution: --db, then SYSIBLE_DB_PATH, then common install locations.
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

# Ordered candidates: env override first, then EE and community install layouts,
# then the DB next to this checkout.
_CANDIDATES = [
    os.getenv("SYSIBLE_DB_PATH"),
    "/opt/sysible-controller-ee/backend/sysible.db",
    "/opt/sysible/backend/sysible.db",
    str(Path(__file__).resolve().parent.parent / "backend" / "sysible.db"),
]

_CLEANUP_TABLES = ["agent_tasks", "agent_results", "metric_samples"]


def _resolve_db(explicit):
    for c in ([explicit] if explicit else []) + _CANDIDATES:
        if c and os.path.exists(c):
            return c
    return None


def _safe(cur, sql, params=()):
    try:
        cur.execute(sql, params)
        return cur.rowcount
    except sqlite3.OperationalError as e:
        print(f"[skip] {e}")
        return 0


def main():
    ap = argparse.ArgumentParser(description="Force-remove a stuck agent from the controller DB.")
    ap.add_argument("host_id", nargs="?", help="agent host_id (uuid)")
    ap.add_argument("--ip", help="match by reported IP")
    ap.add_argument("--name", help="match by hostname")
    ap.add_argument("--db", help="path to sysible.db (else auto-detected)")
    ap.add_argument("--dry-run", action="store_true", help="show matches, delete nothing")
    args = ap.parse_args()

    if not (args.host_id or args.ip or args.name):
        ap.error("give a host_id, or --ip, or --name")

    db = _resolve_db(args.db)
    if not db:
        print("Could not find sysible.db. Pass --db /path/to/backend/sysible.db "
              "or set SYSIBLE_DB_PATH.", file=sys.stderr)
        return 2
    print(f"Using database: {db}")

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Find every matching host_id (a stuck box may have more than one record).
    clauses, params = [], []
    if args.host_id:
        clauses.append("host_id = ?"); params.append(args.host_id)
    if args.ip:
        clauses.append("ip = ?"); params.append(args.ip)
    if args.name:
        clauses.append("hostname = ?"); params.append(args.name)
    cur.execute(f"SELECT host_id, hostname, ip, status, COALESCE(revoked,0) AS revoked "
                f"FROM agents WHERE {' OR '.join(clauses)}", params)
    rows = cur.fetchall()

    if not rows:
        print("No matching agent records found.")
        conn.close()
        return 1

    print(f"Matched {len(rows)} record(s):")
    for r in rows:
        flag = " [revoked]" if r["revoked"] else ""
        print(f"  - {r['hostname']}  ip={r['ip']}  host_id={r['host_id']}{flag}")

    if args.dry_run:
        print("Dry run — nothing deleted.")
        conn.close()
        return 0

    total = 0
    for r in rows:
        hid = r["host_id"]
        total += _safe(cur, "DELETE FROM agents WHERE host_id = ?", (hid,))
        for t in _CLEANUP_TABLES:
            _safe(cur, f"DELETE FROM {t} WHERE host_id = ?", (hid,))
    conn.commit()
    conn.close()
    print(f"Removed {total} agent record(s). If a host's agent is still running, "
          "run disenroll_agent.sh on it (or stop its service) so it doesn't retry.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
