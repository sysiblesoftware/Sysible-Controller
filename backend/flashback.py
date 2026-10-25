"""Controller -> Sysible Flashback client (config snapshot ingest + restore).

WHY THE CONTROLLER IS IN THE MIDDLE. Flashback's agent API is authenticated by a
single bearer token, and its endpoints take `host_id` from the caller. Handing
that token to every managed host would mean any one compromised host could read
every other host's stored configuration and queue a restore that overwrites a
file on it. So hosts never talk to Flashback at all: an agent posts its snapshot
to the Controller over the channel it already has (its own agent_secret, TLS-
pinned, outbound-only, works behind NAT), and the Controller — which has just
authenticated that agent — stamps the host_id itself and relays. The token lives
in exactly one place, and a host cannot name another host.

Reachability: Flashback publishes its API on LOOPBACK on the SLOP host
(127.0.0.1:8770 by default, see the platform's docker-compose.yml), so this adds
no public surface and needs no hole in the gateway's SSO. SYSIBLE_FLASHBACK_URL
overrides it when the Controller and the SLOP stack are on different machines.

Nothing here raises for a Flashback that is down or unconfigured: the caller gets
a typed failure it can turn into an HTTP status, so a broken Flashback degrades
config backup rather than breaking agent check-ins.
"""
from __future__ import annotations

import os

import requests


def _env(suffix: str, default: str = "") -> str:
    """One setting, under Flashback's own name only.

    Flashback was briefly renamed Sysible Baseline and these were migrated to
    SYSIBLE_BASELINE_*. That prefix now belongs to a DIFFERENT product — the
    Salt-driven config manager — so it is deliberately NOT read as a fallback
    here. AGENT_TOKEN and URL are names both products want, and a Controller
    relaying config snapshots to the wrong service's endpoint with the wrong
    service's token fails in a way nobody would think to look for.

    An install stranded by the two-day rename is recovered by the SLOP
    installer, which migrates those keys back once. `stranded_names()` reports
    anything it missed.

    `None` (unset) rather than falsy: an explicitly empty token means config
    backup is OFF on purpose and must not inherit a default.
    """
    val = os.getenv(f"SYSIBLE_FLASHBACK_{suffix}")
    return default if val is None else val


# Settings Flashback owns, used to spot leftovers from the two-day rename.
_OWNED = ("URL", "AGENT_TOKEN", "TIMEOUT_S", "MAX_SNAPSHOT_BYTES")


def stranded_names() -> list[tuple[str, str]]:
    """(stranded, correct) for settings still under the short-lived
    SYSIBLE_BASELINE_* spelling and not set under Flashback's own name.

    Advisory only. A name here may equally belong to the config manager, which
    legitimately owns that prefix now — which is exactly why it cannot simply
    be read as a fallback.
    """
    return sorted(
        (f"SYSIBLE_BASELINE_{k}", f"SYSIBLE_FLASHBACK_{k}")
        for k in _OWNED
        if os.getenv(f"SYSIBLE_BASELINE_{k}") is not None
        and os.getenv(f"SYSIBLE_FLASHBACK_{k}") is None
    )


# Empty URL or token = Flashback integration is off. Both are seeded by the SLOP
# installer into the Controller's own .env (see install.sh), so a stock platform
# install has them; a standalone Controller has neither and simply never offers
# config backup.
FLASHBACK_URL = (_env("URL", "") or "").rstrip("/")
AGENT_TOKEN = _env("AGENT_TOKEN", "") or ""
TIMEOUT_S = float(_env("TIMEOUT_S", "20"))

# Ceiling on one snapshot body, mirrored by the agent so it trims before sending.
#
# This MUST stay below the controller's global request ceiling
# (_max_request_bytes in app.py, 16 MiB): that middleware rejects an over-large
# body before any route sees it, with a flat "Request body too large." At equal
# values the generic message always won, and an operator whose /etc had outgrown
# the cap got no hint about which knob to turn. Below it, the relay's own 413
# names SYSIBLE_FLASHBACK_PATHS instead. tests/test_flashback_relay.py pins the
# ordering so the two cannot drift together.
MAX_SNAPSHOT_BYTES = int(_env("MAX_SNAPSHOT_BYTES", str(12 * 1024 * 1024)))


