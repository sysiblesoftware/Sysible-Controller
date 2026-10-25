"""Controller -> Sysible Baseline, against the real service on the other end.

The two halves ship separately, so the seam between them is the thing most
likely to be wrong and least likely to be noticed: both sides pass their own
tests while disagreeing about the wire. These drive the REAL Baseline app from
the SLOP checkout where there is one, and fall back to pinning the contract
where there is not.

What the seam has to get right:

  * Flashback and Baseline are different services with different tokens. The
    Controller holding one and using it for the other fails in a way nobody
    would think to look for — a snapshot posted to the config manager, or a
    desired-state request to the backup tool.
  * "No state assigned" is not "compliant". A host nobody declared anything for
    is unmanaged, and a console that cannot tell those apart reports an entire
    unconfigured fleet as green.
  * An action the Controller does not understand must be REPORTED, not dropped.
    Baseline and the Controller update independently, so a rule using a newer
    action would otherwise read as active in one console and do nothing here.
"""
import importlib
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SLOP = Path("/home/user/sysible-linux-operations-platform")
BASELINE_DIR = SLOP / "baseline"
TOKEN = "b" * 64


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("SYSIBLE_BASELINE_URL", "http://baseline.test")
    monkeypatch.setenv("SYSIBLE_BASELINE_AGENT_TOKEN", TOKEN)
    from backend import baseline
    return importlib.reload(baseline)


# ---- the contract, with or without the SLOP checkout ----------------------

def test_the_two_products_use_separate_tokens(client, monkeypatch):
    """Flashback's token reads and writes a host's stored configuration;
    Baseline's declares what a host must become. Sharing one would make a
    compromise of either a compromise of both."""
    monkeypatch.setenv("SYSIBLE_FLASHBACK_AGENT_TOKEN", "f" * 64)
    monkeypatch.setenv("SYSIBLE_FLASHBACK_URL", "http://flashback.test")
    from backend import flashback
    fb = importlib.reload(flashback)
    assert client.AGENT_TOKEN != fb.AGENT_TOKEN, "one token serves both services"
    assert client.BASELINE_URL != fb.FLASHBACK_URL, "both services at one address"


def test_baseline_does_not_read_flashbacks_settings(monkeypatch):
    monkeypatch.delenv("SYSIBLE_BASELINE_URL", raising=False)
    monkeypatch.delenv("SYSIBLE_BASELINE_AGENT_TOKEN", raising=False)
    monkeypatch.setenv("SYSIBLE_FLASHBACK_URL", "http://flashback.test")
    monkeypatch.setenv("SYSIBLE_FLASHBACK_AGENT_TOKEN", "f" * 64)
    from backend import baseline
    m = importlib.reload(baseline)
    assert m.configured() is False, "the Controller would send desired-state " \
                                    "requests to the backup tool"


def test_an_unconfigured_controller_degrades_rather_than_raises(monkeypatch):
    monkeypatch.delenv("SYSIBLE_BASELINE_URL", raising=False)
    monkeypatch.delenv("SYSIBLE_BASELINE_AGENT_TOKEN", raising=False)
    from backend import baseline
    m = importlib.reload(baseline)
    with pytest.raises(m.BaselineUnavailable) as e:
        m.desired("h1")
    assert "SYSIBLE_BASELINE_URL" in e.value.reason
    assert e.value.status == 503


def test_an_unknown_action_is_reported_not_dropped(client):
    actions = [{"action": "capture"}, {"action": "notify"}, {"action": "quarantine"}]
    assert client.unknown_actions(actions) == ["quarantine"]
    assert client.unknown_actions([{"action": "capture"}]) == []


def test_the_token_never_reaches_an_error_message(client, monkeypatch):
    import requests

    def boom(*a, **kw):
        raise requests.ConnectionError("refused")
    monkeypatch.setattr(requests, "request", boom)
    with pytest.raises(client.BaselineUnavailable) as e:
        client.desired("h1")
    assert TOKEN not in e.value.reason and "refused" not in e.value.reason


def test_an_enormous_result_is_truncated_not_dropped(client, monkeypatch):
    """A state printing a whole file must not cost the whole run's record."""
    seen = {}

    def fake(method, url, **kw):
        seen["body"] = kw.get("json") or {}

        class R:
            status_code = 200
            def json(self): return {"ok": True}
        return R()
    monkeypatch.setattr(client.requests, "request", fake)
    client.report(1, "x" * (client.MAX_RESULT_BYTES + 5000))
    assert len(seen["body"]["output"]) <= client.MAX_RESULT_BYTES + 100
    assert "truncated" in seen["body"]["output"]


# ---- against the real Baseline service ------------------------------------

needs_slop = pytest.mark.skipif(not BASELINE_DIR.is_dir(),
                                reason="the SLOP checkout is not next to this repo")


