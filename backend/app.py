from typing import Optional

from fastapi import Body, Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
import json
import os
import secrets
import threading
import time

from backend.agent_bundle import mint_agent_bundle, detect_local_ips, resolve_controller_addresses
from backend.auth import require_api_key, require_superuser, require_activity_viewer, acting_admin_name
from backend.db import (
    create_enroll_token,
    create_reissue_token,
    enroll_token_authorizes_rebind,
    validate_enroll_token,
    resolve_enroll_token_host,
    consume_enroll_token,
    create_or_update_agent,
    update_agent_heartbeat,
    insert_metric_sample,
    get_metric_samples,
    upsert_host_snapshot,
    get_host_snapshot,
    upsert_host_health,
    get_all_host_health,
    list_agents,
    delete_agent,
    agent_secret_matches,
    revoke_agent,
    is_agent_revoked,
    agent_exists,
    queue_task,
    fetch_pending_tasks,
    submit_task_result,
    reclaim_stale_tasks,
    get_task_kind,
    get_task_host,
    list_results,
    list_environments,
    create_environment,
    delete_environment,
    set_agent_environment,
    list_environment_sudo_defaults,
    set_environment_sudo_default,
    get_controller_config,
    set_controller_config,
    get_license_config,
    set_license_config,
    get_portal_config,
    set_portal_port,
    get_portal_credentials,
    set_portal_credentials,
    delete_portal_credentials,
    get_portal_login_history,
    get_last_portal_login,
    list_portal_sessions,
    delete_portal_session,
    delete_all_portal_sessions,
    log_portal_event,
    list_administrators,
    count_administrators,
    get_administrator,
    add_administrator,
    remove_administrator,
    set_administrator_sudo_connect,
    set_administrator_role,
    count_administrators_by_role,
    update_administrator_password,
    update_administrator_username,
    record_administrator_login,
    log_admin_audit,
    get_admin_audit_log,
    get_environmental_policy,
    set_environmental_policy,
    get_admin_password_policy,
    set_admin_password_policy,
    create_admin_token,
    resolve_admin_token,
    delete_admin_token,
    delete_admin_tokens_for_user,
    log_activity,
    get_activity_log,
    get_agent_hostname,
    set_agent_sudo_password_required,
)
from backend import portal_auth, portal_files, portal_manager, tls_manager
from backend.policy import validate_password_against_policy
from backend.models.agent_models import (
    ActivityLogRequest,
    EnrollRequest,
    HeartbeatRequest,
    PtyOutputRequest,
    SelfDisenrollRequest,
    TaskCreateRequest,
    TaskResultRequest,
)
from backend.models.environment_models import (
    CreateEnvironmentRequest,
    SetEnvironmentRequest,
)
from backend.models.portal_models import (
    SetControllerConfigRequest,
    SetLicenseKeyRequest,
    SetPortalCredentialsRequest,
    RemovePortalCredentialsRequest,
    SetPortalPortRequest,
    AdminLoginRequest,
    AdminSetupRequest,
    ChangeAdminCredentialsRequest,
    AddAdministratorRequest,
    ForcePasswordChangeRequest,
    ResetAdministratorPasswordRequest,
    SetSudoConnectRequest,
    SetRoleRequest,
)
from backend.models.policy_models import (
    SetEnvironmentalPolicyRequest,
    SetAdminPasswordPolicyRequest,
)
from backend.remote_routes import router as remote_router
from backend.remote_routes import (
    _ensure_controller_key,
    agent_ssh_enable_command,
    register_agent_ssh_host,
    forget_agent_ssh_host,
    sync_agent_ssh_environment,
    ssh_host_exists,
    get_agent_ssh_state,
    set_agent_ssh_state,
    AGENT_SSH_MARKER,
)

# docs_url/redoc_url disabled: the interactive Swagger/ReDoc consoles are
# an unauthenticated map of the whole API (and a ready-made request
# builder) for anyone who can reach the port. openapi_url is disabled too and
# re-served loopback-only below: the only consumer is `sysible_controller`'s
# readiness self-check, a local curl to 127.0.0.1 — so remote callers get 404
# instead of a full map of the control-plane API.
app = FastAPI(title="Sysible Controller", docs_url=None, redoc_url=None, openapi_url=None)


# ---------------------------------------------------------------------------
# Scrub a per-host agent secret from access logs. Current agents send the secret
# in the X-Agent-Secret header (never logged), but the poll/pty endpoints also
# accept it as a `?agent_secret=` query param for backward compatibility with
# older field agents — and a secret in the URL lands in the uvicorn access log
# (and any TLS-terminating proxy log) in cleartext. Redact it there so the
# compat fallback can't leak a bearer secret into logs.
# ---------------------------------------------------------------------------
import logging as _logging
import re as _re_log


class _RedactAgentSecretFilter(_logging.Filter):
    _PAT = _re_log.compile(r"(agent_secret=)[^&\s\"']+", _re_log.IGNORECASE)

    def filter(self, record):
        try:
            if record.args:
                args = tuple(
                    self._PAT.sub(r"\1[redacted]", a) if isinstance(a, str) and "agent_secret=" in a.lower() else a
                    for a in record.args
                )
                record.args = args
            if isinstance(record.msg, str) and "agent_secret=" in record.msg.lower():
                record.msg = self._PAT.sub(r"\1[redacted]", record.msg)
        except Exception:
            pass
        return True


_logging.getLogger("uvicorn.access").addFilter(_RedactAgentSecretFilter())


def _max_request_bytes() -> int:
    """Global request-body ceiling for the controller API. The high-volume agent
    endpoints (heartbeat snapshot, task result, metrics) otherwise accept an
    UNBOUNDED JSON body that Starlette buffers into RAM before validation, so a
    single authenticated/compromised agent could exhaust memory. 16 MiB is far above
    any legitimate heartbeat/result yet bounds the blast radius. Set
    SYSIBLE_MAX_REQUEST_BYTES=0 to disable."""
    try:
        v = int(os.getenv("SYSIBLE_MAX_REQUEST_BYTES", str(16 * 1024 * 1024)))
        return v if v >= 0 else 16 * 1024 * 1024
    except (TypeError, ValueError):
        return 16 * 1024 * 1024


# Ceiling for a single staged portal-download file (the multipart body bypasses the
# global JSON body limit above). Bounds the in-memory read in stage_portal_download_route.
try:
    _PORTAL_STAGE_MAX_BYTES = int(os.getenv("SYSIBLE_PORTAL_FILE_MAX_BYTES", str(100 * 1024 * 1024)))
except (TypeError, ValueError):
    _PORTAL_STAGE_MAX_BYTES = 100 * 1024 * 1024


class _BodyLimitMiddleware:
    """Reject an over-large request body BEFORE it is buffered into memory. Checks a
    declared Content-Length up front (the common case — `requests`/`httpx` always set
    it) and returns 413; for a chunked/mis-declared body it counts bytes as they
    stream and cuts the stream off once the cap is crossed, so memory stays bounded."""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = int(max_bytes)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or self.max_bytes <= 0:
            return await self.app(scope, receive, send)
        for k, v in scope.get("headers") or []:
            if k == b"content-length":
                try:
                    if int(v) > self.max_bytes:
                        return await self._too_large(send)
                except ValueError:
                    pass
                break
        seen = {"n": 0}
        cap = self.max_bytes

        async def _guarded_receive():
            message = await receive()
            if message.get("type") == "http.request":
                seen["n"] += len(message.get("body", b"") or b"")
                if seen["n"] > cap:
                    # Stop the app buffering more; the handler sees end-of-stream.
                    return {"type": "http.disconnect"}
            return message

        return await self.app(scope, _guarded_receive, send)

    async def _too_large(self, send):
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"),
                                (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body",
                    "body": b'{"detail":"Request body too large."}'})


app.add_middleware(_BodyLimitMiddleware, max_bytes=_max_request_bytes())


def _clamp_limit(limit, lo: int = 1, hi: int = 1000) -> int:
    """Bound an operator-supplied list `limit` so a huge value can't force an
    unbounded result set / memory spike. Non-numeric falls back to the max."""
    try:
        return max(lo, min(int(limit), hi))
    except (TypeError, ValueError):
        return hi


@app.get("/openapi.json", include_in_schema=False)
def _openapi_loopback(request: Request):
    """Serve the OpenAPI schema ONLY to a loopback caller (the readiness self-check
    in `sysible_controller`). The schema maps the entire control plane, so a remote
    caller — direct or via a proxy — gets a 404, not a request-builder blueprint."""
    client = request.client.host if request.client else ""
    if client not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=404, detail="Not found")
    return app.openapi()

# Log which directory + commit is actually running, so a wrong-directory deploy is
# obvious in `journalctl -u sysible-backend` (see backend/build_info.py).
try:
    from backend import build_info as _build_info
    _build_info.log_startup()
except Exception:
    pass


@app.middleware("http")
async def _security_headers(request, call_next):
    """Defense-in-depth response headers. The API is HTTPS-only (uvicorn
    is launched with a cert), so HSTS is safe to assert; the rest harden
    against sniffing/clickjacking/referrer leakage and tell caches never
    to store API responses (which can carry host data and tokens)."""
    response = await call_next(request)
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    return response


app.include_router(remote_router, dependencies=[Depends(require_api_key)])


def verify_agent(host_id: str, agent_secret: str):
    """Authenticate a request as coming from a previously-enrolled agent."""

    if not agent_exists(host_id):
        raise HTTPException(status_code=404, detail="Unknown host_id")

    # Revoked hosts are locked out regardless of a correct secret, until an admin
    # re-enrolls them (which mints a fresh secret and clears revocation).
    if is_agent_revoked(host_id):
        raise HTTPException(
            status_code=403,
            detail="Agent secret revoked — re-enroll this host to restore it.",
        )

    if not agent_secret_matches(host_id, agent_secret):
        raise HTTPException(status_code=401, detail="Invalid agent secret")


# =========================================================
# SERVER TOKEN GENERATION (admin-only: localhost + API key)
# =========================================================
@app.post("/admin/enroll-token/generate", dependencies=[Depends(require_api_key), Depends(require_superuser)])
async def generate_token(request: Request):

    client_ip = request.client.host

    if client_ip not in [
        "127.0.0.1",
        "::1",
        "localhost"
    ]:
        raise HTTPException(
            status_code=403,
            detail="Forbidden"
        )

    # Early guardrail: a freshly-minted token always binds to a NEW host, so if
    # we're already at the community host cap, refuse here instead of letting the
    # operator set up an agent that would only be rejected at the final enroll
    # step. (Re-enrolling a wiped host reuses its original token, not this one.)
    from backend.edition import enforce_host_limit
    enforce_host_limit()

    token = secrets.token_hex(16)

    create_enroll_token(token)

    from backend.db import ENROLL_TOKEN_VALID_DAYS
    return {
        "token": token,
        "valid_days": ENROLL_TOKEN_VALID_DAYS
    }


@app.post("/admin/enroll-token/reissue", dependencies=[Depends(require_api_key), Depends(require_superuser)])
async def reissue_token(request: Request, body: dict = Body(...),
                        acting: str = Depends(acting_admin_name)):
    """Mint a REISSUE token that authorizes re-binding one EXISTING host_id.

    Re-enrolling an already-registered host (e.g. after a reinstall that wiped the
    agent's saved secret) is otherwise refused: a bearer enroll token alone must not
    be able to take over a host's identity. An administrator issues this host-bound,
    single-use token deliberately, hands it to the reinstalled host, and enrollment
    then re-binds exactly that host_id and no other. Superuser + localhost gated and
    audited, like token generation."""
    client_ip = request.client.host
    if client_ip not in ["127.0.0.1", "::1", "localhost"]:
        raise HTTPException(status_code=403, detail="Forbidden")

    host_id = (body.get("host_id") or "").strip() if isinstance(body, dict) else ""
    if not host_id:
        raise HTTPException(status_code=400, detail="host_id is required.")
    if not _find_agent(host_id):
        raise HTTPException(
            status_code=404,
            detail="No such enrolled host. Reissue is for re-binding an EXISTING host; "
                   "use Generate token to enroll a brand-new host.")

    token = secrets.token_hex(16)
    create_reissue_token(token, host_id)
    log_admin_audit("enroll_token_reissued", acting,
                    f"reissue token minted to re-bind host {host_id}")

    from backend.db import ENROLL_TOKEN_VALID_DAYS
    return {"token": token, "host_id": host_id, "valid_days": ENROLL_TOKEN_VALID_DAYS}


