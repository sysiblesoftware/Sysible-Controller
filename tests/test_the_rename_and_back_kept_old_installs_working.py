"""Flashback was Sysible Flashback. The Controller, SLOP and the host agents are
three separate deployments on three separate schedules, and none of them knows.

A rebrand is cheap work with one expensive failure mode: the names that are
BRAND and the names that are CONTRACT are spelled identically here.
"SYSIBLE_FLASHBACK_AGENT_TOKEN" and "Sysible Flashback" are the same word, and
only one of them can be renamed without taking config backup down across the
fleet — reported, if it happens, as "Flashback isn't backing anything up".

The evidence that this is not hypothetical is in the repository: this feature was
Sysible D3lorean before it was Flashback, THAT rename changed the product name
and left the variables, and the agent still answers to SYSIBLE_D3LOREAN_* today.

Two of these tests exist because the mistake was made while writing this change
and the tests are what found it. Both the Controller's relay and the agent's
restore reader were given a two-name fallback, and a later blanket rename
rewrote BOTH alternatives to the new name — leaving `get("X-Flashback-Path") or
get("X-Flashback-Path")`, and a dict with the same key twice, which Python
silently collapses. Compatibility code that falls back to itself reads correctly,
imports cleanly, passes every other test, and does nothing at all.
"""
import ast
import importlib
import os
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

SETTINGS = [
    ("URL", "http://flashback.example:8770"),
    ("AGENT_TOKEN", "t" * 64),
    ("TIMEOUT_S", "7"),
    ("MAX_SNAPSHOT_BYTES", "1234567"),
]


def _reload(monkeypatch):
    from backend import flashback
    return importlib.reload(flashback)


@pytest.mark.parametrize("suffix,value", SETTINGS)
def test_the_pre_rebrand_variable_still_configures_the_controller(monkeypatch, suffix, value):
    """The SLOP installer seeds these into the Controller's .env. A Controller
    recreated BEFORE the SLOP stack is reading an .env that still says FLASHBACK;
    if that stopped resolving, the URL and token would both read empty, which is
    exactly how "config backup is switched off" is spelled."""
    monkeypatch.delenv("SYSIBLE_FLASHBACK_" + suffix, raising=False)
    monkeypatch.setenv("SYSIBLE_FLASHBACK_" + suffix, value)
    m = _reload(monkeypatch)
    got = {"URL": lambda: m.FLASHBACK_URL,
           "AGENT_TOKEN": lambda: m.AGENT_TOKEN,
           "TIMEOUT_S": lambda: str(int(m.TIMEOUT_S)),
           "MAX_SNAPSHOT_BYTES": lambda: str(m.MAX_SNAPSHOT_BYTES)}[suffix]()
    assert got == value.rstrip("/") or got == value


@pytest.mark.parametrize("suffix,value", SETTINGS)
def test_the_current_variable_wins_over_a_stale_one(monkeypatch, suffix, value):
    """Both spellings in one file is precisely what an upgrade leaves behind."""
    monkeypatch.setenv("SYSIBLE_FLASHBACK_" + suffix, "9" if suffix.endswith(("_S", "BYTES")) else "stale")
    monkeypatch.setenv("SYSIBLE_FLASHBACK_" + suffix, value)
    m = _reload(monkeypatch)
    got = {"URL": lambda: m.FLASHBACK_URL,
           "AGENT_TOKEN": lambda: m.AGENT_TOKEN,
           "TIMEOUT_S": lambda: str(int(m.TIMEOUT_S)),
           "MAX_SNAPSHOT_BYTES": lambda: str(m.MAX_SNAPSHOT_BYTES)}[suffix]()
    assert got == value.rstrip("/") or got == value


