"""Controller -> Sysible Baseline client (desired-state configuration).

WHY THE CONTROLLER IS IN THE MIDDLE, again. Exactly the reasoning behind
backend/flashback.py, and for the same reason: the Controller already holds the
only authenticated, outbound-only, NAT-friendly channel to every enrolled host,
and it is the only component trusted to say which host is which. Giving Baseline
its own fleet credentials would double the blast radius of a compromise and buy
nothing — so Baseline never talks to a host, and this module is the whole of the
path between them.

One cycle, per host:

    1. ask Baseline what this host must look like    -> a list of salt-call commands
    2. run each on the host through the agent channel
    3. post the output back                          -> Baseline parses it and
                                                        says which rules fired
    4. carry out those actions, because we are the one with the channel

STEP 4 IS WHERE THE DANGER IS, and it is deliberately narrow. Baseline returns
actions, not commands to trust blindly: a `command` action is a shell string an
operator typed into a rule, and it runs as root on a host. It is therefore
dispatched through exactly the same path as any other operator-initiated command
— attributed, logged, and refused if the rule's author could not have run it
themselves. `capture` and `reapply` are verbs this module implements; nothing
else is executed at all.

Nothing here raises for a Baseline that is down or unconfigured: the caller gets
a typed failure it can turn into an HTTP status, so a broken config manager
degrades configuration management rather than breaking agent check-ins.
"""

from __future__ import annotations

import os

import requests


def _env(suffix: str, default: str = "") -> str:
    """One setting, under Baseline's own name only.

    Baseline owns SYSIBLE_BASELINE_*. Sysible Flashback carried that prefix for
    two days during a rename and deliberately no longer reads it — AGENT_TOKEN
    and URL are names both products want, and the Controller posting a config
    snapshot to the config manager (or a desired-state request to the backup
    tool) fails in a way nobody would think to look for.
    """
    val = os.getenv(f"SYSIBLE_BASELINE_{suffix}")
    return default if val is None else val


BASELINE_URL = (_env("URL", "") or "").rstrip("/")
AGENT_TOKEN = _env("AGENT_TOKEN", "") or ""
TIMEOUT_S = float(_env("TIMEOUT_S", "20"))

# What one host's salt-call output may be. A state run prints per-state comments
# and, on failure, diffs; 4 MiB is generous for that and far below the
# controller's global request ceiling.
MAX_RESULT_BYTES = int(_env("MAX_RESULT_BYTES", str(4 * 1024 * 1024)))

# Actions this module is willing to carry out. An action Baseline invents, or
# one added to its vocabulary before the Controller learns it, is REPORTED and
# not executed — silently ignoring it would make a rule an operator believes is
# protecting them do nothing.
KNOWN_ACTIONS = ("notify", "command", "capture", "reapply")


class BaselineUnavailable(RuntimeError):
    """Baseline is not configured, or could not be reached/understood. Carries
    an operator-facing reason; never contains the token."""

    def __init__(self, reason: str, status: int = 503):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def configured() -> bool:
    return bool(BASELINE_URL and AGENT_TOKEN)


def _require_configured() -> None:
    if BASELINE_URL and AGENT_TOKEN:
        return
    missing = []
    if not BASELINE_URL:
        missing.append("SYSIBLE_BASELINE_URL")
    if not AGENT_TOKEN:
        missing.append("SYSIBLE_BASELINE_AGENT_TOKEN")
    raise BaselineUnavailable(
        "Desired-state configuration is not configured on this controller ("
        + " and ".join(missing) + " unset). Run the SLOP installer, or set them "
        "in the controller's .env.")


def _call(method: str, path: str, **kw):
    _require_configured()
    try:
        r = requests.request(method, f"{BASELINE_URL}{path}",
                             headers={"Authorization": f"Bearer {AGENT_TOKEN}",
                                      "Accept": "application/json"},
                             timeout=TIMEOUT_S, **kw)
    except requests.RequestException as e:
        # Never echo the URL's credentials or the token — only the failure shape.
        raise BaselineUnavailable(f"Baseline is unreachable ({type(e).__name__}).") from None
    if r.status_code in (401, 403):
        raise BaselineUnavailable(
            "Baseline rejected this controller's agent token — the two halves do "
            "not match. Re-run the SLOP installer so both get the same value.",
            status=502)
    if r.status_code == 503:
        raise BaselineUnavailable(_detail(r) or "Baseline is not accepting requests.",
                                  status=502)
    if r.status_code >= 400:
        raise BaselineUnavailable(f"Baseline returned HTTP {r.status_code}.", status=502)
    return r


def _detail(r) -> str:
    try:
        return str((r.json() or {}).get("detail") or "")
    except Exception:
        return ""


def desired(host_id: str, environment: str = "", preview: bool = False) -> list:
    """What this host must look like: [{run_id, state, version, preview, command}].

    An empty list means the host has no state assigned — NOT that it is
    compliant. A host nobody declared anything for is simply not managed, and
    the console has to be able to tell those apart.
    """
    r = _call("GET", "/api/agent/desired",
              params={"host_id": host_id, "environment": environment or "",
                      "preview": "true" if preview else "false"})
    try:
        return (r.json() or {}).get("apply") or []
    except Exception:
        raise BaselineUnavailable("Baseline returned a malformed desired-state reply.",
                                  status=502) from None


def report(run_id: int, output: str, preview: bool = False) -> dict:
    """Hand one salt-call result back. Returns Baseline's parse plus the actions
    its rules fired, for the caller to carry out."""
    if output and len(output) > MAX_RESULT_BYTES:
        # Truncate rather than refuse: a huge return is usually one state
        # printing a whole file, and losing the run entirely would hide whatever
        # else happened in it. Say so in-band so the console shows it.
        output = output[:MAX_RESULT_BYTES] + "\n[controller] output truncated\n"
    r = _call("POST", "/api/agent/result",
              json={"run_id": int(run_id), "output": output or "", "preview": bool(preview)})
    try:
        return r.json() or {}
    except Exception:
        raise BaselineUnavailable("Baseline returned a malformed result reply.",
                                  status=502) from None


def unknown_actions(actions: list) -> list:
    """Actions this Controller does not know how to carry out.

    Baseline and the Controller update independently, so a rule using an action
    added to Baseline's vocabulary first would otherwise be silently dropped by
    an older Controller — a rule that reads as active in one console and does
    nothing in the other.
    """
    return sorted({(a.get("action") or "").strip().lower() for a in (actions or [])}
                  - set(KNOWN_ACTIONS) - {""})
