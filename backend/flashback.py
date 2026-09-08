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

# Empty URL or token = Flashback integration is off. Both are seeded by the SLOP
# installer into the Controller's own .env (see install.sh), so a stock platform
# install has them; a standalone Controller has neither and simply never offers
# config backup.
FLASHBACK_URL = (os.getenv("SYSIBLE_FLASHBACK_URL", "") or "").rstrip("/")
AGENT_TOKEN = os.getenv("SYSIBLE_FLASHBACK_AGENT_TOKEN", "") or ""
TIMEOUT_S = float(os.getenv("SYSIBLE_FLASHBACK_TIMEOUT_S", "20"))

# Ceiling on one snapshot body, mirrored by the agent so it trims before sending.
#
# This MUST stay below the controller's global request ceiling
# (_max_request_bytes in app.py, 16 MiB): that middleware rejects an over-large
# body before any route sees it, with a flat "Request body too large." At equal
# values the generic message always won, and an operator whose /etc had outgrown
# the cap got no hint about which knob to turn. Below it, the relay's own 413
# names SYSIBLE_D3LOREAN_PATHS instead. tests/test_flashback_relay.py pins the
# ordering so the two cannot drift together.
MAX_SNAPSHOT_BYTES = int(os.getenv("SYSIBLE_FLASHBACK_MAX_SNAPSHOT_BYTES", str(12 * 1024 * 1024)))


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
    X-Flashback-* headers; the agent verifies the digest before writing."""
    r = _call("GET", f"/api/agent/restores/{int(restore_id)}/payload",
              params={"host_id": host_id})
    return {
        "path": r.headers.get("X-Flashback-Path", ""),
        "sha256": r.headers.get("X-Flashback-Sha256", ""),
    }, r.content


def ack_restore(host_id: str, restore_id: int, ok: bool = True) -> dict:
    r = _call("POST", f"/api/agent/restores/{int(restore_id)}/ack",
              params={"host_id": host_id}, json={"ok": bool(ok)})
    try:
        return r.json()
    except Exception:
        return {"ok": bool(ok)}
