"""
Regression tests for the QA/security sweep fixes (both-edition shared code):

- Q7  activity-log incremental polling must not drop rows during a burst.
- Q14 the alerts evaluator must survive a null/blank threshold (no int(None) crash).
- Q8/Q13 the swap-file builders shrink correctly and match /etc/fstab exactly.
"""
import backend.db as db
from webgui.alerts import evaluate_rules
from client._api_storage import (
    cmd_create_swap_file, cmd_resize_swap_file, cmd_create_swap_partition,
)


# ---------------------------------------------------------------------------
# Q7 — get_activity_log(since_id>0) delivers EVERY new row across polls.
# ---------------------------------------------------------------------------
def test_activity_since_id_incremental_delivers_all_new_rows():
    for i in range(6):
        db.log_activity(f"u{i}", "host", f"desc{i}")
    all_ids = sorted(r["id"] for r in db.get_activity_log(limit=1000, since_id=0))
    assert len(all_ids) == 6
    # A poller that has already seen through the 2nd row: 4 newer rows remain — more
    # than the page size below, which is exactly when the old DESC+LIMIT dropped the
    # middle rows in (cursor, max-limit].
    cursor = all_ids[1]
    expected = [i for i in all_ids if i > cursor]
    assert len(expected) == 4

    collected = []
    for _ in range(20):
        rows = db.get_activity_log(limit=2, since_id=cursor)
        if not rows:
            break
        ids = sorted(r["id"] for r in rows)
        assert all(i > cursor for i in ids)                      # only new rows
        assert not (set(ids) & set(collected)), "row delivered twice"
        collected.extend(ids)
        cursor = max(ids)                                        # advance the cursor
    assert sorted(collected) == expected, "an incremental poll skipped audit rows"


def test_activity_since_id_zero_is_newest_first():
    for i in range(3):
        db.log_activity(f"u{i}", "host", f"d{i}")
    rows = db.get_activity_log(limit=10, since_id=0)
    ids = [r["id"] for r in rows]
    assert ids == sorted(ids, reverse=True)                     # newest-first view


# ---------------------------------------------------------------------------
# Q14 — a null threshold must not crash the evaluation cycle.
# ---------------------------------------------------------------------------
def test_alerts_null_threshold_does_not_crash_and_uses_default():
    cfg = {"rules": {"disk_critical": {"enabled": True, "threshold": None}}}
    hosts = [{"id": "h1", "host": "h1", "disk": 95}]
    firing = evaluate_rules(cfg, hosts)                          # must NOT raise
    assert any(f["rule"] == "disk_critical" for f in firing)    # fired at default 90

    # Blank string and garbage also fall back to the default rather than throwing.
    for bad in ("", "not-a-number", None):
        cfg["rules"]["disk_critical"]["threshold"] = bad
        assert evaluate_rules(cfg, hosts)                        # still fires, no crash


# ---------------------------------------------------------------------------
# Q8 / Q13 — swap builders: shrink works + exact first-field fstab match.
# ---------------------------------------------------------------------------
def test_swap_resize_removes_old_file_before_recreate():
    r = cmd_resize_swap_file("/swapfile", 512)
    assert "rm -f" in r                                         # shrink no longer no-ops
    assert "grep -qF" not in r                                  # old substring match gone
    assert 'ENVIRON["p"]' in r                                  # exact first-field match


def test_swap_builders_use_exact_fstab_field_match():
    for cmd in (cmd_create_swap_file("/swap", 256),
                cmd_create_swap_partition("/dev/sdb1")):
        assert 'ENVIRON["p"]' in cmd
        assert "grep -qF" not in cmd
