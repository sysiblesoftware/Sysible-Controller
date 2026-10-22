"""A nightly patch run that reached 14 of 16 hosts is not a failed run.

Reported as "I created a schedule but it's not working for patching", against a
schedule whose every run for two weeks read:

    error · 11/16 host(s) succeeded
    error · 14/16 host(s) succeeded
    error · 9/16  host(s) succeeded

It was working. One line decided the outcome:

    status = "ok" if ok == len(entries) else "error"

so a fleet patch with a host switched off reported exactly what a run where
nothing patched at all reports. Fifteen of sixteen is red; zero of sixteen is
red. Every morning, for weeks — and the operator reasonably concluded the
schedule was broken.

Worse, nothing recorded WHICH hosts missed out, so there was no way to tell
whether the same two fail nightly or a different two, nor whether they were
offline or genuinely failing to patch. Those need opposite responses.
"""
import pytest

from webgui import server


class _Job(dict):
    pass


@pytest.fixture()
def fleet(monkeypatch):
    """A fleet whose per-host outcomes the test decides, driving the real
    _run_scheduled_job rather than re-implementing its arithmetic."""
    state = {"hosts": [], "results": {}}

    def entries():
        return [{"id": h, "label": h, "kind": "agent", "environment": "Dev"}
                for h in state["hosts"]]

    monkeypatch.setattr(server.dispatch, "list_merged_hosts", lambda **k: entries())
    monkeypatch.setattr(server.api, "cmd_update_all", lambda *a, **k: "true", raising=False)

    def fake_dispatch(e, cmd, kind, become, token, desc, **kw):
        return state["results"][e["label"]]

    monkeypatch.setattr(server, "_dispatch_one", fake_dispatch)

    class Fleet:
        def host(self, name, ok=True, error=None, code=None):
            """code defaults to None for a failure, which is what _dispatch_one
            really returns when it could not reach the host — 0 would be counted
            as a success by the `r.get("code") == 0` arm, which is how the first
            version of this fixture reported a fleet of offline hosts as 10/10."""
            state["hosts"].append(name)
            if code is None:
                code = 0 if ok else None
            state["results"][name] = {"host": name, "ok": ok, "error": error,
                                      "stderr": "", "code": code}

        def run(self, action="all_updates"):
            return server._run_scheduled_job(
                _Job({"id": "j1", "action": action, "targets": state["hosts"],
                      "created_by": "admin", "arg": ""}))

    return Fleet()


# ---- the three outcomes ----------------------------------------------------
def test_every_host_succeeding_is_ok(fleet):
    for n in ("a", "b", "c"):
        fleet.host(n)
    status, detail = fleet.run()
    assert status == "ok"
    assert "3/3" in detail


def test_some_hosts_succeeding_is_partial_not_error(fleet):
    """The report that prompted this. It is not a failed run."""
    for n in ("a", "b"):
        fleet.host(n)
    fleet.host("c", ok=False, error="offline")

    status, detail = fleet.run()

    assert status == "partial", "a mostly-successful fleet patch still reads as failed"
    assert "2/3" in detail


def test_no_host_succeeding_is_still_an_error(fleet):
    """The distinction only means something if a real failure stays red."""
    for n in ("a", "b"):
        fleet.host(n, ok=False, error="offline")
    status, _ = fleet.run()
    assert status == "error"


# ---- which hosts, and why --------------------------------------------------
def test_the_hosts_that_missed_out_are_named(fleet):
    """A count cannot be acted on: it cannot say whether the same two fail every
    night or a different two."""
    fleet.host("ok-1")
    fleet.host("rocky-03", ok=False, error="offline")

    _, detail = fleet.run()

    assert "rocky-03" in detail


def test_offline_is_distinguished_from_a_real_failure(fleet):
    """Switched off and 'dpkg failed' need opposite responses, and lumping them
    together is what buries the one that matters."""
    fleet.host("ok-1")
    fleet.host("opensuse-01", ok=False, error="offline")
    fleet.host("deb-web-1", ok=False, error="E: Could not get lock /var/lib/dpkg/lock")

    _, detail = fleet.run()

    assert "1 offline" in detail and "opensuse-01" in detail
    assert "1 failed" in detail and "deb-web-1" in detail
    # the real failure must not be hidden behind the boring one
    assert detail.index("offline") < detail.index("failed")


def test_a_long_failure_list_is_summarised_not_truncated_mid_host(fleet):
    """record_run caps the detail at 500 characters, so an unbounded list would
    lose the end of it — and with it the count."""
    fleet.host("ok-1")
    for i in range(9):
        fleet.host(f"host-{i:02d}", ok=False, error="offline")

    _, detail = fleet.run()

    assert "9 offline" in detail, detail
    assert "+5" in detail, "it does not say how many it did not list"
    assert len(detail) < 500, len(detail)


def test_a_failure_reason_does_not_run_away(fleet):
    """stderr can be a screenful. One line, bounded."""
    fleet.host("ok-1")
    fleet.host("noisy", ok=False, error="x" * 4000)

    _, detail = fleet.run()

    assert len(detail) < 500, len(detail)


# ---- the console must not paint partial as failure -------------------------
def test_the_console_colours_partial_differently():
    from pathlib import Path
    src = Path("webgui/frontend/src/views/Schedules.jsx").read_text(encoding="utf-8")
    assert "runColor" in src
    assert '"partial"' in src
    i = src.index("function runColor")
    body = src[i:i + 300]
    assert "#e0a83a" in body, "partial is not amber"
    assert body.index("#4ec07a") < body.index("#e0a83a") < body.index("#e06c6c")


def test_the_stored_model_documents_the_new_state():
    from pathlib import Path
    src = Path("webgui/schedules.py").read_text(encoding="utf-8")
    assert "ok|partial|error|running" in src