@app.get("/admin/enrollment-pause", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def get_enrollment_pause_route():
    """Whether new agent enrollment is currently paused (the runaway kill-switch)."""
    from backend.db import get_enrollment_control
    return get_enrollment_control()


@app.post("/admin/enrollment-pause", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def set_enrollment_pause_route(body: dict = Body(...),
                               acting: str = Depends(acting_admin_name)):
    """Pause or resume all new agent enrollment. Paused, /agents/enroll refuses
    every new host — the emergency brake for a runaway that re-enrolls faster than
    you can delete it. Existing agents are unaffected. Superuser-gated, audited."""
    from backend.db import set_enrollment_paused, get_enrollment_control
    paused = bool(body.get("paused")) if isinstance(body, dict) else False
    set_enrollment_paused(paused, actor=acting)
    log_admin_audit("enrollment_paused" if paused else "enrollment_resumed",
                    acting, "new agent enrollment " + ("PAUSED" if paused else "resumed"))
    return get_enrollment_control()


@app.get("/admin/enroll-allowlist", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def get_enroll_allowlist_route():
    """The enrollment source-IP allowlist. Empty == all sources allowed (a valid
    token is still required); a non-empty list restricts which networks may enroll."""
    from backend.db import list_enroll_allowlist
    return {"entries": list_enroll_allowlist()}


@app.post("/admin/enroll-allowlist", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def add_enroll_allowlist_route(body: dict = Body(...), acting: str = Depends(acting_admin_name)):
    """Add an allowed source CIDR (or bare IP) to the enrollment allowlist.
    Superuser-gated, audited."""
    from backend.db import add_enroll_allowlist_cidr, list_enroll_allowlist
    cidr = (body.get("cidr") or "").strip() if isinstance(body, dict) else ""
    note = (body.get("note") or "").strip() if isinstance(body, dict) else ""
    try:
        norm = add_enroll_allowlist_cidr(cidr, note, actor=acting)
    except ValueError:
        raise HTTPException(status_code=400,
                            detail=f"Not a valid CIDR or IP address: {cidr!r}")
    log_admin_audit("enroll_allowlist_add", acting,
                    f"allow {norm}" + (f" ({note})" if note else ""))
    return {"entries": list_enroll_allowlist()}


@app.delete("/admin/enroll-allowlist/{entry_id}",
            dependencies=[Depends(require_api_key), Depends(require_superuser)])
def remove_enroll_allowlist_route(entry_id: int, acting: str = Depends(acting_admin_name)):
    """Remove an enrollment-allowlist entry by id. Superuser-gated, audited.
    Removing the last entry re-opens enrollment to all sources (token still required)."""
    from backend.db import remove_enroll_allowlist_cidr, list_enroll_allowlist
    if remove_enroll_allowlist_cidr(entry_id, actor=acting):
        log_admin_audit("enroll_allowlist_remove", acting, f"entry {entry_id}")
    return {"entries": list_enroll_allowlist()}


# =========================================================
# AGENT ENROLLMENT (agent-facing: authenticated by one-time token)
#
# A token may be reused by the SAME host within
# ENROLL_TOKEN_REUSE_WINDOW of its last use (see backend/db.py) - this
# covers disenroll-then-reenroll with the same bundle, where the
# host's local agent_state.json was wiped and it has no way to report
# its old host_id. resolve_enroll_token_host() swaps in the token's
# originally-bound host_id on that within-window reuse, so the host
# lands back on its existing inventory entry instead of silently
# failing to appear under a new, orphaned host_id.
# =========================================================
# =========================================================
# AGENT -> SSH TERMINAL AUTO-ENROLLMENT (see backend/remote_routes.py)
#
# When an agent enrolls (or heartbeats without ever having been set
# up), the controller queues one root command on it that installs the
# controller's SSH key and reports whether sshd is running. The result
# comes back through the normal task-result path below, where
# _consume_ssh_enable_result() registers the host as an SSH connection
# (giving it a real interactive terminal) or records that sshd is
# missing so the GUI can tell the operator.
# =========================================================
def _find_agent(host_id):
    for a in list_agents():
        if a.get("host_id") == host_id:
            return a
    return None


def _find_supersedable_agent(hostname, ip, exclude_host_id):
    """An existing inventory entry that is the SAME machine now re-enrolling and is
    safe to take over instead of creating a duplicate "zombie" record.

    A host that reinstalls loses its agent_state.json and mints a brand-new random
    host_id; if it also enrolls with a FRESH token (e.g. because its old one was
    tied to a now-revoked host), the token carries no bound_host_id, so without this
    the enroll would create a second record for the same box. Match on IP AND the
    same hostname, and only when the old record is NOT currently live (a live agent
    still holds that identity — never hijack it). Returns the best candidate to
    adopt, or None.

    SECURITY: `ip` and `hostname` are unauthenticated request-body fields, so
    adoption is deliberately narrow. A REVOKED record is never adopted here:
    adopting-by-address would let anyone holding a fresh enroll token resurrect an
    admin-revoked host just by claiming its hostname/IP (revocation bypass /
    identity takeover). A revoked host must be re-issued from the console instead
    (the enroll revoked-guard enforces that). We also require the hostname to
    match, not IP alone, so a bare IP claim can't seize an unrelated record."""
    if not ip:
        return None
    in_host = (hostname or "").strip()
    now = time.time()
    candidates = []
    for a in list_agents():
        if a.get("host_id") == exclude_host_id:
            continue
        if (a.get("ip") or "") != ip:
            continue
        # Same box: when BOTH sides report a hostname they must match (a bare IP
        # claim can't seize an unrelated record). But an agent that reports NO
        # hostname (empty — some minimal/bastion hosts) must still adopt its own
        # offline record at this IP instead of spawning a duplicate, so fall back
        # to IP alone in that case.
        a_host = (a.get("hostname") or "").strip()
        if in_host and a_host and a_host != in_host:
            continue
        if a.get("revoked"):                   # never resurrect a revoked host
            continue
        if now - (a.get("last_seen") or 0) < _REENROLL_LIVE_HOST_GRACE_S:
            continue                      # a live agent still owns this identity
        candidates.append(a)
    if not candidates:
        return None
    candidates.sort(key=lambda a: -(a.get("last_seen") or 0))   # most-recently seen
    return candidates[0]


def _forget_ssh_for_deleted_agent(deleted_host_id, agent):
    """Remove the auto-created SSH record for a just-deleted agent — matching on
    BOTH the agent's hostname AND its IP (match_all), so we only ever forget the
    record that genuinely belongs to this agent.

    SECURITY: forgetting by hostname (or IP) alone let a rogue agent that reused a
    still-valid host's hostname (at a different IP) trigger deletion of the VALID
    host's SSH record and un-pin its known_hosts key on disenroll — a TOFU MITM /
    DoS. Requiring hostname AND IP to match the deleted agent closes that: a
    rogue at a different IP can't match the victim's record."""
    if not agent:
        return
    try:
        forget_agent_ssh_host(agent.get("hostname"), agent.get("ip"), match_all=True)
    except Exception:
        pass


def _agent_ssh_shadow_enabled():
    """Whether an agent host should ALSO be auto-registered as an SSH connection
    (the "Agent + SSH" shadow record). OFF by default: agent hosts are fully
    manageable over the agent's own outbound channel — including the web terminal,
    which streams a local PTY over that channel and needs no inbound SSH — so the
    shadow record is redundant and only clutters the inventory with a second entry
    per host that (before this) couldn't be individually deleted. We are phasing
    SSH connections out; set SYSIBLE_AGENT_SSH_TERMINAL=1 to restore the legacy
    auto-SSH behaviour."""
    return os.getenv("SYSIBLE_AGENT_SSH_TERMINAL", "0").strip().lower() in (
        "1", "true", "yes", "on")


def _maybe_enroll_agent_ssh(host_id, hostname, ip, environment, force=False):
    """Queue the one-time SSH-enable command on an agent host, unless we
    already have an SSH connection for it or an attempt is already
    pending. force=True (a fresh /enroll) always re-queues - the
    operator may have just installed sshd after a prior 'sshd_missing'.

    No-op unless the legacy auto-SSH shadow is explicitly enabled
    (SYSIBLE_AGENT_SSH_TERMINAL=1) — see _agent_ssh_shadow_enabled()."""
    if not _agent_ssh_shadow_enabled():
        return
    if not hostname or not ip:
        return
    if ssh_host_exists(hostname):
        set_agent_ssh_state(host_id, {"status": "enabled"})
        return
    state = get_agent_ssh_state(host_id)
    if not force and state and state.get("status") in ("pending", "enabled"):
        return
    try:
        pubkey = _ensure_controller_key()
        task_id = queue_task(host_id, agent_ssh_enable_command(pubkey), kind="ssh_enable")
    except Exception:
        return
    set_agent_ssh_state(host_id, {
        "status": "pending",
        "task_id": task_id,
        "hostname": hostname,
        "ip": ip,
        "environment": environment or "",
    })


def _consume_ssh_enable_result(host_id, result_str):
    """Process the agent's reply to the SSH-enable command: register the
    host as an SSH connection if sshd is up, else record sshd_missing."""
    state = get_agent_ssh_state(host_id) or {}
    hostname = state.get("hostname")
    ip = state.get("ip")
    environment = state.get("environment", "")
    if not hostname or not ip:
        agent = _find_agent(host_id)
        if agent:
            hostname = hostname or agent.get("hostname")
            ip = ip or agent.get("ip")
            environment = environment or agent.get("environment") or ""

    stdout = ""
    try:
        parsed = json.loads(result_str)
        if isinstance(parsed, dict):
            stdout = parsed.get("stdout") or ""
    except (ValueError, TypeError):
        stdout = result_str or ""

    if f"{AGENT_SSH_MARKER}running" in stdout:
        if hostname and ip:
            try:
                if register_agent_ssh_host(hostname, ip, environment):
                    set_agent_ssh_state(host_id, {"status": "enabled"})
                else:
                    # IP already owned by another host — auto-SSH skipped rather
                    # than clobbering that record. Surface it for an admin.
                    set_agent_ssh_state(host_id, {
                        "status": "error",
                        "detail": f"SSH auto-enroll skipped: {ip} is already managed as another host",
                    })
                return
            except Exception:
                pass
        set_agent_ssh_state(host_id, {"status": "error"})
    elif f"{AGENT_SSH_MARKER}stopped" in stdout:
        set_agent_ssh_state(host_id, {"status": "sshd_missing"})
    else:
        set_agent_ssh_state(host_id, {"status": "error"})


# host_id values reserved for internal meaning that must never be a real host.
_RESERVED_HOST_IDS = {"*"}


# Coarse in-process per-IP flood guard for /agents/enroll (see _enroll_rate_limited).
_ENROLL_RATE = {}          # ip -> list[float] recent request timestamps
_ENROLL_RATE_LOCK = threading.Lock()


def _enroll_rate_limited(ip):
    """Return retry-after seconds if `ip` has exceeded the enroll flood limit, else 0.

    Allows up to SYSIBLE_ENROLL_RATE_MAX requests per SYSIBLE_ENROLL_RATE_WINDOW
    seconds per SOURCE IP (default 240 / 60s) — high enough for a legitimate staggered
    mass rollout, even many hosts behind one NAT, but it caps a pathological flood that
    would otherwise hold the enroll lock and starve the threadpool. In-process guard;
    the DB enroll-token consume stays the authoritative anti-replay.
    Set SYSIBLE_ENROLL_RATE_MAX=0 to disable."""
    if not ip:
        return 0
    try:
        maxn = int(os.getenv("SYSIBLE_ENROLL_RATE_MAX", "240"))
    except (TypeError, ValueError):
        maxn = 240
    if maxn <= 0:
        return 0
    try:
        window = int(os.getenv("SYSIBLE_ENROLL_RATE_WINDOW", "60"))
    except (TypeError, ValueError):
        window = 60
    if window <= 0:
        return 0
    now = time.time()
    cutoff = now - window
    with _ENROLL_RATE_LOCK:
        hits = [t for t in _ENROLL_RATE.get(ip, ()) if t >= cutoff]
        if len(hits) >= maxn:
            _ENROLL_RATE[ip] = hits
            return max(1, int(hits[0] + window - now))
        hits.append(now)
        _ENROLL_RATE[ip] = hits
        # Bound memory: drop IPs with no recent hits once the table grows large.
        if len(_ENROLL_RATE) > 10000:
            for k in [k for k, v in list(_ENROLL_RATE.items())
                      if not v or v[-1] < cutoff]:
                _ENROLL_RATE.pop(k, None)
        return 0


@app.post("/agents/enroll")
def enroll(req: EnrollRequest, request: Request):
    # Plain `def` (threadpooled) for the same reason as heartbeat below: the
    # body is all blocking DB/token work and shouldn't occupy the event loop.

    # host_id is client-supplied and used verbatim on a first enroll, so it must
    # be a safe token — no shell/path metacharacters, and never a reserved
    # sentinel. Rejecting it outright (rather than relying on parameterized
    # queries to render it inert) closes injection and reserved-id abuse.
    hid_in = (req.host_id or "").strip()
    if hid_in and (hid_in in _RESERVED_HOST_IDS or
                   not all(c.isalnum() or c in "._-" for c in hid_in)):
        raise HTTPException(status_code=400, detail="Invalid host_id.")

    # Coarse per-source-IP flood guard: shed an enrollment storm before it takes the
    # enroll lock and expensive token validation (a compromised/looping source can't
    # starve the threadpool). Generous by default so legitimate mass rollout is fine.
    _client_ip = request.client.host if request.client else ""
    _retry_after = _enroll_rate_limited(_client_ip)
    if _retry_after:
        raise HTTPException(
            status_code=429,
            detail="Too many enrollment attempts from this source; retry shortly.",
            headers={"Retry-After": str(_retry_after)})

    # Source-IP allowlist: a non-empty allowlist restricts WHICH networks may
    # enroll a new host (the token is still required on top). Empty == allow all
    # (backward compatible); loopback is always allowed (self-enroll / BFF). This
    # narrows the leaked-bundle exposure — a valid token can't be presented from an
    # off-subnet source. Managed from Settings → Enrollment Access.
    from backend.db import enroll_ip_allowed
    if not enroll_ip_allowed(_client_ip):
        raise HTTPException(
            status_code=403,
            detail="Enrollment from this network is not permitted.")

    # Emergency brake: when an admin has paused enrollment (the runaway
    # kill-switch), refuse ALL new enrollments so a crash-looping/re-provisioning
    # agent can't keep spawning rows faster than they're deleted. Existing agents
    # keep heartbeating; only /agents/enroll is closed. 503 (not 403) so a healthy
    # agent's client treats it as retryable rather than a hard auth failure.
    from backend.db import get_enrollment_paused
    if get_enrollment_paused():
        raise HTTPException(
            status_code=503,
            detail="Enrollment is paused by an administrator. New hosts cannot enroll "
                   "until it is resumed from the console.")

    # Hold the enroll lock across validate→consume so a single-use token can't
    # be claimed by two concurrent requests (TOCTOU). enforce_host_limit's
    # count + create_or_update_agent also live inside it, so the cap can't be
    # raced past either.
    with _ENROLL_LOCK:
        if not validate_enroll_token(req.token):
            raise HTTPException(
                status_code=403,
                detail="Invalid or expired token"
            )

        host_id = resolve_enroll_token_host(req.token, req.host_id)

        # Re-enroll takeover guard. On a within-window token REUSE,
        # resolve_enroll_token_host returns the ORIGINAL bound host_id, so this
        # is a re-bind of an existing inventory entry (a fresh first-ever enroll
        # instead lands on a brand-new random host_id with no existing record).
        existing = _find_agent(host_id)

        # Duplicate reconciliation: if this looks like a brand-new host_id but the
        # same machine (same hostname AND IP) already has a stale, NON-revoked
        # record — the classic "reinstall with a fresh token" case, which otherwise
        # spawns a same-IP zombie — adopt that record instead of creating a
        # duplicate. _find_supersedable_agent deliberately never returns a revoked
        # record, so adoption can never silently un-revoke a host.
        if not existing:
            dup = _find_supersedable_agent(req.hostname, req.ip, exclude_host_id=host_id)
            if dup:
                host_id = dup["host_id"]
                existing = dup

        # Refuse the two dangerous re-binds a stolen token enables:
        if existing:
            # (a) An admin deliberately revoked this host — a replayed/fresh token
            #     must never silently un-revoke and resurrect it. The admin has to
            #     re-issue enrollment from the console (which mints a fresh secret
            #     and clears revocation deliberately).
            if existing.get("revoked"):
                raise HTTPException(
                    status_code=403,
                    detail="This host was revoked by an administrator. A superuser "
                           "can Restore it in place (POST /agents/{host_id}/restore) "
                           "if the agent is still installed; a reimaged host must be "
                           "Force-Deleted and enrolled fresh.",
                )
            # (b) Re-binding an EXISTING host's identity requires proof that the caller
            #     is authorized to take it over — a bearer enroll token alone is NOT
            #     enough. A fresh token echoes the caller's requested host_id verbatim
            #     (resolve_enroll_token_host) and host_id is a non-secret,
            #     machine-derivable identifier, so without this gate any token holder
            #     who knows (or computes) a victim's host_id could overwrite its
            #     agent_secret and take it over — locking out the real host, whether it
            #     is online OR merely offline. Authorization is one of:
            #       P1  the caller presents the CURRENT agent_secret (it genuinely IS
            #           the incumbent host — e.g. a deliberate secret refresh), or
            #       P3  an administrator minted a REISSUE token bound to this exact
            #           host_id (the supported path for a reinstall that lost its
            #           secret; mint it from the console / the reissue endpoint).
            #     Absent proof, an online host is refused (409) and an offline one is
            #     refused (403) directing the operator to Reissue or Force Delete —
            #     never a silent takeover. (EE additionally accepts a signature from the
            #     host's registered key as proof; see its enroll handler.)
            #     NOTE: do NOT "supersede when req.host_id == resolved host_id" — that
            #     comparison is a tautology for a fresh token (resolve echoes the input),
            #     so it authenticates nothing and reintroduces the takeover.
            proven = False
            if req.prev_agent_secret and agent_secret_matches(host_id, req.prev_agent_secret):
                proven = True   # P1: holds the incumbent secret
            if not proven and enroll_token_authorizes_rebind(req.token, host_id):
                proven = True       # P3: admin-authorized reissue token for this host

            if not proven and _ENROLL_REQUIRE_REBIND_AUTH:
                last_seen = existing.get("last_seen") or 0
                if time.time() - last_seen < _REENROLL_LIVE_HOST_GRACE_S:
                    raise HTTPException(
                        status_code=409,
                        detail="A live agent is already enrolled for this host and is "
                               "still online, so re-enrollment is blocked (this stops a "
                               "token holder from hijacking a live host). Wait until it "
                               "goes offline, or Force Delete its record first.",
                    )
                raise HTTPException(
                    status_code=403,
                    detail="This host_id is already enrolled. Re-enrolling an existing "
                           "host requires authorization — either the current agent "
                           "credential, or an admin-issued reissue token for this host "
                           "(console: host → Reissue enrollment). To replace it "
                           "outright, Force Delete the record first, then enroll fresh.",
                )

        # Community-edition host cap (no-op in an unlimited/Enterprise build).
        # Keyed on host_id: a new agent host_id always counts against the cap,
        # even if its hostname collides with an already-enrolled host.
        from backend.edition import enforce_host_limit
        enforce_host_limit(req.hostname or host_id, candidate_host_id=host_id)

        agent_secret = secrets.token_hex(24)

        # Bind/consume the token BEFORE inserting the agent row. If the row insert
        # fails (e.g. transient DB lock), the token is already bound to this
        # host_id, so a retry with the same token resolves back to it and updates
        # the one row — rather than the reverse order, where a row could exist
        # with an unbound token and the next fresh-uuid enroll would spawn a
        # duplicate. (Both live inside _ENROLL_LOCK.) The claim is atomic at the DB
        # (conditional UPDATE), so even if a second replica raced past validate with
        # the same fresh token, only one claim wins; the loser gets 409 here rather
        # than both enrolling distinct hosts off one token.
        if not consume_enroll_token(req.token, host_id):
            raise HTTPException(
                status_code=409,
                detail="This enrollment token was just claimed by another host. "
                       "Generate a fresh token and try again.",
            )

        create_or_update_agent(
            host_id,
            req.hostname,
            req.platform,
            req.kernel,
            "online",
            time.time(),
            agent_secret,
            req.ip
        )

        # Re-seal the integrity baseline for this (re-)enrollment. The baseline is
        # keyed by host_id and SURVIVES disenroll, but a re-enroll reuses the same
        # host_id with a freshly-installed agent whose self-measurement legitimately
        # differs (the bundle patches the controller address into agent.py at build
        # time, and the agent may be a newer build). Without dropping the stale
        # baseline the very next heartbeat compares against it and quarantines a
        # perfectly healthy re-enrolled host. Dropping it now makes the next
        # heartbeat re-seal from the current measurement — exactly what an admin
        # Rebaseline/Restore does. Harmless no-op on a brand-new host_id.
        from backend import agent_integrity as _agent_integrity
        _agent_integrity.rebaseline(host_id)

    # Give this agent host a real SSH terminal automatically: queue the
    # controller's key-install + sshd-check command. force=True so a
    # re-enroll retries even if a previous attempt found sshd missing.
    agent = _find_agent(host_id)
    _maybe_enroll_agent_ssh(
        host_id,
        req.hostname or (agent or {}).get("hostname"),
        req.ip or (agent or {}).get("ip"),
        (agent or {}).get("environment"),
        force=True,
    )

    # Surface the enrollment as a fleet event: it lands in the activity feed
    # (Live Activity & Logs) and the web console pops a toast when it notices the
    # new host. The actor is the host itself (enrollment is token-driven, not an
    # operator action), so attribute it to "enrollment". Best-effort — a logging
    # hiccup must never fail the enroll.
    try:
        log_activity("enrollment", req.hostname or host_id,
                     f"Host enrolled ({req.platform or 'agent'}) at {req.ip or 'unknown IP'}", "")
    except Exception:
        pass

    return {
        "host_id": host_id,
        "agent_secret": agent_secret,
        "status": "enrolled"
    }


# =========================================================
# HEARTBEAT (agent-facing: authenticated by per-host secret)
# =========================================================
@app.post("/agents/heartbeat")
def heartbeat(req: HeartbeatRequest):
    # Plain `def` (not async) on purpose: the body does blocking SQLite work
    # (verify, update_agent_heartbeat) and FastAPI runs sync handlers in a
    # threadpool. As an `async def` it ran on the single event loop, so every
    # agent's heartbeat - the most frequent request in the system, once per
    # SYSIBLE_POLL_INTERVAL per host - serialized through one thread and
    # became the throughput ceiling for large fleets. Threadpooling lets many
    # heartbeats land concurrently.

    verify_agent(req.host_id, req.agent_secret)

    update_agent_heartbeat(req.host_id, req.ip, req.hostname, agent_version=req.agent_version)

    # Agent integrity (Tier 1): when the agent reports a self-measurement,
    # compare it against this host's sealed baseline (trust-on-first-use) and
    # quarantine on mismatch. evaluate() never raises, so integrity can't break
    # the heartbeat path. Quarantine is enforced in poll_agent_tasks below.
    from backend import agent_integrity
    if req.measurements is not None:
        agent_integrity.evaluate(req.host_id, req.measurements)
    else:
        # No manifest on this heartbeat. Harmless for an agent that never
        # measured, but a host that HAS a sealed baseline and stops reporting is
        # a compromised agent trying to evade the check by dropping the field —
        # note_missing() quarantines it once the omission outlives the grace
        # window. Never raises.
        agent_integrity.note_missing(req.host_id)

    # Persist a performance sample if this heartbeat carried one (newer agents
    # attach one ~once per SYSIBLE_METRICS_INTERVAL). Wrapped so a malformed
    # payload or a transient write error can never fail the heartbeat itself -
    # losing one sample is harmless; a 500 here would spam the agent log.
    if req.metrics:
        try:
            def _num(key, cast):
                v = req.metrics.get(key)
                return None if v is None else cast(v)
            insert_metric_sample(
                req.host_id,
                time.time(),
                _num("load1", float),
                _num("cores", int),
                _num("mem", int),
                _num("disk", int),
                load5=_num("load5", float),
                load15=_num("load15", float),
                cpu=_num("cpu", float),
                swap=_num("swap", int),
                net_rx=_num("net_rx", float),
                net_tx=_num("net_tx", float),
                io_r=_num("io_r", float),
                io_w=_num("io_w", float),
                procs=_num("procs", int),
            )
        except Exception:
            pass
        # Latest fleet-health reading (newer agents attach failed/sysd/oom so the
        # dashboard can render health without a live probe). Best-effort; a miss
        # just means that host gets live-probed next sweep.
        try:
            upsert_host_health(
                req.host_id, time.time(),
                _num("disk", int), _num("mem", int), _num("load1", float),
                _num("cores", int), _num("failed", int), _num("oom", int),
                _num("uptime", int),
                (req.metrics.get("sysd") or None),
                (req.metrics.get("mount") or None),
                req.metrics.get("units") or [],
                (req.metrics.get("hyp") or None),
                _num("vms", int),
            )
        except Exception:
            pass

    # Persist the latest rich detail snapshot if attached (newer agents only).
    # Stored as one row per host (overwritten), feeding the per-host drill-down.
    if req.snapshot:
        try:
            upsert_host_snapshot(req.host_id, time.time(), json.dumps(req.snapshot))
        except Exception:
            pass

    # Catch up already-enrolled agents that predate SSH auto-enrollment.
    # Heartbeats are frequent, so gate on the cheap state read first:
    # only when a host has *never* been attempted (state is None) do we
    # pay for the agent lookup + queue. Once set (pending/enabled/
    # sshd_missing/error) this is a single small file read and returns.
    if get_agent_ssh_state(req.host_id) is None:
        agent = _find_agent(req.host_id)
        if agent:
            _maybe_enroll_agent_ssh(
                req.host_id,
                agent.get("hostname"),
                req.ip or agent.get("ip"),
                agent.get("environment"),
            )

    return {
        "status": "ok"
    }


# =========================================================
# TASK QUEUE
# =========================================================
# Become-passwords (sudo passwords for password-sudo hosts) are held ONLY in
# this process's memory, keyed by task id, and never written to the DB or
# logged. They're handed to the host's authenticated agent exactly once when
# it polls for the task, then deleted; a TTL sweep drops any that were never
# collected (e.g. an offline host) so a password can't linger.
# Task kinds that are background/internal reads, not operator actions -
# kept out of the activity feed (e.g. the User & Group panel's user-list
# sync that fires automatically after any change).
_NON_LOGGED_KINDS = {"sync_users", "ssh_enable", "ssh_grant", "ssh_revoke"}

_PENDING_BECOME = {}            # task_id -> {"password": str, "expiry": float}
_PENDING_BECOME_LOCK = threading.Lock()
_BECOME_TTL_S = 300

# Serializes the agent-enrollment critical section. Enrollment validates a
# single-use token, then (separately) consumes it; without this lock two
# concurrent requests carrying the SAME token can both pass validation before
# either consumes it (a TOCTOU race), letting one leaked bundle enroll more
# than one host and slip past the host cap. Enrollment is infrequent and the
# controller is single-process, so a coarse lock here is cheap and correct.
_ENROLL_LOCK = threading.Lock()

# Re-enroll (enroll-token reuse within the grace window) hardening. The enroll
# token is a bearer credential baked into the downloadable bundle, so a leaked
# token could otherwise be replayed to re-bind an existing host_id — minting a
# fresh agent_secret (locking out the real host and handing the attacker its
# queued tasks) and clearing a prior admin revocation. A host that is still
# actively heartbeating within this window has its identity and never needs to
# re-enroll, so a re-bind against a live host is treated as a takeover attempt.
try:
    _REENROLL_LIVE_HOST_GRACE_S = int(os.getenv("SYSIBLE_REENROLL_LIVE_HOST_GRACE_S", "300"))
except ValueError:
    _REENROLL_LIVE_HOST_GRACE_S = 300

# Require proof-of-possession (current agent_secret) or an admin reissue token before
# re-binding an ALREADY-ENROLLED host_id. Default ON — this closes the offline-host
# takeover where a bearer enroll-token holder overwrites a host's credential. It can be
# set to "0" as a temporary escape hatch during a fleet migration that predates the
# reissue flow, but leaving it off re-opens the takeover, so keep it on in production.
_ENROLL_REQUIRE_REBIND_AUTH = os.getenv("SYSIBLE_ENROLL_REQUIRE_REBIND_AUTH", "1") != "0"

# Reclaim tasks stuck in 'dispatched' (the host was handed the command but its
# result never came back — lost in transit, or the agent died on receipt) so they
# reach a terminal 'timed_out' state with a synthetic result instead of living
# forever. Well beyond the agent's 300s command timeout + the 300s become TTL, so
# a legitimately-long task is never reclaimed early. NOT re-queued (see
# db.reclaim_stale_tasks): at-most-once, to avoid double-running a privileged
# command whose result was merely lost.
_TASK_RECLAIM_SECONDS = int(os.getenv("SYSIBLE_TASK_RECLAIM_SECONDS", "900"))


def _task_reclaim_loop():
    import time as _t
    _first = True
    while True:
        # Reclaim once at startup BEFORE the first sleep: a command dispatched
        # seconds before a backend restart would otherwise sit "in progress" for up
        # to the reclaim window (default 15 min) before being marked timed_out.
        if not _first:
            _t.sleep(60)
        _first = False
        try:
            n = reclaim_stale_tasks(_TASK_RECLAIM_SECONDS)
            if n:
                print(f"[controller] reclaimed {n} stale task(s) as timed_out")
        except Exception:
            pass


threading.Thread(target=_task_reclaim_loop, name="sysible-task-reclaim", daemon=True).start()


def _sweep_become(now=None):
    now = now or time.time()
    with _PENDING_BECOME_LOCK:
        for tid in [t for t, v in _PENDING_BECOME.items() if v["expiry"] < now]:
            _PENDING_BECOME.pop(tid, None)


def _store_become(task_id, password):
    if not password:
        return
    _sweep_become()
    with _PENDING_BECOME_LOCK:
        _PENDING_BECOME[task_id] = {"password": password, "expiry": time.time() + _BECOME_TTL_S}


def _take_become(task_id):
    with _PENDING_BECOME_LOCK:
        rec = _PENDING_BECOME.pop(task_id, None)
    return rec["password"] if rec else None


@app.post("/agents/{host_id}/tasks", dependencies=[Depends(require_api_key)])
def queue_agent_task(host_id: str, body: TaskCreateRequest, request: Request):

    if not agent_exists(host_id):
        raise HTTPException(status_code=404, detail="Unknown host_id")

    # RBAC: tag the task with the initiating admin's username, taken from
    # their login token (NOT from any client-supplied field), so the agent
    # runs the command as that matching local user under the host's own
    # sudo policy. A present-but-invalid/expired token is rejected. No token
    # at all => an internal/controller-issued task (e.g. SSH auto-enroll,
    # user sync) that runs as the agent itself (root).
    run_as = None
    token = request.headers.get("X-Sysible-Admin-Token")
    if token:
        admin = resolve_admin_token(token)
        if not admin:
            raise HTTPException(status_code=401, detail="Invalid or expired admin token")
        # Read-only 'auditor' accounts can never dispatch a command to a host.
        # Enforced here (front-end independent) so it holds for the web console
        # or a hand-crafted API call alike.
        if admin.get("role") == "auditor":
            raise HTTPException(status_code=403, detail="Auditor accounts are read-only.")
        run_as = admin["username"]

    task_id = queue_task(host_id, body.command, body.kind, run_as=run_as)

    # Become-password (for password-sudo hosts): RAM only, keyed to this task,
    # delivered to the agent on its next poll. Never stored in the DB/log.
    if run_as and body.become_password:
        _store_become(task_id, body.become_password)

    # Activity feed: record who did what, where. Only for admin-initiated
    # tasks (run_as set), and not background/internal reads - the user-list
    # sync and SSH auto-enroll aren't operator actions and would just be
    # noise (showing as "ran a script" after an unrelated click).
    if run_as and body.log and body.kind not in _NON_LOGGED_KINDS:
        log_activity(run_as, get_agent_hostname(host_id),
                     body.description or _describe_command(body.command), body.command)

    return {
        "task_id": task_id,
        "status": "queued"
    }


@app.post("/activity", dependencies=[Depends(require_api_key)])
def post_activity_route(body: ActivityLogRequest, request: Request):
    """Record ONE attributed activity entry. The web console uses this to log a
    single grouped summary for a multi-host tool run (e.g. "List disks · dev1,
    prod1, prod2") instead of one near-identical row per host. Attributed to the
    caller's admin token; read-only auditors are refused."""
    token = request.headers.get("X-Sysible-Admin-Token")
    admin = resolve_admin_token(token) if token else None
    if not admin:
        raise HTTPException(status_code=401, detail="A login token is required for this action.")
    if admin.get("role") == "auditor":
        raise HTTPException(status_code=403, detail="Auditor accounts are read-only.")
    # Bound the free-text fields — this is a one-line summary, not a payload.
    desc = (body.description or "").strip()[:200]
    if not desc:
        raise HTTPException(status_code=400, detail="description required")
    log_activity(admin["username"], (body.host or "")[:200], desc, "")
    return {"ok": True}


def _build_agent_update_command():
    """(version, command) that updates a managed host's agent in place. The
    controller's current host_agent/agent.py is shipped inline (base64) so the
    task needs no extra fetch/credentials, decoded over the installed agent.py,
    py_compiled as a guard (a corrupt transfer leaves the old agent untouched),
    then the agent service is restarted OUT OF BAND (a scheduled transient unit
    / detached shell) so the task can report success before the agent bounces -
    a plain `systemctl restart` would kill the agent process running this task."""
    import base64
    from backend.agent_bundle import (AGENT_SOURCE_FILE, _AGENT_INSTALL_DIR,
                                      _SERVICE_NAME, agent_version_of)

    src = AGENT_SOURCE_FILE.read_text(encoding="utf-8")
    b64 = base64.b64encode(src.encode("utf-8")).decode("ascii")
    # Match what the agent reports post-update (AGENT_VERSION normalizes the
    # baked controller-URL default out before hashing) so rollout tracking and
    # the "updated to X" message agree with the fleet's heartbeat version.
    ver = agent_version_of(src)
    install = f"{_AGENT_INSTALL_DIR}/agent.py"
    svc = _SERVICE_NAME
    cmd = (
        "set -e\n"
        f"nf={install}.new\n"
        f"printf '%s' '{b64}' | base64 -d > \"$nf\"\n"
        "python3 -m py_compile \"$nf\"\n"
        f"mv -f \"$nf\" {install}\n"
        # Restart after this task returns: a 3s-delayed transient unit (own
        # cgroup) if systemd-run exists, else a detached shell. Either way the
        # restart can't kill the agent before it reports this task's result.
        "if command -v systemd-run >/dev/null 2>&1; then\n"
        f"  systemd-run --collect --on-active=3 --unit=sysible-agent-selfupdate "
        f"systemctl restart {svc} >/dev/null 2>&1 "
        f"|| setsid sh -c 'sleep 3; systemctl restart {svc}' >/dev/null 2>&1 &\n"
        "else\n"
        f"  setsid sh -c 'sleep 3; systemctl restart {svc}' >/dev/null 2>&1 &\n"
        "fi\n"
        f"echo \"sysible-agent updated to {ver}; restarting\"\n"
    )
    return ver, cmd


@app.post("/agents/update", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def update_agents_route(request: Request):
    """Push the controller's current agent to every managed host over the
    existing task channel: queue a self-update command (runs as root on the
    host - no run_as, so it isn't gated on an operator's local account) on each
    agent. Agents apply it on their next poll and restart with the new code.
    Superuser-only. Offline agents pick it up whenever they next check in."""
    ver, cmd = _build_agent_update_command()
    from backend import agent_integrity
    queued = 0
    for a in list_agents():
        hid = a.get("host_id")
        if not hid:
            continue
        # A disenrolled/revoked host keeps its row (for history / re-enroll), but
        # its agent_secret is revoked so it can never poll this task, and it is no
        # longer a managed host — don't queue an update it would never apply.
        if a.get("revoked"):
            continue
        try:
            queue_task(hid, cmd, kind="agent-update", run_as=None)
            # The agent's own files (and reported version) will change once it
            # applies this update — that's an expected, controller-initiated
            # change, so open a re-seal window instead of quarantining the host
            # when its next heartbeat diverges from the sealed baseline.
            agent_integrity.mark_updating(hid)
            queued += 1
        except Exception:
            pass

    # Attribute the bulk action in the activity feed (single entry, no command
    # text - it's a 50KB base64 blob, and the agent log deliberately omits it).
    token = request.headers.get("X-Sysible-Admin-Token")
    actor = "controller"
    if token:
        admin = resolve_admin_token(token)
        if admin:
            actor = admin.get("username", "controller")
    try:
        log_activity(actor, f"{queued} host(s)",
                     f"Pushed agent update ({ver}) to all managed hosts", "")
    except Exception:
        pass

    return {"queued": queued, "version": ver,
            "message": f"Agent update queued for {queued} host(s). "
                       "Each applies it on its next check-in and restarts."}


def _current_agent_version():
    """Short hash of the controller's CURRENT host_agent/agent.py — the build an
    'update agents' push would deliver. Agents report theirs on heartbeat, so a
    mismatch means the agent is out of date. The per-host baked controller-URL
    default is normalized out the same way the agent computes AGENT_VERSION —
    otherwise every enrolled agent (whose bundle patched that line) would look
    permanently outdated."""
    from backend.agent_bundle import AGENT_SOURCE_FILE, agent_version_of
    try:
        return agent_version_of(AGENT_SOURCE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _controller_update_available():
    """Best-effort: is the deployed controller behind its git remote? Reads the
    install checkout path from <base>/.install_src (written at install time),
    fetches, and counts commits behind. Never raises — returns a dict the UI can
    render, with checked=False + a reason when it can't tell (no git, no network,
    private-repo auth, etc.)."""
    import os as _os
    import subprocess as _sp
    base = _os.getenv("SYSIBLE_HOME", "/opt/sysible")
    try:
        src = open(_os.path.join(base, ".install_src")).read().strip()
    except Exception:
        return {"checked": False, "reason": "install source unknown (.install_src missing)"}
    if not src or not _os.path.isdir(_os.path.join(src, ".git")):
        return {"checked": False, "reason": "install source is not a git checkout"}

    # Use `git ls-remote` — a READ-ONLY remote query that writes no refs and
    # fetches no objects — instead of `git fetch`. A `git fetch` in the live
    # deployment repo (a) collides with the self-update pull and (b), when killed
    # (timeout / cancelled request / restart), strands *.lock files under
    # .git/refs that then break every later ref update ("cannot lock ref ...
    # .lock: File exists" → "reference already exists"), wedging self-update.
    # ls-remote can't do either, so the periodic check is safe. Bounded so the
    # endpoint (which also carries the instant agent counts) returns promptly.
    tmo = float(os.getenv("SYSIBLE_UPDATE_FETCH_TIMEOUT", "8"))

    def _git(*args, timeout=8):
        return _sp.run(["git", "-C", src, "-c", f"safe.directory={src}", *args],
                       capture_output=True, text=True, timeout=timeout)
    try:
        cur_full = _git("rev-parse", "HEAD").stdout.strip()
        cur = cur_full[:7]
        branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        up = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}").stdout.strip()
        if not up or "/" not in up:
            return {"checked": False, "reason": "no upstream branch configured",
                    "current": cur, "branch": branch}
        remote, rbranch = up.split("/", 1)
        ls = _git("ls-remote", remote, rbranch, timeout=tmo)
        if ls.returncode != 0:
            # Surface the ACTUAL git error (auth prompt, unknown host, TLS, proxy,
            # permission denied…) so the operator can act on it, instead of a generic
            # "network or auth". Trimmed and single-lined for the UI.
            detail = " ".join((ls.stderr or ls.stdout or "").split())[:200]
            return {"checked": False,
                    "reason": f"git ls-remote {remote} {rbranch} failed"
                              + (f": {detail}" if detail else " (network or auth)"),
                    "current": cur, "branch": branch}
        remote_sha = (ls.stdout.split() or [""])[0].strip()
        if not remote_sha:
            return {"checked": False, "reason": "remote branch not found on origin",
                    "current": cur, "branch": branch}
        available = bool(cur_full) and remote_sha != cur_full
        return {"checked": True, "available": available, "current": cur,
                "latest": remote_sha[:7], "branch": branch}
    except Exception as e:
        return {"checked": False, "reason": str(e)[:120]}


@app.get("/update-status", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def update_status_route():
    """Whether the CONTROLLER (git-behind) and AGENTS (build-hash mismatch) have a
    software update available. Superuser-only. The git check does a live fetch, so
    call it on demand, not on a poll."""
    cur_ver = _current_agent_version()
    # Exclude revoked/disenrolled hosts: they keep their row but aren't managed
    # agents anymore, so they must not count as "outdated" or in the fleet total.
    agents = [a for a in list_agents() if not a.get("revoked")]
    outdated = [{"host_id": a.get("host_id"), "hostname": a.get("hostname"),
                 "agent_version": a.get("agent_version")}
                for a in agents if cur_ver and a.get("agent_version") and a.get("agent_version") != cur_ver]
    unknown = sum(1 for a in agents if not a.get("agent_version"))
    return {
        "controller": _controller_update_available(),
        "agents": {"current_version": cur_ver, "total": len(agents),
                   "outdated": outdated, "outdated_count": len(outdated),
                   "unknown_count": unknown},
    }


def _describe_command(command: str) -> str:
    """Fallback human description when a tool didn't supply one. Deliberately
    NEVER echoes raw code into the feed: multi-line / script / python
    commands collapse to a generic phrase, and a concise single-line shell
    command shows just its leading program name (e.g. 'ran: systemctl')."""
    c = (command or "").strip()
    if not c:
        return "ran a command"
    first = c.split("\n", 1)[0].strip()
    is_scripty = (
        "\n" in c
        or first.startswith(("import ", "python", "#!", "cat <<", "base64", "{"))
        or len(first) > 80
    )
    # Never the words "ran a script" here — that phrasing is reserved for the
    # explicit "Run a script on all hosts" action from Connect (which passes its
    # own description). A generic multi-line/opaque command is just "ran a command".
    if is_scripty:
        return "ran a command"
    return "ran: " + first[:80]


@app.get("/agents/{host_id}/tasks")
def poll_agent_tasks(host_id: str, agent_secret: str = "",
                     x_agent_secret: str = Header(default=None, alias="X-Agent-Secret")):
    # Prefer the header (kept out of URLs/access logs); fall back to the query
    # param so already-deployed agents keep working.
    verify_agent(host_id, x_agent_secret or agent_secret)

    # Agent integrity: a quarantined host (self-measurement diverged from its
    # sealed baseline) keeps heartbeating but is handed NO work — the soft
    # lockout — so a tampered agent is cut off from dispatch. Enforced here on
    # the controller (the trust anchor). Cleared by an admin resume/rebaseline.
    from backend import agent_integrity
    if agent_integrity.is_quarantined(host_id):
        return {"tasks": [], "quarantined": True}

    tasks = fetch_pending_tasks(host_id)
    # Attach any RAM-held become-password to its task, exactly once. Only this
    # host's authenticated agent reaches here (verify_agent above), so a
    # password for host X is only ever handed to host X.
    for t in tasks:
        pw = _take_become(t.get("id"))
        if pw is not None:
            t["become_password"] = pw

    return {"tasks": tasks}


@app.post("/agents/{host_id}/tasks/result")
def post_task_result(host_id: str, body: TaskResultRequest):

    if host_id != body.host_id:
        raise HTTPException(status_code=400, detail="host_id mismatch")

    verify_agent(body.host_id, body.agent_secret)

    # The (authenticated) agent may only report on a task that was actually
    # queued for IT - otherwise a valid agent could post results against
    # another host's task_id. An unknown task_id (already pruned, never
    # existed) is rejected too.
    owner = get_task_host(body.task_id)
    if owner is None or owner != body.host_id:
        raise HTTPException(status_code=404, detail="Unknown task for this host")

    applied = submit_task_result(body.task_id, body.host_id, body.result)

    # Only act on the FIRST valid result for a dispatched task. A duplicate /
    # retried result (task already 'done') or a result for a never-dispatched
    # task is ignored, so side effects like the SSH-enable registration below
    # can't run twice.
    # The controller's own SSH-terminal auto-enroll command reports back
    # through this same path - intercept its result to register the SSH
    # connection (or record sshd_missing) rather than showing it to the
    # operator as an ordinary command result.
    if applied and get_task_kind(body.task_id) == "ssh_enable":
        _consume_ssh_enable_result(body.host_id, body.result)

    return {
        "status": "recorded" if applied else "ignored"
    }


# =========================================================
# AGENT-HOSTED PTY (Option B): the agent runs the shell locally and streams it
# to the controller over these outbound calls, so terminals work with no inbound
# SSH to the host. Authenticated by the per-host agent secret. See
# backend/remote_routes.py for the controller-side session buffers.
# =========================================================
@app.post("/agents/{host_id}/pty/{session_id}/output")
def pty_output(host_id: str, session_id: str, body: PtyOutputRequest):
    verify_agent(host_id, body.agent_secret)
    from backend.remote_routes import pty_push_output
    closed = pty_push_output(session_id, host_id, body.data, body.ended)
    return {"ok": True, "closed": closed}


@app.get("/agents/{host_id}/pty/{session_id}/io")
def pty_io(host_id: str, session_id: str, agent_secret: str = "",
           x_agent_secret: str = Header(default=None, alias="X-Agent-Secret")):
    verify_agent(host_id, x_agent_secret or agent_secret)
    from backend.remote_routes import pty_take_input
    msgs, closed = pty_take_input(session_id, host_id, wait=25.0)
    return {"msgs": msgs, "closed": closed}


@app.get("/agents/{host_id}/results", dependencies=[Depends(require_api_key)])
def get_agent_results(
    host_id: str,
    kind: Optional[str] = None,
    task_id: Optional[int] = None
):

    if not agent_exists(host_id):
        raise HTTPException(status_code=404, detail="Unknown host_id")

    return {
        "results": list_results(host_id, kind=kind, task_id=task_id)
    }


# =========================================================
# EDITION (host-cap info for the GUI to display)
# =========================================================
@app.get("/edition", dependencies=[Depends(require_api_key)])
def get_edition():
    from backend.edition import edition_info
    return edition_info()


@app.get("/activity-log", dependencies=[Depends(require_api_key), Depends(require_activity_viewer)])
def get_activity_log_route(limit: int = 200, since_id: int = 0):
    """Human-readable, attributed feed of actions the controller carried out
    (who did what, where) - a fleet-wide audit view. Visible to superusers and
    the read-only 'auditor' role (see require_activity_viewer). The controller
    service log below stays superuser-only. `since_id` lets the GUI poll for
    only new rows."""
    return {"entries": get_activity_log(limit=_clamp_limit(limit), since_id=since_id)}


@app.get("/activity-log/verify",
         dependencies=[Depends(require_api_key), Depends(require_activity_viewer)])
def verify_activity_log_route():
    """Recompute the tamper-evident hash chain over the activity log AND the admin
    audit log and report whether each verifies. Each result is
    {ok, checked, broken_at, keyed} — `ok=false` with `broken_at` pinpoints the
    first altered/removed row; `keyed=true` means the chain is HMAC-keyed
    (unforgeable without the master key) rather than merely SHA-256 tamper-evident.
    Visible to superusers and the read-only auditor role."""
    from backend.db import verify_activity_chain, verify_admin_audit_chain
    result = dict(verify_activity_chain())
    result["admin_audit"] = verify_admin_audit_chain()
    return result


def _cert_fingerprints(path):
    """SHA-256 fingerprints (hex) of every certificate in a PEM file, as a
    frozenset. Parses the DER so PEM whitespace/ordering differences don't
    produce false mismatches. Returns None if the file is missing or can't
    be read/parsed (best-effort — a warning must never be a false alarm just
    because the controller service user can't read a path)."""
    import hashlib
    from pathlib import Path
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        raw = Path(path).read_bytes()
    except Exception:
        return None
    fps = set()
    marker = b"-----BEGIN CERTIFICATE-----"
    try:
        idx = raw.find(marker)
        while idx != -1:
            nxt = raw.find(marker, idx + len(marker))
            block = raw[idx:] if nxt == -1 else raw[idx:nxt]
            try:
                cert = x509.load_pem_x509_certificate(block)
                der = cert.public_bytes(serialization.Encoding.DER)
                fps.add(hashlib.sha256(der).hexdigest())
            except Exception:
                pass
            idx = nxt
    except Exception:
        return None
    return frozenset(fps) if fps else None


def _health_warnings():
    """Cheap, defensive fleet-health signals for the console warning banner.
    Each detector is independently wrapped so a failure in one can't blank the
    others or throw. Returns a list of {id, severity, title, detail, hint}."""
    warnings = []

    # --- Stale pinned TLS cert (the classic "agents can't check in after a
    # controller cert regen / reinstall / new-IP reissue" outage). Compare what
    # the controller now hands out for pinning (trust.crt, falling back to the
    # serving leaf) against the cert the controller's own loopback agent has
    # pinned locally. If they diverge, every agent pinning the old cert will
    # fail TLS verification until re-deployed.
    try:
        from backend import tls_manager
        expected_src = tls_manager.TRUST_FILE if tls_manager.TRUST_FILE.exists() \
            else tls_manager.CERT_FILE
        expected = _cert_fingerprints(expected_src)
        pinned_path = os.getenv("SYSIBLE_CA_CERT", "/etc/sysible/controller.crt")
        pinned = _cert_fingerprints(pinned_path)
        # Only warn when BOTH are readable and genuinely differ — an unreadable
        # pinned file (permissions, not co-located) means "can't tell", not "drift".
        if expected and pinned and expected != pinned:
            warnings.append({
                "id": "tls_cert_pin_drift",
                "severity": "critical",
                "title": "Controller TLS certificate changed — agents can't check in",
                "detail": ("The controller is now serving a TLS certificate that "
                           "differs from the one agents pinned. Agents that pinned "
                           "the old certificate will fail to connect (TLS verify) "
                           "until they receive the new one."),
                "hint": ("Refresh the pinned cert on each host: copy the controller's "
                         "current cert to the agent's pinned path (default "
                         "/etc/sysible/controller.crt) and restart sysible-agent, or "
                         "re-enroll with a fresh bundle. Remote hosts also need the "
                         "new controller address if it moved."),
            })
    except Exception:
        pass

    # --- Mass host silence. A high fraction of enrolled hosts going stale at
    # once is the fleet-wide symptom of the outage above (cert drift, address
    # change, controller down), distinct from a single host being powered off.
    try:
        now = time.time()
        stale_after = int(os.getenv("SYSIBLE_HEALTH_OFFLINE_S",
                                    str(max(300, _REENROLL_LIVE_HOST_GRACE_S))))
        agents = [a for a in list_agents() if not a.get("revoked")]
        total = len(agents)
        offline = [a for a in agents
                   if (now - (a.get("last_seen") or 0)) > stale_after]
        n_off = len(offline)
        if total >= 2 and n_off >= 2 and (n_off / total) >= 0.5:
            mins = max(1, stale_after // 60)
            warnings.append({
                "id": "hosts_offline",
                "severity": "warning",
                "title": f"{n_off} of {total} hosts have stopped checking in",
                "detail": (f"{n_off} of {total} enrolled hosts have not reported in "
                           f"over {mins} min. When most of the fleet goes quiet at "
                           "once it usually points at a controller-side change — a "
                           "regenerated TLS certificate, a moved controller address, "
                           "or the controller being down."),
                "hint": ("Check the controller service and its TLS cert first; if the "
                         "cert or address changed, re-deploy the pinned cert/bundle to "
                         "the affected hosts."),
            })
    except Exception:
        pass

    return warnings


@app.get("/admin/health-warnings",
         dependencies=[Depends(require_api_key), Depends(require_superuser)])
def health_warnings_route():
    """Operational warnings for the console banner: stale/mismatched pinned TLS
    cert and mass host silence. Cheap enough to poll; read-only and defensive."""
    return {"warnings": _health_warnings()}


@app.get("/controller-log", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def get_controller_log_route(lines: int = 400):
    """Recent controller (sysible-backend) log lines from the journal, for
    the Live Activity & Logs view. The backend runs under systemd, so it can
    read its own journal."""
    import subprocess
    lines = max(1, min(int(lines), 5000))
    try:
        proc = subprocess.run(
            ["journalctl", "-u", "sysible-backend", "-n", str(lines), "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=10,
        )
        out = proc.stdout or proc.stderr
        if proc.returncode != 0 and not proc.stdout:
            out = (f"(could not read journal: {proc.stderr.strip() or 'journalctl failed'})\n"
                   "If the controller wasn't started via systemd, use "
                   "'journalctl -u sysible-backend' or 'sysible_controller logs'.")
    except FileNotFoundError:
        out = "(journalctl not available on this host)"
    except subprocess.TimeoutExpired:
        out = "(timed out reading the journal)"
    return {"log": out}


# =========================================================
# INVENTORY
# =========================================================
@app.get("/metrics/timeseries", dependencies=[Depends(require_api_key)])
def get_metrics_timeseries(window: int = 3600):
    """Per-host performance time-series (load/mem/disk) for the last `window`
    seconds, grouped for the web console's Performance view. Read-only; the
    samples are reported by the agents themselves on heartbeat."""
    hosts = get_metric_samples(window)
    return {"hosts": hosts, "window": window, "now": time.time()}


@app.get("/metrics/fleet-health", dependencies=[Depends(require_api_key)])
def get_fleet_health_readings_route():
    """Latest fleet-HEALTH reading for every host, keyed by host_id, from what the
    agents reported on heartbeat (disk/mem/load + failed-units/systemd/OOM). Lets
    the console render the dashboard health donut without a live probe per host.
    Read-only; the console computes the verdict + freshness from these."""
    return {"hosts": get_all_host_health(), "now": time.time()}


@app.get("/metrics/snapshot/{host_id}", dependencies=[Depends(require_api_key)])
def get_metrics_snapshot_route(host_id: str):
    """Latest rich detail snapshot for one host (per-core CPU, memory breakdown,
    per-interface network, per-mount disk, top processes) for the per-host
    metrics drill-down. Reported by the agent on heartbeat; read-only."""
    snap = get_host_snapshot(host_id)
    if not snap:
        return {"host_id": host_id, "ts": None, "snapshot": None}
    try:
        data = json.loads(snap["data"]) if snap.get("data") else None
    except (ValueError, TypeError):
        data = None
    return {"host_id": host_id, "ts": snap.get("ts"), "snapshot": data}


_CTRL_IDENTITY_CACHE = {"ts": 0.0, "val": None}
_CTRL_IDENTITY_TTL = float(os.getenv("SYSIBLE_CONTROLLER_IDENTITY_TTL", "300"))


def _controller_identity():
    """This controller host's own names + IPs, so an enrolled host that IS the
    controller can be labelled as such. Best-effort; empty sets never match.

    DNS-FREE and cached: it must NOT do a reverse-DNS lookup. `socket.getfqdn()`
    (used here originally) can block for SECONDS on a LAN without reverse records,
    and this runs on every GET /agents call — so it stalled every host-list
    refresh (the Set-Environment "took 5s to show up" regression). We use only the
    local hostname (a syscall, no DNS) plus NIC addresses, and cache the result
    for a TTL so repeated refreshes cost nothing. An enrolled controller still
    matches by IP even if only its short name is known."""
    now = time.time()
    cached = _CTRL_IDENTITY_CACHE["val"]
    if cached is not None and (now - _CTRL_IDENTITY_CACHE["ts"]) < _CTRL_IDENTITY_TTL:
        return cached
    import socket as _s
    names = set()
    try:
        hn = _s.gethostname() or ""      # local syscall — never a DNS lookup
        if hn:
            names.add(hn.lower()); names.add(hn.split(".")[0].lower())
    except Exception:
        pass
    names.discard("")
    ips = {"127.0.0.1", "::1"}
    try:
        ips.update(detect_local_ips())
    except Exception:
        pass
    # Also fold in the operator-configured controller address (the same identity
    # baked into agent bundles). The OS hostname / NIC-detected IPs can diverge
    # from how the controller is actually addressed — e.g. after a LAN renumber or
    # on a multi-homed box — which would stop the controller recognising its OWN
    # self-managed host (drawing it as a duplicate managed host in the topology).
    # DNS-free: this only reads the stored config row.
    try:
        from backend.db import get_controller_config
        _cfg = get_controller_config() or {}
        _chn = (_cfg.get("hostname") or "").strip().lower()
        if _chn:
            names.add(_chn); names.add(_chn.split(".")[0])
        _cip = (_cfg.get("ip") or "").strip()
        if _cip:
            ips.add(_cip)
    except Exception:
        pass
    val = (names, ips)
    _CTRL_IDENTITY_CACHE["val"] = val
    _CTRL_IDENTITY_CACHE["ts"] = now
    return val


def _is_controller_host(hostname, ip, names, ips):
    hn = (hostname or "").lower()
    if hn and (hn in names or hn.split(".")[0] in names):
        return True
    return bool(ip) and ip in ips


@app.get("/agents", dependencies=[Depends(require_api_key)])
def get_agents():

    from backend import agent_integrity
    agents = list_agents()
    ctrl_names, ctrl_ips = _controller_identity()
    # Load the SSH-terminal and integrity state for the WHOLE fleet once, then index
    # in-memory in the loop below. Previously each iteration re-read and re-parsed the
    # entire state file (get_agent_ssh_state / agent_integrity.status) — an O(N^2)
    # re-parse that made this hot dashboard route crawl at fleet scale.
    from backend.remote_routes import get_all_agent_ssh_states
    ssh_states = get_all_agent_ssh_states()
    integrity_states = agent_integrity.status_all()
    for a in agents:
        st = ssh_states.get(a.get("host_id"))
        # "enabled" | "pending" | "sshd_missing" | "error" | None
        a["ssh_terminal_state"] = (st or {}).get("status")
        # Agent integrity: let the console flag a host whose self-measurement
        # diverged from its sealed baseline ('revoked' already comes from
        # list_agents). integrity_detail carries the mismatch reasons for the UI.
        _ist = integrity_states.get(a.get("host_id")) or {"status": "unknown"}
        a["integrity_quarantined"] = _ist.get("status") == "quarantined"
        a["integrity_detail"] = _ist.get("mismatches", [])
        # Is THIS enrolled host the controller itself (self-managed)? Lets the
        # console label it so an operator never mistakes the control node for an
        # ordinary managed host.
        a["is_controller"] = _is_controller_host(a.get("hostname"), a.get("ip"), ctrl_names, ctrl_ips)

    return {
        "agents": agents
    }


# =========================================================
# DISENROLL
# =========================================================
@app.delete("/agents/{host_id}", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def remove_agent(host_id: str, purge_token: int = 0):
    """Remove an enrolled host from the controller. `purge_token=1` (Force Delete)
    also invalidates the enrollment token bound to this host, so a still-running
    zombie agent — whose token is baked into its sysible_agent.env — can't just
    re-enroll onto the same host_id and resurrect the record you just deleted."""

    # Look up the agent's identity BEFORE deleting it, so we can also clean up
    # the auto-created SSH record (register_agent_ssh_host) that would otherwise
    # linger in hosts.json as an orphan "Unassigned" host.
    agent = _find_agent(host_id)

    # Serialize delete + token purge against the enroll critical section
    # (_ENROLL_LOCK). Otherwise a zombie agent re-enrolling with the bound token
    # can interleave — validating the token and recreating the record AFTER
    # delete_agent runs — defeating the Force Delete it raced.
    tokens_purged = 0
    with _ENROLL_LOCK:
        delete_agent(host_id)
        if purge_token:
            from backend.db import invalidate_enroll_tokens_for_host
            tokens_purged = invalidate_enroll_tokens_for_host(host_id)
    # Forget the integrity baseline too — it's keyed by host_id and would
    # otherwise outlive the host, quarantining a future re-enroll on the same id.
    # (Enroll also re-seals, so this is belt-and-suspenders + state hygiene.)
    from backend import agent_integrity as _agent_integrity
    _agent_integrity.rebaseline(host_id)

    # Clean up the auto-created SSH record (matching hostname AND IP) — outside the
    # enroll lock since it only touches hosts.json, not the agent/token tables.
    _forget_ssh_for_deleted_agent(host_id, agent)

    return {
        "status": "removed",
        "host_id": host_id,
        "tokens_purged": tokens_purged,
    }


@app.post("/agents/{host_id}/revoke", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def revoke_agent_route(host_id: str,
                       x_admin_token: str = Header(default=None, alias="X-Sysible-Admin-Token")):
    """Hard lock-out: invalidate the host's agent secret so every authenticated
    request (heartbeat/poll/result) is rejected until an admin re-enrolls it with
    a fresh single-use token. This is the escalation from an integrity quarantine
    ('no tasks') to 'cannot talk to the controller at all'. The sealed integrity
    baseline is dropped too, so a re-enrolled/reimaged host re-seals cleanly."""
    if not agent_exists(host_id):
        raise HTTPException(status_code=404, detail="Unknown host_id")

    revoke_agent(host_id)
    from backend import agent_integrity
    agent_integrity.rebaseline(host_id)

    actor = None
    if x_admin_token:
        from backend.db import resolve_admin_token
        admin = resolve_admin_token(x_admin_token)
        actor = admin["username"] if admin else None
    log_admin_audit("agent_secret_revoked", actor or "superuser", f"host {host_id}")

    return {"status": "revoked", "host_id": host_id}


@app.post("/agents/{host_id}/resume", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def resume_agent_route(host_id: str,
                       x_admin_token: str = Header(default=None, alias="X-Sysible-Admin-Token")):
    """Clear an integrity QUARANTINE (the soft lockout): drop the sealed baseline
    so the next heartbeat re-seals from the host's current measurements and
    dispatch resumes. Use after a legitimate change (e.g. an agent upgrade) that
    tripped the check. Does NOT un-revoke a hard-revoked host — that needs a
    re-enroll."""
    if not agent_exists(host_id):
        raise HTTPException(status_code=404, detail="Unknown host_id")

    from backend import agent_integrity
    agent_integrity.rebaseline(host_id)

    actor = None
    if x_admin_token:
        from backend.db import resolve_admin_token
        admin = resolve_admin_token(x_admin_token)
        actor = admin["username"] if admin else None
    log_admin_audit("agent_integrity_resumed", actor or "superuser", f"host {host_id}")

    return {"status": "resumed", "host_id": host_id}


@app.post("/agents/{host_id}/restore", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def restore_agent_route(host_id: str,
                        x_admin_token: str = Header(default=None, alias="X-Sysible-Admin-Token")):
    """Undo a hard REVOCATION in place, keeping the existing agent secret so a
    still-installed agent resumes immediately — no re-enroll, host_id/history
    preserved. For a superuser who revoked a host by mistake (or has cleared
    whatever prompted it). Re-enrollment is only needed for a host that was
    reimaged and lost its secret."""
    if not agent_exists(host_id):
        raise HTTPException(status_code=404, detail="Unknown host_id")

    from backend.db import unrevoke_agent
    if not unrevoke_agent(host_id):
        raise HTTPException(status_code=404, detail="Unknown host_id")

    actor = None
    if x_admin_token:
        from backend.db import resolve_admin_token
        admin = resolve_admin_token(x_admin_token)
        actor = admin["username"] if admin else None
    log_admin_audit("agent_secret_restored", actor or "superuser", f"host {host_id}")

    return {"status": "restored", "host_id": host_id}


# =========================================================
# SELF-DISENROLL (agent-facing: authenticated by per-host secret, not
# the controller API key - this is what the agent bundle's
# disenroll_agent.sh calls so a host can clean itself up without
# needing admin credentials, mirroring how heartbeat/fetch_tasks
# already authenticate with host_id+agent_secret)
# =========================================================
@app.post("/agents/{host_id}/disenroll")
def self_disenroll(host_id: str, req: SelfDisenrollRequest):

    if host_id != req.host_id:
        raise HTTPException(status_code=400, detail="host_id mismatch")

    verify_agent(req.host_id, req.agent_secret)

    agent = _find_agent(req.host_id)

    # Serialize the delete against the enroll critical section (_ENROLL_LOCK),
    # same as the admin Force-Delete path: without it a zombie agent re-enrolling
    # with the same token can interleave and recreate the record right after
    # delete_agent runs, defeating the disenroll it raced.
    with _ENROLL_LOCK:
        delete_agent(req.host_id)

    _forget_ssh_for_deleted_agent(req.host_id, agent)

    # Forget the integrity baseline (keyed by host_id) so it can't quarantine a
    # future re-enroll on the same id. Enroll also re-seals — belt-and-suspenders.
    from backend import agent_integrity as _agent_integrity
    _agent_integrity.rebaseline(req.host_id)

    return {
        "status": "disenrolled",
        "host_id": req.host_id
    }


# =========================================================
# AGENT ENVIRONMENT TAGGING (admin-only - the agent itself never
# reports this, it's an operator-assigned label used to group hosts
# in the GUI across Host Enrollment / User Administration / Remote
# Administration)
# =========================================================
@app.post("/agents/{host_id}/environment", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def set_agent_environment_route(host_id: str, body: SetEnvironmentRequest):

    if not agent_exists(host_id):
        raise HTTPException(status_code=404, detail="Unknown host_id")

    set_agent_environment(host_id, body.environment)

    # Keep the agent's auto-created SSH record's environment in sync so the two
    # records don't diverge (e.g. show as assigned vs. Unassigned).
    agent = _find_agent(host_id)
    if agent:
        try:
            sync_agent_ssh_environment(agent.get("hostname"), agent.get("ip"), body.environment)
        except Exception:
            pass

    return {
        "host_id": host_id,
        "environment": body.environment
    }


@app.post("/agents/{host_id}/sudo-password-required",
          dependencies=[Depends(require_api_key), Depends(require_superuser)])
def set_agent_sudo_password_required_route(host_id: str, body: dict = Body(...)):
    """Mark whether this host forbids passwordless sudo. When required, the
    GUI supplies the operator's sudo password for dispatched commands and the
    agent elevates with `sudo -S`; when not, the agent uses `sudo -n`."""
    if not agent_exists(host_id):
        raise HTTPException(status_code=404, detail="Unknown host_id")
    required = bool(body.get("required"))
    set_agent_sudo_password_required(host_id, required)
    return {"host_id": host_id, "requires_sudo_password": required}


# =========================================================
# ENVIRONMENTS (dev/stage/prod, etc. - editable registry shared by
# agent hosts and SSH hosts; admin-only)
# =========================================================
@app.get("/environments", dependencies=[Depends(require_api_key)])
def get_environments():

    return {
        "environments": list_environments()
    }


@app.get("/environments/sudo-defaults", dependencies=[Depends(require_api_key)])
def get_environment_sudo_defaults():
    """{environment: bool} - which environments default to password-sudo, so
    hosts assigned to them inherit it."""
    return {"defaults": list_environment_sudo_defaults()}


@app.post("/environments/{name}/sudo-default",
          dependencies=[Depends(require_api_key), Depends(require_superuser)])
def set_environment_sudo_default_route(name: str, body: dict = Body(...)):
    required = bool(body.get("required"))
    set_environment_sudo_default(name, required)
    return {"environment": name, "requires_sudo_password": required}


@app.post("/environments", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def add_environment(body: CreateEnvironmentRequest):

    name = body.name.strip()

    if not name:
        raise HTTPException(status_code=400, detail="Environment name cannot be empty")

    # create_environment uses INSERT OR IGNORE, which silently no-ops on a
    # duplicate name and would report success — surface it as a clear 409 so the
    # operator knows the name is taken rather than believing a second one was
    # made (the per-environment sudo policy is keyed by name, so two "prod"s
    # would be indistinguishable).
    if any((e.get("name") if isinstance(e, dict) else e) == name for e in list_environments()):
        raise HTTPException(status_code=409, detail=f"An environment named '{name}' already exists.")

    create_environment(name)

    return {
        "environments": list_environments()
    }


@app.delete("/environments/{name}", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def remove_environment(name: str, force: int = 0):

    # Deleting an environment that still has hosts orphans them (their free-text
    # environment label points at nothing) and silently drops the environment's
    # sudo default. Refuse unless the caller explicitly forces it, telling them
    # how many hosts to reassign first. force=1 keeps the escape hatch.
    if not force:
        in_env = [a for a in list_agents() if (a.get("environment") or "") == name]
        try:
            from backend.remote_routes import load_hosts
            in_env += [n for n, h in load_hosts().items()
                       if (h.get("environment") or "") == name]
        except Exception:
            pass
        if in_env:
            raise HTTPException(
                status_code=409,
                detail=(f"{len(in_env)} host(s) are still in '{name}'. Reassign them "
                        f"to another environment first, or force deletion."))

    delete_environment(name)

    return {
        "environments": list_environments()
    }


# =========================================================
# CONTROLLER CONFIGURATION (admin-only)
# The hostname/port baked into agent bundles the Webserver Portal
# hands out - see backend/agent_bundle.py.
# =========================================================
@app.get("/version", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def version_route():
    """Which directory + git commit the live controller is actually running, and
    whether code on disk changed since start (pulled-but-not-restarted). Lets an
    operator confirm they're updating the directory the service really loads —
    the guard against silently updating a different checkout than the one running.

    Superuser-only: it discloses the absolute install path, exact commit, and PID —
    deployment internals that low-privilege (auditor/sysadmin) roles don't need and
    that would aid targeting."""
    from backend import build_info
    return build_info.info()


@app.get("/controller-config", dependencies=[Depends(require_api_key)])
def get_controller_config_route():

    return get_controller_config()


@app.post("/controller-config", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def set_controller_config_route(body: SetControllerConfigRequest):

    # IP-ONLY by design: agent bundles never bake in a hostname (that assumes DNS
    # is configured on every managed host). A legacy "hostname" mode is coerced to
    # "ip", and any hostname value is dropped.
    ip = body.ip.strip()
    hostname = ""
    address_mode = body.address_mode if body.address_mode in ("ip", "all") else "ip"

    if address_mode == "ip" and not ip:
        raise HTTPException(status_code=400, detail="Enter the controller's IP address (or choose 'all detected IPs').")

    if address_mode == "all" and not detect_local_ips():
        raise HTTPException(
            status_code=400,
            detail="No local IP addresses were detected on this controller - check its network interfaces.",
        )

    if not (1 <= body.port <= 65535):
        raise HTTPException(status_code=400, detail="Port must be between 1 and 65535")

    # Capture the address set BEFORE saving, so we can tell whether the controller's
    # address actually changed (and thus whether the self-signed cert's SAN is now
    # stale) rather than regenerating on every unrelated save.
    try:
        old_addrs = set(resolve_controller_addresses(get_controller_config()))
    except Exception:
        old_addrs = set()

    set_controller_config(hostname, ip, address_mode, body.port)
    result = get_controller_config()

    # When the address changes, the default self-signed cert's SAN no longer matches,
    # so agents/clients that verify the pinned cert would hit a hostname mismatch.
    # Regenerate it to cover the new address. NEVER regenerate over an imported PKI
    # cert — the admin owns that one (and its SAN); just flag that it may need
    # reissuing. uvicorn reads the cert once at startup, so a restart is required to
    # actually serve the new cert (surfaced to the UI, not forced here).
    try:
        from backend import tls_manager
        new_addrs = set(resolve_controller_addresses(result))
        if new_addrs and new_addrs != old_addrs:
            if tls_manager.current_is_self_signed():
                # Do NOT regenerate the live cert here. uvicorn only rereads its
                # cert on restart, so overwriting the served cert/trust now —
                # while the OLD cert is still being served — leaves every TLS
                # client (including this console over loopback, and the client
                # that pins server.crt) verifying the still-served OLD cert
                # against the just-written NEW one. That fails the handshake
                # (HTTPSConnectionPool / SSL error) and the controller becomes
                # unreachable until a manual restart. Instead just flag it: the
                # "Regenerate self-signed cert" action reissues for the new
                # address AND restarts atomically, so the served cert and the
                # pinned trust move together.
                result["cert_stale"] = True
                result["cert_note"] = (
                    "The controller address changed, so the self-signed TLS certificate "
                    "no longer matches it. Click “Regenerate self-signed cert” to "
                    "reissue it for the new address and restart the controller, then "
                    "redistribute the trust bundle / re-download the agent bundle so "
                    "hosts trust and reach it."
                )
            else:
                result["cert_note"] = (
                    "The controller address changed, but a custom (PKI) certificate is "
                    "installed and was left untouched — make sure its SAN covers the new "
                    "address, or import an updated certificate."
                )
    except Exception as e:
        result["cert_warning"] = f"Could not evaluate the TLS certificate: {e}"

    return result


@app.get("/license-config", dependencies=[Depends(require_api_key)])
def get_license_config_route():

    return get_license_config()


@app.post("/license-config", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def set_license_config_route(body: SetLicenseKeyRequest):

    return set_license_config(body.license_key.strip())


@app.get("/controller-config/local-ips", dependencies=[Depends(require_api_key)])
def get_local_ips_route():
    """Every non-loopback IPv4 address found on this controller right
    now - powers the IP picker/"All Detected IPs" option in Controller
    Configuration so the admin never has to run `ip addr`/`ifconfig` by
    hand."""

    return {"ips": detect_local_ips()}


@app.get("/controller-config/agent-bundle", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def download_agent_bundle_route():
    """Build and hand back a ready-to-run agent bundle straight from the
    admin GUI - the same zip the Webserver Portal hands a remote host
    operator (see backend/portal_app.py's /files/bundle), generated here
    so one's available even when the portal isn't running, or isn't
    reachable from wherever this GUI happens to be."""

    config = get_controller_config()
    addresses = resolve_controller_addresses(config)

    if not addresses:
        detail = (
            "No local IP addresses were detected on this controller - check its network interfaces."
            if config.get("address_mode") == "all"
            else "Set a Hostname or IP Address above and save before downloading a bundle."
        )
        raise HTTPException(status_code=400, detail=detail)

    filename, zip_bytes = mint_agent_bundle(addresses, config["port"])

    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # Rebuilt from current config each call — never let a client cache it.
            "Cache-Control": "no-store",
        },
    )


@app.get("/self-enroll-bundle", dependencies=[Depends(require_api_key)])
def self_enroll_bundle_route():
    """Bundle for enrolling the CONTROLLER ITSELF as a managed host, always built
    against loopback (127.0.0.1) rather than the configured LAN address. The
    controller's own agent runs on the same box, so loopback is the most reliable
    target and — crucially — works on a fresh install BEFORE any hostname/IP has
    been configured (the self-signed cert is always scoped to 127.0.0.1). Used by
    `sysible_controller self-enroll`. Same single-use-token minting and host-cap
    enforcement as every other bundle (mint_agent_bundle)."""
    port = (get_controller_config() or {}).get("port") or 9000
    filename, zip_bytes = mint_agent_bundle(["127.0.0.1"], port)
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


# =========================================================
# TLS CERTIFICATE (admin-only)
# Lets an admin swap the controller's self-signed cert for one issued
# by an external PKI team. See backend/tls_manager.py for the actual
# parse/validate/install/restart logic - these routes just wire it up
# to HTTP the same multipart-upload way remote_routes.py's
# /hosts/{name}/files/upload route already does.
# =========================================================
@app.get("/controller-config/tls/info", dependencies=[Depends(require_api_key)])
def get_tls_info_route():
    """Metadata about whatever cert is currently in front of uvicorn -
    never returns key material."""

    return tls_manager.get_tls_info()


@app.post("/controller-config/tls/install", dependencies=[Depends(require_api_key), Depends(require_superuser)])
async def install_tls_certificate_route(
    cert_file: UploadFile = File(...),
    key_file: UploadFile = File(...),
    chain_file: Optional[UploadFile] = File(None),
):
    """Validates the uploaded cert/key(/chain), installs them as
    server.crt/server.key/trust.crt, then restarts the backend so
    uvicorn actually picks up the new cert - it only reads
    --ssl-certfile/--ssl-keyfile once, at process start, there's no
    dynamic reload. The restart is deliberately delayed a couple
    seconds (see tls_manager.restart_backend) so this response makes it
    back to the GUI first; the GUI itself is expected to suppress its
    backend watchdog for a few seconds around this call (see
    client/events.py's backend_restart_expected signal) so a deliberate
    restart doesn't get mistaken for a crash."""

    # Cap each upload: a PEM cert/key/chain is a few KB, so a 1 MB ceiling is
    # generous. Reading with an explicit bound (read(cap+1)) means a multi-GB body
    # can't be buffered into RAM before we reject it -- matching the portal/SSH
    # upload paths, which are already capped. (Superuser-gated, so this is DoS
    # defence-in-depth, not a privilege boundary.)
    _CERT_UPLOAD_MAX = 1024 * 1024

    async def _read_capped(upload, label):
        if upload is None:
            return None
        data = await upload.read(_CERT_UPLOAD_MAX + 1)
        if len(data) > _CERT_UPLOAD_MAX:
            raise HTTPException(status_code=413, detail=f"{label} is too large (max 1 MB).")
        return data

    cert_pem = await _read_capped(cert_file, "Certificate")
    key_pem = await _read_capped(key_file, "Private key")
    chain_pem = await _read_capped(chain_file, "Chain")

    try:
        info = tls_manager.install_certificate(cert_pem, key_pem, chain_pem or None)
    except tls_manager.TLSValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    tls_manager.restart_backend()

    return {
        **info,
        "restarting": True,
        "message": "Certificate installed. The backend is restarting now to apply it.",
    }


@app.post("/controller-config/tls/regenerate-self-signed",
          dependencies=[Depends(require_api_key), Depends(require_superuser)])
def regenerate_self_signed_route():
    """Regenerate the self-signed TLS certificate ON DEMAND so its SAN matches the
    controller's CURRENT hostname/IP, then restart the backend to serve it. Use this
    when the address was changed (so agents that verify the pinned cert stop hitting
    a hostname mismatch) — unlike the automatic regen on an address change, this
    works even when the config is already saved at the new value. Refuses to clobber
    an imported PKI certificate (regenerate that from your CA instead)."""
    if not tls_manager.current_is_self_signed():
        raise HTTPException(
            status_code=400,
            detail="A custom (PKI) certificate is installed — regenerating a self-signed "
                   "cert would replace it. Import an updated PKI cert instead.",
        )
    addresses = resolve_controller_addresses(get_controller_config())
    if not addresses:
        raise HTTPException(
            status_code=400,
            detail="Set the controller Hostname/IP first so the certificate can cover it.",
        )
    info = tls_manager.regenerate_self_signed(hostnames=addresses)
    tls_manager.restart_backend()
    return {
        **info,
        "restarting": True,
        "addresses": addresses,
        "message": ("Self-signed certificate regenerated for the current address. The "
                    "backend is restarting to serve it — then redistribute the trust "
                    "bundle / re-download the agent bundle so hosts trust and reach it."),
    }


@app.post("/controller/restart", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def controller_restart_route():
    """Restart the controller backend service (systemctl restart sysible-backend)
    from the web console, so an admin can bounce it without shell access.
    Superuser-only.

    Unlike a self-update this touches only the backend service, not the web
    console (a separate service), so the operator's session survives — API calls
    just blip for a few seconds while it comes back (systemd Restart=always). The
    restart is fired ~1.5s later on a background thread (see tls_manager) so this
    response flushes to the caller before uvicorn is killed."""
    from backend import tls_manager
    tls_manager.restart_backend()
    return {"restarting": True, "service": "sysible-backend"}


@app.post("/controller/update", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def controller_update_route(acting: str = Depends(acting_admin_name)):
    """Trigger an in-place controller self-update: the same work as the CLI
    `sysible_controller update` (git pull -> rsync into /opt/sysible -> rebuild
    the web console -> restart services). Superuser-only.

    The update restarts BOTH the backend and the web console, so it must NOT run
    as a child of either service: a `systemctl restart` kills the whole service
    cgroup and would abort the update mid-flight. We launch it as a *transient*
    systemd unit (systemd-run --collect), which gets its own cgroup and survives
    the restarts, and we return immediately so this response reaches the caller
    before anything bounces. Progress is in `journalctl -u sysible-self-update`."""
    import os
    import shutil
    import subprocess

    cli = shutil.which("sysible_controller") or "/usr/local/bin/sysible_controller"
    if not os.path.exists(cli):
        raise HTTPException(status_code=500, detail="Controller CLI not found on this host.")

    unit = "sysible-self-update"
    # Don't stack updates - if one is already in flight, say so.
    try:
        active = subprocess.run(
            ["systemctl", "is-active", f"{unit}.service"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if active == "active":
            return {"status": "already-running", "message": "An update is already in progress."}
    except Exception:
        pass

    if not shutil.which("systemd-run"):
        raise HTTPException(
            status_code=500,
            detail="systemd-run is unavailable; run 'sudo sysible_controller update' on the host instead.",
        )

    # --collect reaps the transient unit after it exits so the next update can
    # reuse the name. The update itself re-execs the services from a clean cgroup.
    cmd = ["systemd-run", "--collect", f"--unit={unit}",
           "--description=Sysible Controller self-update", cli, "update"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip() or "failed to launch updater"
        raise HTTPException(status_code=500, detail=f"Could not start update: {detail}")

    # Production code change — record it in the audit trail, attributed to the
    # superuser who triggered it (the agent-fleet update is logged too).
    try:
        log_admin_audit("controller_update_started", acting,
                        "in-place controller self-update triggered")
    except Exception:
        pass

    return {
        "status": "started",
        "message": "Update started. The controller and web console will restart shortly — "
                   "you'll be logged out. Wait ~30–60s, then hard-refresh and sign back in.",
    }


@app.post("/controller/webgui/rebuild", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def controller_webgui_rebuild_route(acting: str = Depends(acting_admin_name)):
    """Rebuild the web console front end (npm install + npm run build) from the browser,
    so an admin never needs shell access after pulling new code. The console serves a
    COMPILED bundle (webgui/frontend/dist, not tracked in git), so source changes only
    show once it's rebuilt — the in-app 'Update controller' already does this; this is the
    same build on demand (e.g. after a manual git pull that restarted only the backend).
    Superuser-only. Returns each step's output so the UI can show exactly what happened."""
    from backend import webgui_manager
    result = webgui_manager.install_dependencies()
    try:
        log_admin_audit("controller_update_started", acting,
                        "web console front end rebuilt from the console")
    except Exception:
        pass
    return result


@app.get("/controller/update-status", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def controller_update_status_route():
    """Report the outcome of the most recent controller self-update, so the
    'Update controller' buttons can show the TRUTH (updated to X / failed:
    reason) instead of guessing from whether the console bounced. The updater
    (sysible_controller cmd_update) writes run/last_update.json at each stage;
    we read it back here. Returns status 'none' if no update has ever run."""
    import json
    import time as _t
    from pathlib import Path

    status_file = Path(__file__).resolve().parent.parent / "run" / "last_update.json"
    try:
        rec = json.loads(status_file.read_text())
    except FileNotFoundError:
        return {"status": "none"}
    except Exception as e:
        return {"status": "unknown", "message": f"could not read update status: {e}"}
    ts = rec.get("ts")
    if isinstance(ts, (int, float)):
        rec["age_seconds"] = max(0, int(_t.time() - ts))
    return rec


def _read_update_log(lines=0):
    """The controller self-update's output. Prefers the persistent file the
    updater tees to (run/last_update.log, one run's full output); falls back to
    the systemd journal of the transient unit. `lines` > 0 returns only the tail."""
    from pathlib import Path
    logf = Path(__file__).resolve().parent.parent / "run" / "last_update.log"
    try:
        if logf.exists():
            text = logf.read_text(errors="replace")
            if lines and lines > 0:
                rows = text.splitlines()
                if len(rows) > lines:
                    text = "\n".join(rows[-lines:])
            return text
    except Exception:
        pass
    import subprocess
    n = str(max(1, min(int(lines or 5000), 20000)))
    try:
        proc = subprocess.run(
            ["journalctl", "-u", "sysible-self-update", "-n", n, "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=10,
        )
        out = proc.stdout
        if proc.returncode != 0 and not out:
            out = f"(no update output yet: {proc.stderr.strip() or 'journalctl returned nothing'})"
        return out
    except FileNotFoundError:
        return "(journalctl not available on this host)"
    except subprocess.TimeoutExpired:
        return "(timed out reading the update log)"


@app.get("/controller/update-log", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def controller_update_log_route(lines: int = 400):
    """Output of the controller self-update (git pull → rsync → rebuild →
    restart) so the UI can stream the commands running and offer a download.
    Superuser-only. `lines` limits to the tail for the live console; pass 0 for
    the full log (download)."""
    return {"log": _read_update_log(lines)}


@app.get("/controller-config/tls/trust-bundle", dependencies=[Depends(require_api_key)])
def download_trust_bundle_route():
    """Current trust.crt content - what an admin hands to GUI
    machines/agents that were enrolled before this cert was installed,
    so they can refresh their pinned copy by hand (new agent bundles
    pick this up automatically already - see backend/agent_bundle.py)."""

    try:
        data = tls_manager.trust_bundle_bytes()
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return Response(
        content=data,
        media_type="application/x-pem-file",
        headers={"Content-Disposition": 'attachment; filename="trust.crt"'},
    )


# =========================================================
# ENVIRONMENTAL POLICIES (admin-only)
# Baseline settings for accounts/sudo/umask on managed target hosts -
# System Administration > Environmental Policies pushes these to
# checked hosts, and User & Group Administration uses the password
# sub-object as the baseline for its own Generate Password / Set
# Password validation (see client/api.py's check_password_strength).
# Not to be confused with admin-password-policy below, which only
# governs this controller's own GUI-login administrator accounts.
# =========================================================
@app.get("/environmental-policy", dependencies=[Depends(require_api_key)])
def get_environmental_policy_route():

    return get_environmental_policy()


@app.post("/environmental-policy", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def set_environmental_policy_route(body: SetEnvironmentalPolicyRequest):

    policy = body.model_dump()
    set_environmental_policy(policy)

    return policy


# =========================================================
# WEBSERVER PORTAL (admin-only)
# Start/stop the separate portal process and manage its login
# credentials - see backend/portal_manager.py and backend/portal_app.py
# for why this is a standalone process rather than routes on this app.
# =========================================================
@app.get("/portal/status", dependencies=[Depends(require_api_key)])
def get_portal_status_route():

    creds = get_portal_credentials()
    last_login = get_last_portal_login()

    status = portal_manager.status()
    status["credentials_configured"] = bool(creds and creds.get("username"))
    status["username"] = creds.get("username") if creds else None
    status["last_changed"] = creds.get("last_changed") if creds else None
    status["last_login"] = last_login

    return status


@app.post("/portal/start", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def start_portal_route():

    return portal_manager.start()


@app.post("/portal/stop", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def stop_portal_route():

    return portal_manager.stop()


@app.post("/portal/credentials", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def set_portal_credentials_route(body: SetPortalCredentialsRequest):

    username = body.username.strip()

    if not username:
        raise HTTPException(status_code=400, detail="Username cannot be empty")

    if not body.password:
        raise HTTPException(status_code=400, detail="Password cannot be empty")

    existing = get_portal_credentials()

    # Only enforced once credentials actually exist - the very first
    # time they're set (fresh install) there's nothing yet to confirm
    # against.
    if existing and existing.get("username"):
        if not body.current_password:
            raise HTTPException(status_code=400, detail="Current password is required to reset credentials")

        if not portal_auth.verify_password(
            body.current_password, existing.get("password_salt"), existing.get("password_hash")
        ):
            raise HTTPException(status_code=400, detail="Current password is incorrect")

    salt, password_hash = portal_auth.hash_password(body.password)
    set_portal_credentials(username, password_hash, salt)

    # Every session issued under the old password is invalidated -
    # otherwise a host operator logged in under credentials that were
    # just reset (e.g. because they were thought to be compromised)
    # could keep using the portal until their cookie's TTL ran out.
    delete_all_portal_sessions()

    log_portal_event("credentials_changed", username)

    return {"username": username, "status": "updated"}


@app.delete("/portal/credentials", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def remove_portal_credentials_route(body: RemovePortalCredentialsRequest):

    existing = get_portal_credentials()

    if not existing or not existing.get("username"):
        raise HTTPException(status_code=400, detail="No login credentials are configured")

    if not portal_auth.verify_password(
        body.current_password, existing.get("password_salt"), existing.get("password_hash")
    ):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    removed_username = existing.get("username")

    delete_portal_credentials()

    # No account left to hold a session against - end every session
    # issued under it, same as a credentials reset.
    delete_all_portal_sessions()

    log_portal_event("credentials_removed", removed_username)

    return {"status": "removed"}


@app.get("/portal/login-history", dependencies=[Depends(require_api_key)])
def get_portal_login_history_route(limit: int = 200):
    return {"history": get_portal_login_history(_clamp_limit(limit))}


@app.get("/portal/sessions", dependencies=[Depends(require_api_key)])
def list_portal_sessions_route():
    return {"sessions": list_portal_sessions()}


@app.post("/portal/sessions/{session_id}/revoke", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def revoke_portal_session_route(session_id: int):
    delete_portal_session(session_id)
    return {"status": "revoked"}


# =========================================================
# ADMINISTRATORS (gates the web console itself - the "Sysible
# Administrator Configuration" page. Separate from portal_credentials
# above, which is what a remote host *operator* logs into in a
# browser, not what a Sysible admin uses. All routes still require
# the admin API key, same as every other route in this section -
# this isn't instead of that trust boundary, it's an additional
# human-facing gate on top of it, since the API key alone just
# proves "this is a legitimate Sysible GUI install", not "the person
# currently sitting at it is allowed to use it".
#
# Multiple named administrator accounts, each with their own
# password - replaces the old single shared admin/admin login.
# Account changes and logins are recorded to admin_audit_log; see
# GET /admin/audit-log below.
# =========================================================
@app.get("/admin/setup-required", dependencies=[Depends(require_api_key)])
def admin_setup_required():
    """True on a fresh install where no administrator exists yet - the GUI
    uses this to show a "create your administrator account" screen instead
    of a login screen (there is no default account to log in with)."""
    return {"setup_required": count_administrators() == 0}


@app.post("/admin/setup", dependencies=[Depends(require_api_key)])
def admin_setup(body: AdminSetupRequest):
    """Create the first administrator. Only allowed while the
    administrators table is still empty, so it can't be used to bypass the
    authenticated add-administrator flow once an account exists. The
    operator chooses their own password here (must_change_password=0) -
    there is no default to change."""
    if count_administrators() != 0:
        raise HTTPException(status_code=409, detail="An administrator already exists")

    username = body.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username cannot be empty")
    if not body.password:
        raise HTTPException(status_code=400, detail="Password cannot be empty")

    ok, message = validate_password_against_policy(body.password, get_admin_password_policy())
    if not ok:
        raise HTTPException(status_code=400, detail=message)

    salt, password_hash = portal_auth.hash_password(body.password)
    # The first account is always a superuser - someone has to be able to
    # manage the controller, and there's no one to grant the role yet.
    created = add_administrator(
        username, password_hash, salt, must_change_password=0, created_by="setup",
        role="superuser",
    )
    if not created:
        raise HTTPException(status_code=409, detail="An administrator with that username already exists")

    log_admin_audit("administrator_added", username, "first-run setup (superuser)")

    # Creating the first admin also logs them in: mint the same RBAC identity
    # token /admin/login issues, so the GUI can immediately perform superuser
    # actions (controller config, host enrollment, ...) without a redundant
    # log-out/log-in. Without this the client entered the dashboard with no
    # token and every superuser call 401'd until the operator re-authenticated.
    role = "superuser"
    token = secrets.token_hex(32)
    create_admin_token(token, username, role, time.time() + 12 * 60 * 60)
    record_administrator_login(username)

    return {"username": username, "status": "created", "role": role, "token": token}


# Defense-in-depth brute-force throttle for the GUI admin login. This
# endpoint is already behind the API key (so it isn't a remote guessing
# target without that secret), but a lenient lockout adds a second layer at no
# real usability cost. Backed by the DB (login_throttle table) so the lockout
# SURVIVES a controller restart / crash-loop — an attacker can no longer reset
# accumulated failures by bouncing the process.
_ADMIN_LOGIN_MAX_FAILURES = 10
_ADMIN_LOGIN_WINDOW_S = 15 * 60
_ADMIN_LOGIN_LOCKOUT_S = 10 * 60

# Decoy credential: when the submitted username doesn't exist we still run a full
# PBKDF2 verify against this fixed hash so the response time is the same as for a
# real account — otherwise the short-circuited (fast) miss is a username-enumeration
# timing oracle. Generated once per process from a random password (never matches).
_DECOY_SALT, _DECOY_HASH = portal_auth.hash_password(secrets.token_hex(16))


def _admin_login_locked_for(key):
    from backend import db
    try:
        return db.login_throttle_locked_for(key)
    except Exception:
        return 0  # never let a throttle-store hiccup block a legitimate login


def _admin_login_record_failure(key):
    from backend import db
    try:
        db.login_throttle_record_failure(
            key, _ADMIN_LOGIN_WINDOW_S, _ADMIN_LOGIN_MAX_FAILURES, _ADMIN_LOGIN_LOCKOUT_S)
    except Exception:
        pass


def _admin_login_clear(key):
    from backend import db
    try:
        db.login_throttle_clear(key)
    except Exception:
        pass


@app.post("/admin/login", dependencies=[Depends(require_api_key)])
def admin_login(body: AdminLoginRequest, request: Request):

    username = body.username.strip()

    # Throttle per USERNAME, not per client IP. Every console login reaches the
    # controller from the single BFF process, so an IP key lumped all admins into
    # one bucket — 10 failures locked out the whole console, and one account's
    # failures locked others. Per-username gives each account its own lockout;
    # volumetric abuse is separately bounded by the BFF's per-real-client-IP
    # throttle (webgui/server.py) and the root-only API key.
    throttle_key = username or "(empty)"

    locked = _admin_login_locked_for(throttle_key)
    if locked:
        log_admin_audit("login_throttled", username, f"locked {locked}s")
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in about {max(1, locked // 60)} minute(s).",
        )

    admin = get_administrator(username)

    if admin is not None:
        valid = portal_auth.verify_password(
            body.password, admin["password_salt"], admin["password_hash"])
    else:
        # Decoy verify so a nonexistent username costs the same PBKDF2 time as a
        # real one (closes the username-enumeration timing oracle).
        portal_auth.verify_password(body.password, _DECOY_SALT, _DECOY_HASH)
        valid = False

    if not valid:
        _admin_login_record_failure(throttle_key)
        log_admin_audit("login_failed", username, "Invalid username or password")
        raise HTTPException(status_code=401, detail="Invalid username or password")

    # Transparently upgrade a legacy/under-cost password hash to the current PBKDF2
    # cost now that we hold the plaintext and it verified — so no under-cost hash
    # (and the enumeration timing asymmetry it creates vs the fixed-cost decoy) lingers.
    if admin is not None and portal_auth.needs_rehash(admin.get("password_hash")):
        try:
            _salt, _hash = portal_auth.hash_password(body.password)
            update_administrator_password(username, _hash, _salt,
                                          must_change_password=admin.get("must_change_password", 0))
        except Exception:
            pass

    _admin_login_clear(throttle_key)
    record_administrator_login(username)
    log_admin_audit("login_success", username, "")

    # Issue an identity token bound to this admin. The client sends it back
    # on subsequent requests so dispatch can tag tasks with an unforgeable
    # initiating username (see queue_agent_task). 12-hour lifetime.
    role = admin.get("role") or "superuser"
    token = secrets.token_hex(32)
    create_admin_token(token, username, role, time.time() + 12 * 60 * 60)

    return {
        "status": "ok",
        "username": username,
        "role": role,
        "token": token,
        "must_change_password": bool(admin["must_change_password"]),
        # Per-admin opt-in for the Sysible Connect terminal's "Send sudo
        # password" button (granted by a superuser). Both front ends read this.
        "sudo_connect": bool(admin.get("sudo_connect")),
    }


@app.get("/admin/whoami", dependencies=[Depends(require_api_key)])
def admin_whoami(request: Request):
    """Resolve the caller's admin token to its LIVE identity/role, or 401 if the
    token has been revoked (account removed, role changed, expired). The web
    console calls this periodically so a demoted or removed admin's session is
    dropped promptly instead of coasting on a still-signed cookie until the 12h
    token expiry. resolve_admin_token does the live-account cross-check."""
    token = request.headers.get("X-Sysible-Admin-Token")
    admin = resolve_admin_token(token) if token else None
    if not admin:
        raise HTTPException(status_code=401, detail="Invalid or expired admin token")
    return {"username": admin["username"], "role": admin["role"]}


@app.post("/admin/logout", dependencies=[Depends(require_api_key)])
def admin_logout(request: Request):
    """Invalidate the caller's RBAC identity token so it can't be reused after
    they log out. Best-effort and idempotent: a missing/unknown token is a
    no-op (the client clears its own copy regardless). The admin's account is
    untouched - this only revokes the current session's token."""
    token = request.headers.get("X-Sysible-Admin-Token")
    username = ""
    if token:
        admin = resolve_admin_token(token)
        if admin:
            username = admin.get("username", "")
        delete_admin_token(token)
    if username:
        log_admin_audit("logout", username, "")
    return {"status": "ok"}


@app.get("/admin/administrators", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def list_administrators_route():
    """Username, account metadata only - never password hash/salt.
    Superuser-gated: the administrator roster is a superuser surface (a
    sysadmin has no business enumerating the other admins and their roles)."""

    return {"administrators": list_administrators()}


@app.post("/admin/administrators", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def add_administrator_route(body: AddAdministratorRequest, acting: str = Depends(acting_admin_name)):
    from backend.edition import enforce_role_limit
    from backend.db import count_administrators_by_role

    username = body.username.strip()

    if not username:
        raise HTTPException(status_code=400, detail="Username cannot be empty")

    if not body.password:
        raise HTTPException(status_code=400, detail="Password cannot be empty")

    role = (body.role or "sysadmin").strip().lower()
    if role not in ("superuser", "sysadmin", "auditor"):
        raise HTTPException(status_code=400, detail="Role must be 'superuser', 'sysadmin', or 'auditor'")

    # Edition seat cap (Community: 2 superusers, 5 sysadmins; 'auditor' is
    # read-only and uncapped — enforce_role_limit no-ops for unknown roles).
    enforce_role_limit(role, count_administrators_by_role(role))

    ok, message = validate_password_against_policy(body.password, get_admin_password_policy())
    if not ok:
        raise HTTPException(status_code=400, detail=message)

    salt, password_hash = portal_auth.hash_password(body.password)
    created = add_administrator(
        username, password_hash, salt, must_change_password=1,
        created_by=acting, role=role,
    )

    if not created:
        raise HTTPException(status_code=409, detail="An administrator with that username already exists")

    log_admin_audit("administrator_added", username, f"{role} added by {acting}")

    return {"username": username, "status": "added", "role": role}


@app.delete("/admin/administrators/{username}", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def remove_administrator_route(username: str, acting: str = Depends(acting_admin_name)):

    target = get_administrator(username)
    if target is None:
        raise HTTPException(status_code=404, detail="No such administrator")

    if count_administrators() <= 1:
        raise HTTPException(status_code=400, detail="Cannot remove the last remaining administrator")

    # Deleting the LAST superuser leaves nobody who can manage admins, enroll/
    # remove hosts, change controller config, or run the portal — the same
    # lockout the role-demote path already refuses. The total-admin check above
    # doesn't catch it (sysadmins/auditors can still exist), so guard the role
    # count explicitly. Recovery would otherwise require `sysible_controller
    # reset-admin` on the host console.
    if (target.get("role") or "superuser") == "superuser" and \
            count_administrators_by_role("superuser") <= 1:
        raise HTTPException(
            status_code=400,
            detail="Cannot remove the last remaining superuser — promote another "
                   "administrator to superuser first.")

    remove_administrator(username)
    log_admin_audit("administrator_removed", username, f"removed by {acting}")

    return {"username": username, "status": "removed"}


@app.post("/admin/administrators/{username}/sudo-connect",
          dependencies=[Depends(require_api_key), Depends(require_superuser)])
def set_administrator_sudo_connect_route(username: str, body: SetSudoConnectRequest,
                                         acting: str = Depends(acting_admin_name)):
    """Superuser-gated: grant or revoke an administrator's access to the
    Sysible Connect terminal's "Send sudo password" button. Off by default;
    applies to every account (a superuser can grant their own)."""
    if get_administrator(username) is None:
        raise HTTPException(status_code=404, detail="No such administrator")

    set_administrator_sudo_connect(username, body.allowed)
    log_admin_audit(
        "sudo_connect_set", username,
        f"{'granted' if body.allowed else 'revoked'} by {acting}")

    return {"username": username, "sudo_connect": bool(body.allowed)}


@app.post("/admin/administrators/{username}/role",
          dependencies=[Depends(require_api_key), Depends(require_superuser)])
def set_administrator_role_route(username: str, body: SetRoleRequest,
                                 acting: str = Depends(acting_admin_name)):
    """Superuser-gated promote/demote of an administrator's role
    ('superuser' | 'sysadmin' | 'auditor'). Guards:
      - refuse to demote the LAST superuser (would lock everyone out of admin
        management, controller config, the portal, ...);
      - honour the edition seat caps when promoting into a capped role.
    The role takes full effect at the target admin's next login (their current
    token keeps its old role until it expires / they re-log in)."""
    from backend.edition import enforce_role_limit

    admin = get_administrator(username)
    if admin is None:
        raise HTTPException(status_code=404, detail="No such administrator")

    new_role = (body.role or "").strip().lower()
    if new_role not in ("superuser", "sysadmin", "auditor"):
        raise HTTPException(status_code=400, detail="Role must be 'superuser', 'sysadmin', or 'auditor'")

    current = (admin.get("role") or "superuser")
    if new_role == current:
        return {"username": username, "role": new_role, "status": "unchanged"}

    if current == "superuser" and count_administrators_by_role("superuser") <= 1:
        raise HTTPException(status_code=400, detail="Cannot change the role of the last remaining superuser.")

    # Edition seat cap on the destination role (auditor is uncapped -> no-op).
    enforce_role_limit(new_role, count_administrators_by_role(new_role))

    set_administrator_role(username, new_role)
    # Revoke the target's live sessions so the new role takes effect immediately
    # (a demoted superuser must not keep superuser powers on their old token).
    delete_admin_tokens_for_user(username)
    log_admin_audit("administrator_role_changed", username,
                    f"{current} -> {new_role} by {acting}")
    return {"username": username, "role": new_role, "status": "updated"}


@app.post("/admin/credentials", dependencies=[Depends(require_api_key)])
def change_admin_credentials(body: ChangeAdminCredentialsRequest):
    """Self-service username/password change for the currently
    logged-in administrator named in body.username."""

    admin = get_administrator(body.username)

    if admin is None:
        raise HTTPException(status_code=404, detail="No such administrator")

    if not portal_auth.verify_password(
        body.current_password, admin["password_salt"], admin["password_hash"]
    ):
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    # Both fields are optional (the UI labels them so): a blank new username keeps
    # the current one, a blank new password keeps the current one. This lets an
    # admin rename WITHOUT also setting a new password (which used to be rejected
    # with "Password cannot be empty", making a username-only change impossible).
    new_username = body.new_username.strip() or body.username

    changed = False
    if new_username != body.username:
        renamed = update_administrator_username(body.username, new_username)
        if not renamed:
            raise HTTPException(status_code=409, detail="An administrator with that username already exists")
        changed = True

    if body.new_password:
        ok, message = validate_password_against_policy(body.new_password, get_admin_password_policy())
        if not ok:
            raise HTTPException(status_code=400, detail=message)
        salt, password_hash = portal_auth.hash_password(body.new_password)
        update_administrator_password(new_username, password_hash, salt, must_change_password=0)
        changed = True

    if not changed:
        raise HTTPException(status_code=400, detail="Enter a new username or a new password to change.")

    # Invalidate every live admin token for this account after a credential change,
    # matching the admin-reset path — a password change must not leave other
    # already-issued sessions valid (resolve_admin_token can't retroactively catch
    # them). The caller re-authenticates with the new credentials on their next call.
    delete_admin_tokens_for_user(body.username)
    if new_username != body.username:
        delete_admin_tokens_for_user(new_username)

    log_admin_audit("credentials_changed", new_username, "self-service username/password change")

    return {"username": new_username, "status": "updated"}


@app.post("/admin/force-password-change", dependencies=[Depends(require_api_key)])
def force_admin_password_change(body: ForcePasswordChangeRequest):
    """Used right after login when must_change_password is set -
    same verification as change_admin_credentials but no username
    change, and clears the forced-change flag."""

    admin = get_administrator(body.username)

    if admin is None:
        raise HTTPException(status_code=404, detail="No such administrator")

    if not portal_auth.verify_password(
        body.current_password, admin["password_salt"], admin["password_hash"]
    ):
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    if not body.new_password:
        raise HTTPException(status_code=400, detail="Password cannot be empty")

    ok, message = validate_password_against_policy(body.new_password, get_admin_password_policy())
    if not ok:
        raise HTTPException(status_code=400, detail=message)

    salt, password_hash = portal_auth.hash_password(body.new_password)
    update_administrator_password(body.username, password_hash, salt, must_change_password=0)

    # Drop any tokens issued against the temporary/expired credential so the forced
    # change can't leave a pre-rotation session valid — the admin re-logs in with the
    # new password (matching the reset path).
    delete_admin_tokens_for_user(body.username)

    log_admin_audit("forced_password_change_completed", body.username, "")

    return {"username": body.username, "status": "updated"}


@app.post("/admin/administrators/{username}/password",
          dependencies=[Depends(require_api_key), Depends(require_superuser)])
def reset_administrator_password_route(username: str, body: ResetAdministratorPasswordRequest,
                                       acting: str = Depends(acting_admin_name)):
    """Superuser resets ANOTHER administrator's password without knowing the
    target's current password. The target must change it on next login."""

    admin = get_administrator(username)

    if admin is None:
        raise HTTPException(status_code=404, detail="No such administrator")

    if not body.new_password:
        raise HTTPException(status_code=400, detail="Password cannot be empty")

    ok, message = validate_password_against_policy(body.new_password, get_admin_password_policy())
    if not ok:
        raise HTTPException(status_code=400, detail=message)

    salt, password_hash = portal_auth.hash_password(body.new_password)
    update_administrator_password(username, password_hash, salt, must_change_password=1)
    # Kill the target's existing sessions — a password reset on a suspected-
    # compromised account must invalidate any token the attacker already holds
    # (username/role are unchanged, so resolve_admin_token can't catch this).
    delete_admin_tokens_for_user(username)

    log_admin_audit("password_reset", username, f"reset by {acting}")

    return {"username": username, "status": "reset"}


@app.get("/admin/audit-log", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def get_admin_audit_log_route(limit: int = 200):
    # Superuser-gated: login attempts and administrator-account changes.
    return {"entries": get_admin_audit_log(_clamp_limit(limit))}


# =========================================================
# ADMINISTRATOR PASSWORD POLICY (admin-only)
# Governs the Sysible Controller's own GUI-login administrator
# accounts only - completely separate from environmental-policy
# above, which governs target Linux accounts on managed hosts.
# Configured from Sysible Controller Settings; enforced above in
# add_administrator_route / change_admin_credentials /
# force_admin_password_change.
# =========================================================
@app.get("/admin/password-policy", dependencies=[Depends(require_api_key)])
def get_admin_password_policy_route():

    return get_admin_password_policy()


@app.post("/admin/password-policy", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def set_admin_password_policy_route(body: SetAdminPasswordPolicyRequest):

    policy = body.model_dump()
    set_admin_password_policy(policy)

    return policy


@app.get("/portal/config", dependencies=[Depends(require_api_key)])
def get_portal_config_route():

    return get_portal_config()


@app.post("/portal/config", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def set_portal_config_route(body: SetPortalPortRequest):

    if not (1 <= body.port <= 65535):
        raise HTTPException(status_code=400, detail="Port must be between 1 and 65535")

    set_portal_port(body.port)

    return get_portal_config()


# =========================================================
# WEBSERVER PORTAL FILE POOL (admin-only)
# Shared pool, not per-host: "uploads" are files a host operator sent
# in through the portal, "downloads" are files the admin staged there
# for a host operator to grab. See backend/portal_files.py.
# =========================================================
@app.get("/portal/files/uploads", dependencies=[Depends(require_api_key)])
def list_portal_uploads_route():

    return {"files": portal_files.list_uploads()}


@app.get("/portal/files/uploads/{filename}", dependencies=[Depends(require_api_key)])
def fetch_portal_upload_route(filename: str):

    try:
        path = portal_files.upload_path(filename)
    except (portal_files.InvalidFilename, FileNotFoundError):
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(path, filename=path.name)


@app.delete("/portal/files/uploads/{filename}", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def delete_portal_upload_route(filename: str):

    try:
        deleted = portal_files.delete_upload(filename)
    except portal_files.InvalidFilename:
        raise HTTPException(status_code=404, detail="File not found")

    if not deleted:
        raise HTTPException(status_code=404, detail="File not found")

    return {"status": "deleted", "filename": filename}


@app.get("/portal/files/downloads", dependencies=[Depends(require_api_key)])
def list_portal_downloads_route():

    return {"files": portal_files.list_downloads()}


@app.post("/portal/files/downloads", dependencies=[Depends(require_api_key), Depends(require_superuser)])
async def stage_portal_download_route(request: Request, file: UploadFile = File(...)):

    # Bounded read so a single staged upload can't drive unbounded memory: reject an
    # oversized declared Content-Length up front, and cap the actual read so a
    # mis-declared/chunked body can't exceed the ceiling either.
    _max = _PORTAL_STAGE_MAX_BYTES
    _clen = request.headers.get("content-length")
    if _clen:
        try:
            if int(_clen) > _max + 4096:
                raise HTTPException(status_code=413,
                                    detail=f"File exceeds the {_max}-byte staging limit.")
        except ValueError:
            pass
    data = await file.read(_max + 1)
    if len(data) > _max:
        raise HTTPException(status_code=413,
                            detail=f"File exceeds the {_max}-byte staging limit.")

    try:
        saved_as = portal_files.save_download(file.filename, data)
    except portal_files.InvalidFilename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    return {"status": "staged", "filename": saved_as}


@app.delete("/portal/files/downloads/{filename}", dependencies=[Depends(require_api_key), Depends(require_superuser)])
def delete_portal_download_route(filename: str):

    try:
        deleted = portal_files.delete_download(filename)
    except portal_files.InvalidFilename:
        raise HTTPException(status_code=404, detail="File not found")

    if not deleted:
        raise HTTPException(status_code=404, detail="File not found")

    return {"status": "deleted", "filename": filename}


# =========================================================
# ROOT
# =========================================================
@app.get("/")
def root():

    return {
        "application": "Sysible Controller",
        "status": "running"
    }
