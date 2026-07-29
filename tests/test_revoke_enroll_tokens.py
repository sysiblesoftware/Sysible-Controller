"""Revoke-unconsumed-enroll-tokens (security remediation F-6).

An unclaimed enroll token is a bearer credential that lets any allowlisted host
enroll for its TTL. `purge_unconsumed_enroll_tokens()` (exposed via
`sysible_controller revoke-enroll-tokens`) invalidates every minted-but-unclaimed
token in one action, while leaving already-claimed tokens intact so a legitimate
host's within-window re-enroll still works.
"""
import time

import backend.db as db


def test_purge_removes_only_unconsumed_tokens():
    # A fresh, never-claimed token.
    unclaimed = "enroll-" + "b" * 24
    db.create_enroll_token(unclaimed)
    assert db.validate_enroll_token(unclaimed)

    # A claimed token bound to a host (simulates a completed enroll).
    claimed = "enroll-" + "c" * 24
    db.create_enroll_token(claimed)
    db.consume_enroll_token(claimed, "host-keepme")

    removed = db.purge_unconsumed_enroll_tokens()

    assert removed >= 1
    # The unclaimed token is gone -> can no longer be used to enroll.
    assert not db.validate_enroll_token(unclaimed)
    # The claimed token survives (its host's re-enroll window is preserved).
    assert db.validate_enroll_token(claimed)


def test_purge_is_safe_when_no_tokens():
    # Clear the table, then purging again is a harmless no-op returning 0.
    db.purge_unconsumed_enroll_tokens()
    assert db.purge_unconsumed_enroll_tokens() == 0