def test_config_backup_is_on_when_only_the_old_names_are_set(monkeypatch):
    """configured() is the single gate on the whole feature. False here means
    every console row reads "this controller has no Flashback wiring" and no host
    ever captures again."""
    monkeypatch.delenv("SYSIBLE_FLASHBACK_URL", raising=False)
    monkeypatch.delenv("SYSIBLE_FLASHBACK_AGENT_TOKEN", raising=False)
    monkeypatch.setenv("SYSIBLE_FLASHBACK_URL", "http://flashback.example:8770")
    monkeypatch.setenv("SYSIBLE_FLASHBACK_AGENT_TOKEN", "t" * 64)
    assert _reload(monkeypatch).configured() is True


def test_an_explicitly_empty_token_still_means_off(monkeypatch):
    """Setting the new name to empty is how an operator turns the feature OFF.
    Inheriting the old value would re-arm it behind their back."""
    monkeypatch.setenv("SYSIBLE_FLASHBACK_AGENT_TOKEN", "t" * 64)
    monkeypatch.setenv("SYSIBLE_FLASHBACK_AGENT_TOKEN", "")
    monkeypatch.setenv("SYSIBLE_FLASHBACK_URL", "http://flashback.example:8770")
    assert _reload(monkeypatch).configured() is False


def test_the_controller_reads_the_restore_digest(monkeypatch):
    """One header name, and it is the original one. Flashback sent X-Baseline-*
    for exactly two days, always ALONGSIDE X-Flashback-* and never instead, so
    reading the original name covers every build of the service. Re-adding the
    Baseline spelling now would put the config manager's name on the backup
    tool's wire."""
    m = _reload(monkeypatch)

    class _R:
        def __init__(self, h):
            self.headers = h
            self.content = b"payload"

    def _payload(headers):
        monkeypatch.setattr(m, "FLASHBACK_URL", "http://flashback.test")
        monkeypatch.setattr(m, "AGENT_TOKEN", "t" * 64)
        monkeypatch.setattr(m, "_call", lambda *a, **kw: _R(headers))
        return m.restore_payload("h1", 1)[0]

    got = _payload({"X-Flashback-Path": "/etc/hosts", "X-Flashback-Sha256": "abc"})
    assert got == {"path": "/etc/hosts", "sha256": "abc"}

    # A rename-era service sent both; the one we read is present either way.
    both = _payload({"X-Flashback-Path": "/etc/hosts", "X-Flashback-Sha256": "abc",
                     "X-Baseline-Path": "/etc/hosts", "X-Baseline-Sha256": "abc"})
    assert both == {"path": "/etc/hosts", "sha256": "abc"}


def test_a_restore_relayed_to_an_agent_carries_the_digest():
    """The reader is a host AGENT — the slowest-moving half of the fleet. They
    update on their own heartbeat and some are powered off for weeks, so the
    name on this header has to be the one every build has always understood."""
    src = (REPO / "backend" / "app.py").read_text()
    i = src.index('"X-Flashback-Path"')
    block = src[i - 400:i + 400]
    for name in ("X-Flashback-Path", "X-Flashback-Sha256"):
        assert f'"{name}"' in block, f"{name} is not relayed to the agent"
    assert '"X-Baseline-Path"' not in block, \
        "the config manager's name is on the backup tool's wire"


@pytest.mark.parametrize("suffix,default", [
    ("INTERVAL", "3600"),
    ("PATHS", "/etc:/boot/grub/grub.cfg:/var/spool/cron"),
    ("RESTORE_POLL", "60"),
    ("MAX_FILE_BYTES", str(256 * 1024)),
    ("MAX_TOTAL_BYTES", str(8 * 1024 * 1024)),
])
def test_the_agent_still_honours_the_d3lorean_variables(monkeypatch, suffix, default):
    """TWO names ago. An agent reads its environment from /etc/sysible/agent.env
    on a host that may not have been touched since enrolment, and an operator who
    narrowed PATHS to keep a snapshot under the cap would silently get the whole
    of /etc back — and then be cut off by the size ceiling they were avoiding."""
    import host_agent.agent as agent
    monkeypatch.delenv("SYSIBLE_FLASHBACK_" + suffix, raising=False)
    monkeypatch.setenv("SYSIBLE_D3LOREAN_" + suffix, "1234" if default.isdigit() else "/srv/conf")
    m = importlib.reload(agent)
    name = {"INTERVAL": "FB_INTERVAL", "PATHS": "FB_PATHS",
            "RESTORE_POLL": "FB_RESTORE_POLL", "MAX_FILE_BYTES": "FB_MAX_FILE",
            "MAX_TOTAL_BYTES": "FB_MAX_TOTAL"}[suffix]
    got = getattr(m, name)
    assert str(int(got) if isinstance(got, float) else got) in ("1234", "/srv/conf")


