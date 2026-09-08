"""The activity feed has to say what happened, and let you filter who did it.

Reported from a live console: page after page of identical rows —

    9/3/2026, 1:42:02 PM   api-key   ran a command   ubuntu-web-1   export PATH=…
    9/3/2026, 1:42:02 PM   api-key   ran a command   deb-web-1      export PATH=…
    9/3/2026, 1:38:44 PM   api-key   ran a command   rocky-web-1    export PATH=…

Every one of those is the controller's OWN read-only sweep (posture, health,
package-update checks) which runs against every host on every dashboard load.
_describe_command collapsed anything multi-line to "ran a command", so a
fleet-wide audit log could not distinguish an automated probe from a person
running a script as root — the log existed but told you nothing.

Two things are needed and tested here: a real description for every command, and
a server-side `source` so the feed can be filtered to what PEOPLE did.
"""
import importlib

import pytest

import backend.app as app_module
import backend.db as db
from tests.conftest import key_headers

DESCRIBE = app_module._describe_command
CLASSIFY = app_module._classify_activity


# ---- 1. every builder the controller dispatches gets a real description ----
def _client_builders():
    """Every cmd_* command builder, called with its defaults."""
    from client import _api_dispatch as d
    out = {}
    for name in dir(d):
        if not name.startswith("cmd_"):
            continue
        fn = getattr(d, name)
        if not callable(fn):
            continue
        try:
            out[name] = fn()
        except TypeError:
            try:
                out[name] = fn(False)
            except Exception:
                continue
        except Exception:
            continue
    return out


def test_no_dispatched_command_is_described_as_just_ran_a_command():
    """The anti-drift guard. These commands live in the client and get reworded;
    if one stops matching its signature it must fail HERE, not silently reappear
    in the feed as "ran a command"."""
    generic = []
    for name, cmd in _client_builders().items():
        if DESCRIBE(cmd) == "ran a command":
            generic.append(name)
    assert not generic, (
        "these builders produce commands with no useful description, so they will "
        f"fill the activity feed with 'ran a command': {generic}")


@pytest.mark.parametrize("name,expected", [
    ("cmd_posture_snapshot", "collected host posture"),
    ("cmd_update_status", "checked for available package updates"),
    ("cmd_health_check", "ran a fleet health check"),
])
def test_the_sweeps_that_flooded_the_feed_are_named(name, expected):
    from client import _api_dispatch as d
    fn = getattr(d, name)
    try:
        cmd = fn()
    except TypeError:
        cmd = fn(False)
    assert DESCRIBE(cmd) == expected


def test_an_unknown_script_is_summarised_not_dumped():
    """No raw code in the feed — but the shape and the programs it calls, which
    is the difference between an auditable log and a wall of 'ran a command'."""
    script = "\n".join([
        "export PATH=/usr/local/sbin:$PATH",
        "if command -v systemctl >/dev/null 2>&1; then",
        "  systemctl restart nginx",
        "fi",
        "find /etc -name '*.conf' -newer /tmp/stamp",
        "awk '{print $1}' /var/log/syslog",
    ])
    desc = DESCRIBE(script)
    assert desc.startswith("ran a 6-line script"), desc
    assert "systemctl" in desc and "find" in desc
    assert "/var/log/syslog" not in desc, "must not echo the command body"
    assert "export" not in desc, "shell scaffolding is not what the script does"


def test_a_short_single_line_command_is_shown_as_is():
    assert DESCRIBE("systemctl restart nginx") == "ran: systemctl restart nginx"


def test_an_empty_command_still_has_a_description():
    assert DESCRIBE("") == "ran a command"
    assert DESCRIBE(None) == "ran a command"


# ---- 2. classification -----------------------------------------------------
def test_the_controllers_own_sweeps_classify_as_automation():
    from client import _api_dispatch as d
    assert CLASSIFY(d.cmd_posture_snapshot(), "", False) == "automation"
    # ...even when an operator identity is attached: it is still a sweep, and
    # burying real actions behind it is the whole complaint.
    assert CLASSIFY(d.cmd_posture_snapshot(), "", True) == "automation"


def test_an_operator_action_classifies_as_user():
    assert CLASSIFY("systemctl restart nginx", "ran: systemctl restart nginx", True) == "user"


def test_a_key_only_call_classifies_as_api():
    assert CLASSIFY("rm -rf /tmp/x", "ran: rm -rf /tmp/x", False) == "api"


# ---- 3. the feed can be filtered ------------------------------------------
def _seed():
    db.log_activity("alice", "web01", "ran: systemctl restart nginx",
                    "systemctl restart nginx", source="user")
    db.log_activity("api-key", "web01", "collected host posture", "POSTURE|x=1",
                    source="automation")
    db.log_activity("api-key", "web02", "ran: rm -rf /tmp/x", "rm -rf /tmp/x", source="api")


def test_the_feed_can_be_narrowed_to_what_people_did(controller, superuser_headers):
    _seed()
    r = controller.get("/activity-log?source=user", headers=superuser_headers)
    assert r.status_code == 200, r.text
    rows = r.json()["entries"]
    assert rows and all(e["source"] == "user" for e in rows), rows
    assert any(e["username"] == "alice" for e in rows)


def test_automation_can_be_isolated(controller, superuser_headers):
    _seed()
    rows = controller.get("/activity-log?source=automation",
                          headers=superuser_headers).json()["entries"]
    assert rows and all(e["source"] == "automation" for e in rows)
    assert all("posture" in e["description"] for e in rows)


def test_no_filter_still_returns_everything(controller, superuser_headers):
    _seed()
    rows = controller.get("/activity-log", headers=superuser_headers).json()["entries"]
    assert {e["source"] for e in rows} >= {"user", "api", "automation"}


def test_an_unknown_filter_value_is_ignored_rather_than_returning_nothing(
        controller, superuser_headers):
    _seed()
    rows = controller.get("/activity-log?source=bogus",
                          headers=superuser_headers).json()["entries"]
    assert len(rows) >= 3


def test_rows_written_before_the_column_existed_read_as_api(controller, superuser_headers):
    """An upgrade must not blank out the existing feed."""
    db.log_activity("api-key", "web03", "ran a command", "whoami")
    # Through the backend's own connector, not sqlite3 directly: Enterprise runs
    # this same file against PostgreSQL.
    conn = db._connect()
    conn.execute("UPDATE activity_log SET source=NULL")
    conn.commit()
    conn.close()
    rows = controller.get("/activity-log", headers=superuser_headers).json()["entries"]
    assert rows and all(e["source"] == "api" for e in rows)
    assert controller.get("/activity-log?source=api",
                          headers=superuser_headers).json()["entries"]


# ---- 4. the audit chain still verifies with the new column -----------------
def test_adding_source_did_not_break_the_tamper_evident_chain(controller, superuser_headers):
    _seed()
    v = controller.get("/activity-log/verify", headers=superuser_headers).json()
    assert v["ok"] is True, v