@pytest.fixture()
def real(tmp_path, monkeypatch):
    """The actual Baseline app, wired to the actual Controller client.

    NAME COLLISION, handled the way tests/test_flashback_relay.py handles it:
    Baseline's package is ALSO called `backend`. The originals are saved and
    restored so this controller's modules are exactly as they were.
    """
    os.environ.update(
        SYSIBLE_BASELINE_DATA=str(tmp_path),
        SYSIBLE_BASELINE_AGENT_TOKEN=TOKEN,
        SYSIBLE_BASELINE_TRUST_GATEWAY_AUTH="1",
        SYSIBLE_SSO_SHARED_SECRET="s" * 32,
    )
    saved = {k: v for k, v in sys.modules.items()
             if k == "backend" or k.startswith("backend.")}
    for k in saved:
        del sys.modules[k]
    sys.path.insert(0, str(BASELINE_DIR))
    try:
        store = importlib.import_module("backend.store")
        store.init_db()
        app_mod = importlib.import_module("backend.app")
        from fastapi.testclient import TestClient
        server = TestClient(app_mod.app)
    finally:
        sys.path.remove(str(BASELINE_DIR))
        for k in [k for k in list(sys.modules) if k == "backend" or k.startswith("backend.")]:
            del sys.modules[k]
        sys.modules.update(saved)

    monkeypatch.setenv("SYSIBLE_BASELINE_URL", "http://baseline.test")
    monkeypatch.setenv("SYSIBLE_BASELINE_AGENT_TOKEN", TOKEN)
    from backend import baseline as client_mod
    client_mod = importlib.reload(client_mod)

    import requests as _rq

    def _route(method, url, **kw):
        kw.pop("timeout", None)
        return server.request(method, url[len("http://baseline.test"):], **kw)
    monkeypatch.setattr(_rq, "request", _route)
    return client_mod, server, store


@needs_slop
def test_a_host_with_no_state_is_unmanaged_not_compliant(real):
    client_mod, _, _ = real
    assert client_mod.desired("never-declared") == [], \
        "an undeclared host should report nothing to apply"


@needs_slop
def test_the_whole_cycle(real):
    """Declare, ask, apply, report, and get the actions back."""
    client_mod, server, store = real
    op = {"x-sysible-user": "alice", "x-sysible-role": "operator",
          "x-sysible-auth": "s" * 32}
    assert server.post("/api/states/web", headers=op,
                       json={"sls": "nginx:\n  pkg.installed: []\n"}).status_code == 200
    assert server.post("/api/assign", headers=op,
                       json={"target": "h1", "state": "web"}).status_code == 200
    server.post("/api/rules", headers=op,
                json={"name": "snapshot", "match": "*", "trigger": "changed",
                      "action": "capture"})

    todo = client_mod.desired("h1")
    assert len(todo) == 1 and todo[0]["state"] == "web"
    assert "salt-call" in todo[0]["command"] and "--local" in todo[0]["command"]

    out = json.dumps({"local": {"pkg_|-nginx_|-nginx_|-installed": {
        "result": True, "changes": {"nginx": {"new": "1"}}, "comment": "", "name": "nginx"}}})
    res = client_mod.report(todo[0]["run_id"], out)
    assert res["ok"] is True and res["changed"] == 1
    assert [a["action"] for a in res["actions"]] == ["capture"], \
        "the Flashback hand-off did not come back"
    assert client_mod.unknown_actions(res["actions"]) == []


@needs_slop
def test_a_host_that_could_not_run_salt_is_not_reported_compliant(real):
    """The failure that matters most, across the seam this time."""
    client_mod, server, _ = real
    op = {"x-sysible-user": "alice", "x-sysible-role": "operator",
          "x-sysible-auth": "s" * 32}
    server.post("/api/states/web", headers=op, json={"sls": "a:\n  test.nop: []\n"})
    server.post("/api/assign", headers=op, json={"target": "h2", "state": "web"})
    todo = client_mod.desired("h2")
    res = client_mod.report(todo[0]["run_id"], "bash: salt-call: command not found")
    assert res["ok"] is False and res["error"], "a host without salt reported as clean"
    assert res["actions"] == []


@needs_slop
def test_a_bad_token_is_refused_by_the_real_service(real, monkeypatch):
    client_mod, _, _ = real
    monkeypatch.setattr(client_mod, "AGENT_TOKEN", "wrong" * 13)
    with pytest.raises(client_mod.BaselineUnavailable) as e:
        client_mod.desired("h1")
    assert "token" in e.value.reason.lower()


