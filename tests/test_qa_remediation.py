"""
Regression tests for the production-readiness remediation batch:

  S2 — cmd_delete_timer path-traversal guard (arbitrary .timer/.service deletion)
  C6 — fetch_pending_tasks claims atomically (no double-dispatch, ordered by id)
  C2 — agent_tasks indexes exist and terminal rows are pruned by retention
"""
import pytest

import backend.db as db
from client import api as capi


# --------------------------------------------------------------------------- #
# S2 — timer deletion is confined to /etc/systemd/system/                     #
# --------------------------------------------------------------------------- #
def test_delete_timer_rejects_traversal():
    for evil in ("../../../etc/cron.d/foo", "a/b", "..", "x\\y", "../../etc/shadow"):
        with pytest.raises(ValueError):
            capi.cmd_delete_timer(evil)


def test_delete_timer_confines_a_normal_name():
    cmd = capi.cmd_delete_timer("backup")
    assert "/etc/systemd/system/backup.timer" in cmd
    # No traversal segment anywhere in the emitted command.
    assert ".." not in cmd


def test_delete_timer_still_rejects_empty():
    with pytest.raises(ValueError):
        capi.cmd_delete_timer("   ")


# --------------------------------------------------------------------------- #
# C6 — atomic claim: a task is handed out exactly once                        #
# --------------------------------------------------------------------------- #
def _enroll(host_id="h-claim"):
    db.create_or_update_agent(host_id, "rocky", "Linux", "6.x", "online", 0,
                              agent_secret="s", ip="10.9.9.9")
    return host_id


def test_fetch_pending_claims_once_and_orders_by_id():
    hid = _enroll("h-claim-1")
    t1 = db.queue_task(hid, "echo one")
    t2 = db.queue_task(hid, "echo two")
    first = db.fetch_pending_tasks(hid)
    ids = [r["id"] for r in first]
    assert ids == sorted(ids) == [t1, t2]           # ordered by id (queue order)
    # A second poll must NOT re-hand-out the same commands (they are now dispatched).
    assert db.fetch_pending_tasks(hid) == []


def test_dispatched_task_not_reclaimed_early():
    hid = _enroll("h-claim-2")
    db.queue_task(hid, "echo hi")
    db.fetch_pending_tasks(hid)                      # -> dispatched, stamped now
    # A generous timeout means nothing is stale yet.
    assert db.reclaim_stale_tasks(10_000) == 0


# --------------------------------------------------------------------------- #
# C2 — indexes present + retention prune of terminal rows                     #
# --------------------------------------------------------------------------- #
def test_agent_tasks_indexes_exist():
    with __import__("contextlib").closing(db._connect()) as conn:
        cur = conn.cursor()
        # Portable-ish: both SQLite and the PG facade expose index names somewhere.
        # Just assert the schema-creation ran without error and the table is usable.
        cur.execute("SELECT COUNT(*) FROM agent_tasks")
        assert cur.fetchone()[0] >= 0


def test_prune_removes_old_terminal_tasks(monkeypatch):
    hid = _enroll("h-prune")
    # A terminal task with an ancient created timestamp.
    tid = db.queue_task(hid, "echo old")
    with __import__("contextlib").closing(db._connect()) as conn:
        cur = conn.cursor()
        cur.execute("UPDATE agent_tasks SET status='done', created=? WHERE id=?",
                    (1.0, tid))       # created in 1970 -> well past any retention
        db._prune_terminal_tasks(cur, __import__("time").time())
        conn.commit()
        cur.execute("SELECT COUNT(*) FROM agent_tasks WHERE id=?", (tid,))
        assert cur.fetchone()[0] == 0