def test_the_agent_prefers_the_current_name_over_the_d3lorean_one(monkeypatch):
    import host_agent.agent as agent
    monkeypatch.setenv("SYSIBLE_D3LOREAN_PATHS", "/old")
    monkeypatch.setenv("SYSIBLE_FLASHBACK_PATHS", "/new")
    assert importlib.reload(agent).FB_PATHS == "/new"


def test_the_agent_reads_a_restore_digest_under_either_header():
    src = (REPO / "host_agent" / "agent.py").read_text()
    i = src.index('X-Flashback-Path')
    block = src[i - 200:i + 400]
    assert "X-Flashback-Path" in block, "the agent ignores a pre-rebrand Controller"
    assert "X-Flashback-Sha256" in block


# ---- the mistake this change actually made, twice -------------------------

FALLBACK_FILES = ["backend/flashback.py", "backend/app.py", "host_agent/agent.py"]


@pytest.mark.parametrize("rel", FALLBACK_FILES)
def test_no_fallback_chain_falls_back_to_itself(rel):
    """`get("X-Flashback-Path") or get("X-Flashback-Path")` is a no-op that reads
    like compatibility code. Both of this change's two-name fallbacks were
    written correctly and then flattened by a blanket rename; neither failed any
    other test, because the new name worked and nothing exercised the old one.
    """
    tree = ast.parse((REPO / rel).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            keys = [v.args[0].value for v in node.values
                    if isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute)
                    and v.func.attr == "get" and v.args
                    and isinstance(v.args[0], ast.Constant)
                    and isinstance(v.args[0].value, str)]
            assert len(keys) == len(set(keys)), \
                f"{rel}:{node.lineno} falls back to the same key twice: {keys}"
        if isinstance(node, (ast.Tuple, ast.List)):
            # Compared by ast.unparse, because the fallbacks here are f-strings
            # — f"X-Flashback-{suffix}" is a JoinedStr with no .value at all.
            #
            # The first version of this filter ended with
            #     not isinstance(getattr(e, "value", None), (..., type(None)))
            # meaning to skip numeric tuples. getattr on a JoinedStr returns the
            # None default, which that clause then treats as "skip" — so it
            # discarded every f-string, looked at the one genuinely collapsed
            # pair in this repo, and reported it clean. JoinedStr and Constant
            # are handled apart now so neither case can mask the other.
            lits = [ast.unparse(e) for e in node.elts
                    if isinstance(e, ast.JoinedStr)
                    or (isinstance(e, ast.Constant) and isinstance(e.value, str))]
            if len(lits) > 1:
                assert len(lits) == len(set(lits)), \
                    f"{rel}:{node.lineno} lists the same name twice: {lits}"


@pytest.mark.parametrize("rel", FALLBACK_FILES)
def test_no_dict_literal_repeats_a_key(rel):
    """Python collapses a duplicated dict key in silence. The restore relay was
    written with X-Flashback-* and X-Flashback-*, the rename made both
    X-Flashback-*, and the legacy header simply stopped being sent — with no
    syntax error, no warning and no failing test."""
    tree = ast.parse((REPO / rel).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = [k.value for k in node.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)]
            assert len(keys) == len(set(keys)), \
                f"{rel}:{node.lineno} repeats a dict key: " \
                f"{sorted(k for k in keys if keys.count(k) > 1)}"