# ---- every KNOWN action must actually be carried out ------------------------
# unknown_actions() catches a rule using an action this Controller has never
# heard of. It cannot catch the opposite: an action that IS in KNOWN_ACTIONS and
# has no branch in _carry_out_baseline_actions. That one is worse, because the
# warning deliberately does not fire — the action is known — so the rule reads
# as active in both consoles and does nothing in either. `reapply` was exactly
# that for one commit.
def _carry_out_source():
    import ast
    src = (REPO / "webgui" / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_carry_out_baseline_actions":
            return node
    raise AssertionError("_carry_out_baseline_actions is gone")


def _handled_actions() -> set:
    """Which action names the function actually branches on.

    Read from the AST, not the text: a name in a docstring, a comment or a log
    message is not a branch, and four assertions in this repo have already been
    satisfied by prose rather than code.
    """
    import ast
    handled = set()
    for node in ast.walk(_carry_out_source()):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        if not (isinstance(left, ast.Name) and left.id == "kind"):
            continue
        for op, cmp in zip(node.ops, node.comparators):
            if isinstance(op, ast.Eq) and isinstance(cmp, ast.Constant) \
                    and isinstance(cmp.value, str):
                handled.add(cmp.value)
    return handled


def test_every_known_action_has_a_branch_that_carries_it_out():
    from backend import baseline
    handled = _handled_actions()
    missing = set(baseline.KNOWN_ACTIONS) - handled
    assert not missing, (
        f"{sorted(missing)} are in KNOWN_ACTIONS, so unknown_actions() stays "
        f"silent about them, but nothing in _carry_out_baseline_actions acts on "
        f"them. A rule using one reads as active and does nothing. Handled: "
        f"{sorted(handled)}")


def test_no_branch_claims_an_action_that_is_not_known():
    """The other direction: a branch for an action unknown_actions() would warn
    about is dead code that also makes the warning a lie."""
    from backend import baseline
    extra = _handled_actions() - set(baseline.KNOWN_ACTIONS)
    assert not extra, (
        f"{sorted(extra)} are carried out but are not in KNOWN_ACTIONS, so "
        f"unknown_actions() reports them as unsupported while they run")


def test_reapply_cannot_reapply_forever():
    """Two rules that each reapply on change would otherwise take turns on a
    state that never converges. The guard is a once-per-cycle flag, and the
    second run's own actions are deliberately not carried out."""
    import ast
    node = _carry_out_source()
    src = ast.unparse(node)
    assert "reapplied" in src, "the once-per-cycle reapply guard is gone"
    # The flag must be CHECKED before the dispatch, not merely assigned.
    assert "or reapplied" in src or "if reapplied" in src, \
        "reapplied is set but never tested — the guard does not guard"
    # The reapply must not recurse into carrying out its own result's actions.
    # Checked as a CALL in the AST: ast.unparse(node) includes the def line, so
    # a substring search for the name matches the function's own signature and
    # can never fail. (It did not, which is how this comment exists.)
    calls = [n for n in ast.walk(node)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_carry_out_baseline_actions"]
    assert not calls, \
        "_carry_out_baseline_actions calls itself — a reapply loop has no floor"


def _branch_for(action: str):
    """The body of the `kind == "<action>"` branch, as AST statements."""
    import ast
    for node in ast.walk(_carry_out_source()):
        if not isinstance(node, ast.If):
            continue
        t = node.test
        if isinstance(t, ast.Compare) and isinstance(t.left, ast.Name) \
                and t.left.id == "kind" and len(t.comparators) == 1 \
                and isinstance(t.comparators[0], ast.Constant) \
                and t.comparators[0].value == action:
            return node.body
    raise AssertionError(f"there is no `kind == {action!r}` branch")


def test_a_preview_never_reapplies_to_a_host():
    """Preview is the one mode an operator runs to find out what WOULD happen.
    A reapply firing during it would make that a lie — and it runs salt for
    real, as root.

    Asserted on the GUARD, not on the word: `"preview" in source` is satisfied
    by the docstring above it, which is how two assertions in this repo passed
    while checking nothing.
    """
    import ast
    body = _branch_for("reapply")
    guards = [n for n in body
              if isinstance(n, ast.If)
              and any(isinstance(x, ast.Name) and x.id == "preview"
                      for x in ast.walk(n.test))
              and any(isinstance(x, ast.Continue) for x in ast.walk(n))]
    assert guards, (
        "nothing in the reapply branch tests `preview` and skips — a dry run "
        "would apply the state for real, as root")
    # And the guard must come BEFORE anything is dispatched.
    first_dispatch = next(
        (i for i, n in enumerate(body)
         if any(isinstance(x, ast.Call) and isinstance(x.func, ast.Name)
                and x.func.id == "_dispatch_one" for x in ast.walk(n))), None)
    assert first_dispatch is not None, "the reapply branch dispatches nothing at all"
    assert body.index(guards[0]) < first_dispatch, \
        "the preview guard sits after the dispatch it is supposed to prevent"
