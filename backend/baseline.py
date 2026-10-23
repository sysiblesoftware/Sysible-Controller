"""Controller -> Sysible Baseline client (config snapshot ingest + restore).

WHY THE CONTROLLER IS IN THE MIDDLE. Baseline's agent API is authenticated by a
single bearer token, and its endpoints take `host_id` from the caller. Handing
that token to every managed host would mean any one compromised host could read
every other host's stored configuration and queue a restore that overwrites a
file on it. So hosts never talk to Baseline at all: an agent posts its snapshot
to the Controller over the channel it already has (its own agent_secret, TLS-
pinned, outbound-only, works behind NAT), and the Controller — which has just
authenticated that agent — stamps the host_id itself and relays. The token lives
in exactly one place, and a host cannot name another host.

Reachability: Baseline publishes its API on LOOPBACK on the SLOP host
(127.0.0.1:8770 by default, see the platform's docker-compose.yml), so this adds
no public surface and needs no hole in the gateway's SSO. SYSIBLE_BASELINE_URL
overrides it when the Controller and the SLOP stack are on different machines.

Nothing here raises for a Baseline that is down or unconfigured: the caller gets
a typed failure it can turn into an HTTP status, so a broken Baseline degrades
config backup rather than breaking agent check-ins.
"""
from __future__ import annotations

import os

import requests


def _env(suffix: str, default: str = "") -> str:
    """One setting, under its current name or the one it had before the rebrand.

    Baseline was Sysible Baseline, and the SLOP installer seeds these into the
    Controller's .env. Controller and SLOP are separate deployments on separate
    schedules, so a Controller updated first will be reading an .env that still
    says FLASHBACK — and if that stopped resolving, config backup would switch
    itself off fleet-wide with both halves looking correctly configured.

    New name first, so an operator who HAS migrated is not overridden by a stale
    line left in the same file. `None` (unset) rather than falsy, because an
    explicitly empty token means "off" on purpose.
    """
    for name in (f"SYSIBLE_BASELINE_{suffix}", f"SYSIBLE_FLASHBACK_{suffix}"):
        val = os.getenv(name)
        if val is not None:
            return val
    return default


# Empty URL or token = Baseline integration is off. Both are seeded by the SLOP
# installer into the Controller's own .env (see install.sh), so a stock platform
# install has them; a standalone Controller has neither and simply never offers
# config backup.
BASELINE_URL = (_env("URL", "") or "").rstrip("/")
AGENT_TOKEN = _env("AGENT_TOKEN", "") or ""
TIMEOUT_S = float(_env("TIMEOUT_S", "20"))

# Ceiling on one snapshot body, mirrored by the agent so it trims before sending.
#
# This MUST stay below the controller's global request ceiling
# (_max_request_bytes in app.py, 16 MiB): that middleware rejects an over-large
# body before any route sees it, with a flat "Request body too large." At equal
# values the generic message always won, and an operator whose /etc had outgrown
# the cap got no hint about which knob to turn. Below it, the relay's own 413
# names SYSIBLE_BASELINE_PATHS instead. tests/test_baseline_relay.py pins the
# ordering so the two cannot drift together.
MAX_SNAPSHOT_BYTES = int(_env("MAX_SNAPSHOT_BYTES", str(12 * 1024 * 1024)))


class BaselineUnavailable(RuntimeError):
    """Baseline is not configured, or could not be reached//understood. Carries
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
    # Name the CURRENT spelling. An operator told to set a variable they have
    # never heard of goes looking for it in the manual, which is the right
    # place for them to end up.
    missing = []
    if not BASELINE_URL:
        missing.append("SYSIBLE_BASELINE_URL")
    if not AGENT_TOKEN:
        missing.append("SYSIBLE_BASELINE_AGENT_TOKEN")
    raise BaselineUnavailable(
        "Config backup is not configured on this controller (" + " and ".join(missing)
        + " unset). Run the SLOP installer, or set them in the controller's .env.")


def _headers() -> dict:
    return {"Authorization": f"Bearer {AGENT_TOKEN}", "Accept": "application/json"}


def _call(method: str, path: str, **kw):
    _require_configured()
    url = f"{BASELINE_URL}{path}"
    try:
        r = requests.request(method, url, headers=_headers(), timeout=TIMEOUT_S, **kw)
    except requests.RequestException as e:
        # Never echo the URL's credentials or the token — only the failure shape.
        raise BaselineUnavailable(f"Baseline is unreachable ({type(e).__name__}).") from None
    if r.status_code in (401, 403):
        raise BaselineUnavailable(
            "Baseline rejected this controller's agent token — the two halves do "
            "not match. Re-run the SLOP installer so both get the same value.",
            status=502)
    if r.status_code == 503:
        raise BaselineUnavailable(_detail(r) or "Baseline is not accepting snapshots.", status=502)
    if r.status_code >= 400:
        raise BaselineUnavailable(f"Baseline returned HTTP {r.status_code}.", status=502)
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
        raise BaselineUnavailable("Baseline returned a malformed snapshot reply.", status=502) from None


def pending_restores(host_id: str) -> list:
    r = _call("GET", "/api/agent/restores", params={"host_id": host_id})
    try:
        out = r.json()
    except Exception:
        raise BaselineUnavailable("Baseline returned a malformed restore list.", status=502) from None
    return out if isinstance(out, list) else []


def restore_payload(host_id: str, restore_id: int):
    """(headers, bytes) for one queued restore. The path and sha256 ride in
    X-Baseline-* headers; the agent verifies the digest before writing.

    Either spelling is accepted. A Baseline that has not been updated past the
    rebrand still sends X-Baseline-*, and losing the path and digest here does
    not fail loudly — it hands the agent a restore it cannot verify, which it
    then either refuses or applies to the wrong file.
    """
    r = _call("GET", f"/api/agent/restores/{int(restore_id)}/payload",
              params={"host_id": host_id})

    def _hdr(suffix: str) -> str:
        for name in (f"X-Baseline-{suffix}", f"X-Flashback-{suffix}"):
            val = r.headers.get(name)
            if val:
                return val
        return ""
    return {"path": _hdr("Path"), "sha256": _hdr("Sha256")}, r.content


def ack_restore(host_id: str, restore_id: int, ok: bool = True) -> dict:
    r = _call("POST", f"/api/agent/restores/{int(restore_id)}/ack",
              params={"host_id": host_id}, json={"ok": bool(ok)})
    try:
        return r.json()
    except Exception:
        return {"ok": bool(ok)}