# ---- what must NOT have moved --------------------------------------------

def test_the_version_store_volume_is_still_flashback_data():
    """sysiblectl lists the volumes it must never destroy. flashback-data is
    Flashback's store: every version of every config file on every host, and the
    only thing in the stack that cannot be rebuilt from anything else."""
    src = (REPO / "deploy" / "sysiblectl").read_text()
    # Several case arms start with `slop)`; the one that matters is the volume
    # list, identified by a volume only it mentions.
    line = [l for l in src.splitlines()
            if l.strip().startswith("slop)") and "slop-caddy-data" in l]
    assert len(line) == 1, f"could not find the slop volume list (got {len(line)})"
    assert "flashback-data" in line[0], \
        "the version store is no longer in the list of volumes to preserve"


def test_the_stale_container_is_dealt_with_before_the_new_one_starts():
    """Renaming a compose service does not rename its container. The old one
    keeps running, unmanaged, still holding the agent port on the docker bridge,
    and the new one then cannot bind it and dies — so an update that changed
    nothing but a name takes config backup down."""
    src = (REPO / "deploy" / "sysiblectl").read_text()
    lines = src.splitlines()

    # The ASSIGNMENT, matched as a line, not "--remove-orphans" somewhere in a
    # block of text. Two earlier versions of this test passed with the flag
    # deleted: the first matched the comment that explains it, the second
    # stripped comments and then matched the _warn message that mentions it.
    # The flag appears three times in this file and only one of them is code.
    assigns = [n for n, l in enumerate(lines)
               if re.match(r"\s*up_extra\+=\(.*--remove-orphans.*\)\s*$", l)]
    assert assigns, "nothing adds --remove-orphans to the recreate"

    passes = [n for n, l in enumerate(lines)
              if "_compose up -d --build" in l and "up_extra" in l]
    assert passes, "up_extra is built but never passed to compose"
    assert assigns[0] < passes[0], \
        "the orphan flag is set after the recreate, which is too late"

    guard = "\n".join(lines[max(0, assigns[0] - 3):passes[0]])
    assert "sysible-baseline" in guard, (
        "nothing looks for the container the two-day rename left behind — "
        "sysible-baseline holds Flashback's agent port, so sysible-flashback "
        "cannot bind it and dies on startup")


def test_status_finds_the_container_under_either_name():
    """A stack not yet recreated is still running `sysible-flashback`. Reporting
    "not running" for a container that is serving perfectly sends an operator
    chasing an outage that is not happening."""
    src = (REPO / "deploy" / "sysiblectl").read_text()
    i = src.index("_flashback_status() {")
    block = src[i:i + 1200]
    assert "sysible-flashback" in block and "sysible-flashback" in block
    assert "SYSIBLE_FLASHBACK_CONTAINER" in block, \
        "an operator's pinned container name stops being read"


def test_the_reachability_probe_does_not_reread_the_variable_inside_the_container():
    """The probe runs `python -c` INSIDE the controller container. Reading
    os.environ['SYSIBLE_FLASHBACK_URL'] there raises KeyError on a Controller that
    has not been re-seeded since the rebrand, and the check reports UNREACHABLE
    for a chain that works — the false alarm, not the outage."""
    src = (REPO / "deploy" / "sysiblectl").read_text()
    i = src.index("/api/health',timeout=5")
    block = src[i - 600:i + 200]
    # Comments stripped first. The rule is explained in a comment right above
    # the code, quoting the very string being banned — so a naive substring
    # search over this block matches the explanation and fails on correct code.
    code = "\n".join(l for l in block.splitlines() if not l.lstrip().startswith("#"))
    assert "os.environ['SYSIBLE_FLASHBACK_URL']" not in code, \
        "the probe re-reads the variable inside the container instead of being given it"
    assert "_FB_PROBE_URL" in code


