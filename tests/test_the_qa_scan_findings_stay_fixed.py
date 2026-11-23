"""Guards for a sweep that looked for two shapes, not two bugs.

    * a failure rendered as a clean result
    * a capability, or a surface, that no interface reaches

Three of the findings below were one-line fixes. They are guarded anyway,
because each was found by a scan and a one-line fix is exactly what gets undone
by the next person who reads the line without the reason. Two of the scans are
kept here as tests, so the CLASS is closed rather than the instance.
"""
import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
SERVER = (REPO / "webgui" / "server.py").read_text(encoding="utf-8")


# ---- "could not check" is not "checked and fine" ---------------------------
def test_an_uninspectable_vault_is_not_called_safe(monkeypatch):
    """True means "no warning". An exception used to assert that a vault nobody
    could inspect was safe, and the operator carried away a sanitized bundle
    that may hold plaintext sudo passwords."""
    from backend import secret_vault

    def boom():
        raise RuntimeError("no crypto library on this host")
    monkeypatch.setattr(secret_vault, "_fernet", boom)
    assert secret_vault.is_encrypting_at_rest() is False


def test_the_bundle_warning_does_not_assert_a_cause_it_never_established():
    """It is reached BOTH from the degraded mode and from "could not look". The
    old text named only the first, sending an operator to check a setting that
    may not be the problem."""
    src = (REPO / "tools" / "backup_lib.py").read_text(encoding="utf-8")
    block = src[src.index('manifest["warnings"].append'):][:700]
    assert "NOT CONFIRMED" in block, "the warning still asserts a specific cause"
    assert "PLAINTEXT" in block, "it no longer states the consequence"


# ---- surfaces nothing reaches ----------------------------------------------
def test_the_orphaned_checkin_endpoint_is_gone():
    """No client called it, it duplicated the Ping action, it sat behind
    require_operator, and it was the only fleet operation in the file that ran
    serially -- sixteen hosts could hold a request thread for minutes while the
    browser gave up at sixty seconds."""
    assert '@app.post("/api/checkin")' not in SERVER
    assert "class CheckinRequest" not in SERVER


def test_no_comment_sends_the_operator_to_a_control_that_does_not_exist():
    """The hint for SSH-only hosts read "use Check In / Ping" -- half of which
    named an endpoint no interface had ever offered."""
    assert "Check In / Ping" not in SERVER


def test_every_api_route_has_a_caller():
    """THE SCAN, kept as a test. A route no client reaches is either dead weight
    on an authenticated surface or a capability the operator cannot get to.

    The allowlist is for routes whose caller is deliberately not a browser. Each
    entry has to say who calls it, because "add it to the list" is how this test
    would otherwise be made to pass.
    """
    NON_BROWSER = {
        "/api/auth/verify": "the gateway's forward-auth check (see test_sso_gateway_trust)",
        "/api/docs/download": "the download control inside the manual the rail opens",
        "/api/controller-key": "the enrollment path; the BFF proxies /remote/controller-key",
    }
    client = ""
    for d in ("webgui/frontend/src", "client", "webgui"):
        for f in (REPO / d).rglob("*"):
            if (f.is_file() and f.suffix in (".js", ".jsx", ".py", ".html")
                    and "node_modules" not in str(f) and "/dist/" not in str(f)
                    and f.name != "server.py"):
                client += f.read_text(encoding="utf-8", errors="ignore")
    orphans = []
    for m in re.finditer(r'@app\.(get|post|put|delete|patch|websocket)\(\s*"(/api[^"]*)"', SERVER):
        path = m.group(2)
        stem = re.sub(r"\{[^}]*\}.*$", "", path).rstrip("/")
        if stem in client or path in NON_BROWSER:
            continue
        orphans.append(f"{m.group(1).upper()} {path} (server.py:{SERVER[:m.start()].count(chr(10))+1})")
    assert not orphans, (
        "routes no client reaches — wire them up, delete them, or name the "
        "non-browser caller:\n  " + "\n  ".join(orphans))


# ---- one token, one value --------------------------------------------------
def test_no_css_token_is_defined_twice_in_one_block():
    """THE OTHER SCAN, kept as a test. The second definition wins silently, so
    the first is dead and anyone reading it is told the wrong colour. --green
    was #2ea043 and #36b37e in the same block.

    Scoped per block on purpose: a light skin redefining a token is the
    mechanism working, not a duplicate.
    """
    css = (REPO / "webgui" / "frontend" / "src" / "styles.css").read_text(encoding="utf-8")
    dupes = []
    for m in re.finditer(r"([^{}]*)\{([^{}]*)\}", css):
        sel = (m.group(1).strip().splitlines() or ["?"])[-1].strip()
        seen = {}
        for d in re.finditer(r"(--[A-Za-z0-9_-]+)\s*:\s*([^;]+);", m.group(2)):
            name, val = d.group(1), d.group(2).strip()
            if name in seen and seen[name] != val:
                dupes.append(f"{sel}: {name} = {seen[name]} then {val}")
            seen[name] = val
    assert not dupes, "tokens defined twice in one block:\n  " + "\n  ".join(dupes)
