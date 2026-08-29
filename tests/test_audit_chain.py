"""Tamper-evident hash chain over activity_log + admin_audit_log.

Verifies: a clean chain validates and reports keyed/unkeyed correctly; editing,
removing a middle row, or truncating the tail is detected and pinpointed; the
one-time backfill hashes pre-existing (pre-upgrade) rows in insertion order; and
the unkeyed SHA-256 fallback still validates.
"""
import sqlite3

import pytest

import backend.db as db

_KEY = b"k" * 32


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "audit.db")
    monkeypatch.setattr(db, "_audit_key", lambda: _KEY)  # keyed by default
    db.init_db()
    return db


def _raw(dbmod):
    conn = sqlite3.connect(str(dbmod.DB_PATH))
    return conn


def test_clean_chain_verifies_keyed(fresh_db):
    for i in range(5):
        fresh_db.log_activity("alice", "web0%d" % i, "did thing %d" % i, "cmd %d" % i)
    res = fresh_db.verify_activity_chain()
    assert res["ok"] is True
    assert res["checked"] == 5
    assert res["keyed"] is True
    assert res["broken_at"] is None


def test_edit_is_detected(fresh_db):
    for i in range(4):
        fresh_db.log_activity("bob", "h", "line %d" % i)
    conn = _raw(fresh_db)
    # Tamper: change a retained row's description without fixing its hash.
    conn.execute("UPDATE activity_log SET description='HACKED' WHERE id=2")
    conn.commit()
    conn.close()
    res = fresh_db.verify_activity_chain()
    assert res["ok"] is False
    assert res["broken_at"] == 2


def test_middle_row_deletion_is_detected(fresh_db):
    for i in range(4):
        fresh_db.log_activity("bob", "h", "line %d" % i)
    conn = _raw(fresh_db)
    conn.execute("DELETE FROM activity_log WHERE id=2")  # break continuity
    conn.commit()
    conn.close()
    res = fresh_db.verify_activity_chain()
    assert res["ok"] is False
    # The row after the hole no longer chains to its recorded predecessor.
    assert res["broken_at"] == 3


def test_tail_truncation_is_detected(fresh_db):
    for i in range(4):
        fresh_db.log_activity("bob", "h", "line %d" % i)
    conn = _raw(fresh_db)
    conn.execute("DELETE FROM activity_log WHERE id=(SELECT MAX(id) FROM activity_log)")
    conn.commit()
    conn.close()
    res = fresh_db.verify_activity_chain()
    # The prev_hash walk still validates the shorter chain, but the recorded
    # high-water mark is above the new tail -> truncation flagged.
    assert res["ok"] is False
    assert res.get("truncated") is True


def test_unkeyed_fallback_still_verifies(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "unkeyed.db")
    monkeypatch.setattr(db, "_audit_key", lambda: None)  # no master key
    # Strict-audit mode fails closed on a missing key; this test exercises the
    # deliberate degraded/dev fallback, so opt out of strict mode explicitly.
    monkeypatch.setenv("SYSIBLE_AUDIT_REQUIRED", "0")
    db.init_db()
    for i in range(3):
        db.log_activity("u", "h", "x %d" % i)
    res = db.verify_activity_chain()
    assert res["ok"] is True
    assert res["keyed"] is False   # tamper-evident but not unforgeable


def test_backfill_hashes_preexisting_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "backfill.db")
    monkeypatch.setattr(db, "_audit_key", lambda: _KEY)
    db.init_db()
    # Simulate a pre-upgrade DB: rows already present with NO hash columns set.
    conn = _raw(db)
    for i in range(3):
        conn.execute("INSERT INTO activity_log (timestamp, username, host, description, command) "
                     "VALUES (?, ?, ?, ?, ?)", (1000.0 + i, "legacy", "h", "old %d" % i, ""))
    conn.execute("UPDATE activity_log SET prev_hash=NULL, entry_hash=NULL")
    conn.commit()
    # Backfill (as init_db would on the upgrade run).
    conn2 = sqlite3.connect(str(db.DB_PATH))
    db._backfill_audit_chains(conn2)
    conn2.close()
    res = db.verify_activity_chain()
    assert res["ok"] is True
    assert res["checked"] == 3
    assert res["keyed"] is True
    # A new append continues the backfilled chain.
    db.log_activity("newadmin", "h", "after upgrade")
    assert db.verify_activity_chain()["ok"] is True
    conn.close()


def test_admin_audit_chain_verifies_and_detects_edit(fresh_db):
    for i in range(3):
        fresh_db.log_admin_audit("login", "carol", "detail %d" % i)
    assert fresh_db.verify_admin_audit_chain()["ok"] is True
    conn = _raw(fresh_db)
    conn.execute("UPDATE admin_audit_log SET detail='tampered' WHERE id=1")
    conn.commit()
    conn.close()
    res = fresh_db.verify_admin_audit_chain()
    assert res["ok"] is False
    assert res["broken_at"] == 1