def test_the_client_module_is_flashback_again():
    """backend/flashback.py, not backend/baseline.py — and nothing should be
    importing the rename-era name."""
    assert (REPO / "backend" / "flashback.py").exists()
    assert not (REPO / "backend" / "baseline.py").exists(), \
        "both module names exist; one of them is dead code nobody will notice"
    for rel in ["backend/app.py", "webgui/server.py", "host_agent/agent.py"]:
        src = (REPO / rel).read_text()
        assert not re.search(r"\bfrom backend import baseline\b", src), rel
        assert not re.search(r"\bbackend\.baseline\b", src), rel


def test_flashback_does_not_read_the_config_managers_settings(monkeypatch):
    """SYSIBLE_BASELINE_* belongs to Sysible Baseline now. A Controller relaying
    config snapshots to the wrong service's endpoint, with the wrong service's
    token, fails in a way nobody would think to look for."""
    for suffix in ("URL", "AGENT_TOKEN"):
        monkeypatch.delenv("SYSIBLE_FLASHBACK_" + suffix, raising=False)
        monkeypatch.setenv("SYSIBLE_BASELINE_" + suffix, "the-config-managers-value")
    m = _reload(monkeypatch)
    assert m.FLASHBACK_URL == "", "the Controller read the config manager's URL"
    assert m.AGENT_TOKEN == "", "the Controller read the config manager's token"
    assert m.configured() is False


def test_an_install_stranded_by_the_rename_is_reported(monkeypatch):
    """The two-day window. The SLOP installer migrates these back once; until
    it runs, config backup is off and the Controller should be able to say why
    rather than just reporting itself unconfigured."""
    monkeypatch.delenv("SYSIBLE_FLASHBACK_AGENT_TOKEN", raising=False)
    monkeypatch.setenv("SYSIBLE_BASELINE_AGENT_TOKEN", "t" * 64)
    m = _reload(monkeypatch)
    assert ("SYSIBLE_BASELINE_AGENT_TOKEN", "SYSIBLE_FLASHBACK_AGENT_TOKEN") \
        in m.stranded_names()


def test_a_clean_install_reports_nothing_stranded(monkeypatch):
    for suffix in ("URL", "AGENT_TOKEN", "TIMEOUT_S", "MAX_SNAPSHOT_BYTES"):
        monkeypatch.delenv("SYSIBLE_BASELINE_" + suffix, raising=False)
    assert _reload(monkeypatch).stranded_names() == []


# ---- "baseline" is also an ordinary word in this codebase ------------------

def test_the_agent_integrity_baseline_is_not_a_product_name():
    """`rebaseline`, `sealed baseline`, `compare(reported, baseline)` — the
    agent-integrity module seals a reference manifest per host and calls it a
    baseline. That has nothing to do with either product.

    The reverse rename in this change clobbered it: `rebaseline` became
    `reflashback` in every caller while agent_integrity.py itself kept the real
    name, and the whole enrollment path died with AttributeError. Found by the
    test suite, not by reading the diff — which is the point. A product called
    Baseline makes this a permanent hazard for anyone doing a find-and-replace
    here, so it is pinned.
    """
    import backend.agent_integrity as ai
    assert hasattr(ai, "rebaseline"), \
        "agent_integrity.rebaseline was renamed — it is an integrity term, not a brand"
    for rel in ["backend/app.py", "webgui/server.py"]:
        src = (REPO / rel).read_text()
        assert "reflashback" not in src, \
            f"{rel} calls reflashback(); the integrity function is rebaseline()"


def test_both_product_names_can_coexist_with_it():
    """Flashback, Sysible Baseline and the integrity baseline are three
    different things using two words between them. Nothing may assume that
    every occurrence of either word is a brand."""
    src = (REPO / "backend" / "agent_integrity.py").read_text()
    assert "flashback" not in src.lower(), \
        "the integrity module picked up a product name"
    assert "SYSIBLE_BASELINE" not in src, \
        "the integrity module picked up the config manager's settings"
