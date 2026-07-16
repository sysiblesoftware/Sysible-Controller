"""The login throttle is DB-backed so a lockout survives a controller restart
(the previous in-memory dict reset on every process start, letting an attacker
clear accumulated failures by bouncing the service)."""
import backend.db as db


def test_lockout_after_max_failures_and_survives_reload():
    key = "attacker@example.com"
    window, maxf, lockout = 900, 5, 600
    # Under the threshold: not locked yet.
    for _ in range(maxf - 1):
        assert db.login_throttle_record_failure(key, window, maxf, lockout) == 0
    assert db.login_throttle_locked_for(key) == 0
    # The failure that hits the threshold sets the lockout.
    locked = db.login_throttle_record_failure(key, window, maxf, lockout)
    assert locked == lockout
    # The lockout is persisted in the table (survives a "restart" — a fresh read).
    remaining = db.login_throttle_locked_for(key)
    assert 0 < remaining <= lockout


def test_clear_on_success_resets():
    key = "good@example.com"
    db.login_throttle_record_failure(key, 900, 3, 600)
    db.login_throttle_clear(key)
    assert db.login_throttle_locked_for(key) == 0


def test_empty_key_is_noop():
    assert db.login_throttle_locked_for("") == 0
    assert db.login_throttle_record_failure("", 900, 3, 600) == 0
    db.login_throttle_clear("")  # must not raise


def test_failures_outside_window_do_not_accumulate():
    # Simulate old failures by writing them directly with timestamps far in the past.
    import json
    import time
    key = "slow@example.com"
    conn = db._connect()
    conn.execute("INSERT INTO login_throttle (key, fails, until) VALUES (?, ?, ?)",
                 (key, json.dumps([time.time() - 100000, time.time() - 99999]), 0))
    conn.commit()
    conn.close()
    # A new failure with a 900s window prunes the stale ones, so we're nowhere near lockout.
    assert db.login_throttle_record_failure(key, 900, 5, 600) == 0
    assert db.login_throttle_locked_for(key) == 0