class FlashbackUnavailable(RuntimeError):
    """Flashback is not configured, or could not be reached//understood. Carries
    an operator-facing reason; never contains the token."""

    def __init__(self, reason: str, status: int = 503):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def configured() -> bool:
    return bool(FLASHBACK_URL and AGENT_TOKEN)


def _require_configured() -> None:
    if FLASHBACK_URL and AGENT_TOKEN:
        return
    # Name the CURRENT spelling. An operator told to set a variable they have
    # never heard of goes looking for it in the manual, which is the right
    # place for them to end up.
    missing = []
    if not FLASHBACK_URL:
        missing.append("SYSIBLE_FLASHBACK_URL")
    if not AGENT_TOKEN:
        missing.append("SYSIBLE_FLASHBACK_AGENT_TOKEN")
    raise FlashbackUnavailable(
        "Config backup is not configured on this controller (" + " and ".join(missing)
        + " unset). Run the SLOP installer, or set them in the controller's .env.")


def _headers() -> dict:
    return {"Authorization": f"Bearer {AGENT_TOKEN}", "Accept": "application/json"}


def _call(method: str, path: str, **kw):
    _require_configured()
    url = f"{FLASHBACK_URL}{path}"
    try:
        r = requests.request(method, url, headers=_headers(), timeout=TIMEOUT_S, **kw)
    except requests.RequestException as e:
        # Never echo the URL's credentials or the token — only the failure shape.
        raise FlashbackUnavailable(f"Flashback is unreachable ({type(e).__name__}).") from None
    if r.status_code in (401, 403):
        raise FlashbackUnavailable(
            "Flashback rejected this controller's agent token — the two halves do "
            "not match. Re-run the SLOP installer so both get the same value.",
            status=502)
    if r.status_code == 503:
        raise FlashbackUnavailable(_detail(r) or "Flashback is not accepting snapshots.", status=502)
    if r.status_code >= 400:
        raise FlashbackUnavailable(f"Flashback returned HTTP {r.status_code}.", status=502)
    return r


def _detail(r) -> str:
    try:
        return str((r.json() or {}).get("detail") or "")
    except Exception:
        return ""


def post_snapshot(host_id: str, label: str, files: list) -> dict:
    """Relay one snapshot. `files` is [{path, content_b64}]; host_id is the one
    the Controller authenticated, never one the agent chose."""
    r = _call("POST", "/api/agent/snapshot",
              json={"host_id": host_id, "label": label or host_id, "files": files or []})
    try:
        return r.json()
    except Exception:
        raise FlashbackUnavailable("Flashback returned a malformed snapshot reply.", status=502) from None


def pending_restores(host_id: str) -> list:
    r = _call("GET", "/api/agent/restores", params={"host_id": host_id})
    try:
        out = r.json()
    except Exception:
        raise FlashbackUnavailable("Flashback returned a malformed restore list.", status=502) from None
    return out if isinstance(out, list) else []


def restore_payload(host_id: str, restore_id: int):
    """(headers, bytes) for one queued restore. The path and sha256 ride in
    X-Flashback-* headers; the agent verifies the digest before writing.

    Either spelling is accepted. A Flashback that has not been updated past the
    rebrand still sends X-Flashback-*, and losing the path and digest here does
    not fail loudly — it hands the agent a restore it cannot verify, which it
    then either refuses or applies to the wrong file.
    """
    r = _call("GET", f"/api/agent/restores/{int(restore_id)}/payload",
              params={"host_id": host_id})

    def _hdr(suffix: str) -> str:
        # One name. Flashback has sent X-Flashback-* for its whole life; the
        # two days it was called Baseline it sent X-Baseline-* alongside, never
        # instead, so reading the original name covers every build.
        return r.headers.get(f"X-Flashback-{suffix}") or ""

    return {"path": _hdr("Path"), "sha256": _hdr("Sha256")}, r.content


def ack_restore(host_id: str, restore_id: int, ok: bool = True) -> dict:
    r = _call("POST", f"/api/agent/restores/{int(restore_id)}/ack",
              params={"host_id": host_id}, json={"ok": bool(ok)})
    try:
        return r.json()
    except Exception:
        return {"ok": bool(ok)}
