from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
import json
import secrets
import time

from backend.agent_bundle import build_agent_bundle, detect_local_ips, resolve_controller_addresses
from backend.auth import require_api_key
from backend.db import (
    create_enroll_token,
    validate_enroll_token,
    resolve_enroll_token_host,
    consume_enroll_token,
    create_or_update_agent,
    update_agent_heartbeat,
    list_agents,
    delete_agent,
    get_agent_secret,
    agent_exists,
    queue_task,
    fetch_pending_tasks,
    submit_task_result,
    get_task_kind,
    list_results,
    list_environments,
    create_environment,
    delete_environment,
    set_agent_environment,
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
    update_administrator_password,
    update_administrator_username,
    record_administrator_login,
    log_admin_audit,
    get_admin_audit_log,
    get_environmental_policy,
    set_environmental_policy,
    get_admin_password_policy,
    set_admin_password_policy,
)
from backend import portal_auth, portal_files, portal_manager, tls_manager
from backend.policy import validate_password_against_policy
from backend.models.agent_models import (
    EnrollRequest,
    HeartbeatRequest,
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
    ssh_host_exists,
    get_agent_ssh_state,
    set_agent_ssh_state,
    AGENT_SSH_MARKER,
)

# docs_url/redoc_url disabled: the interactive Swagger/ReDoc consoles are
# an unauthenticated map of the whole API (and a ready-made request
# builder) for anyone who can reach the port. openapi_url is kept because
# `sysible_controller`'s readiness self-check fetches /openapi.json to
# confirm the running process is current code.
app = FastAPI(title="Sysible Controller", docs_url=None, redoc_url=None)


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

    expected = get_agent_secret(host_id)

    if not expected or not secrets.compare_digest(agent_secret, expected):
        raise HTTPException(status_code=401, detail="Invalid agent secret")


# =========================================================
# SERVER TOKEN GENERATION (admin-only: localhost + API key)
# =========================================================
@app.post("/admin/enroll-token/generate", dependencies=[Depends(require_api_key)])
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

    token = secrets.token_hex(16)

    create_enroll_token(token)

    return {
        "token": token,
        "valid_days": 365
    }


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


def _maybe_enroll_agent_ssh(host_id, hostname, ip, environment, force=False):
    """Queue the one-time SSH-enable command on an agent host, unless we
    already have an SSH connection for it or an attempt is already
    pending. force=True (a fresh /enroll) always re-queues - the
    operator may have just installed sshd after a prior 'sshd_missing'."""
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
                register_agent_ssh_host(hostname, ip, environment)
                set_agent_ssh_state(host_id, {"status": "enabled"})
                return
            except Exception:
                pass
        set_agent_ssh_state(host_id, {"status": "error"})
    elif f"{AGENT_SSH_MARKER}stopped" in stdout:
        set_agent_ssh_state(host_id, {"status": "sshd_missing"})
    else:
        set_agent_ssh_state(host_id, {"status": "error"})


@app.post("/agents/enroll")
def enroll(req: EnrollRequest):
    # Plain `def` (threadpooled) for the same reason as heartbeat below: the
    # body is all blocking DB/token work and shouldn't occupy the event loop.

    if not validate_enroll_token(req.token):
        raise HTTPException(
            status_code=403,
            detail="Invalid or expired token"
        )

    host_id = resolve_enroll_token_host(req.token, req.host_id)

    # Community-edition host cap (no-op in an unlimited/Enterprise build).
    from backend.edition import enforce_host_limit
    enforce_host_limit(req.hostname or host_id)

    agent_secret = secrets.token_hex(24)

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

    consume_enroll_token(req.token, host_id)

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

    update_agent_heartbeat(req.host_id, req.ip)

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
@app.post("/agents/{host_id}/tasks", dependencies=[Depends(require_api_key)])
def queue_agent_task(host_id: str, body: TaskCreateRequest):

    if not agent_exists(host_id):
        raise HTTPException(status_code=404, detail="Unknown host_id")

    task_id = queue_task(host_id, body.command, body.kind)

    return {
        "task_id": task_id,
        "status": "queued"
    }


@app.get("/agents/{host_id}/tasks")
def poll_agent_tasks(host_id: str, agent_secret: str):

    verify_agent(host_id, agent_secret)

    return {
        "tasks": fetch_pending_tasks(host_id)
    }


@app.post("/agents/{host_id}/tasks/result")
def post_task_result(host_id: str, body: TaskResultRequest):

    if host_id != body.host_id:
        raise HTTPException(status_code=400, detail="host_id mismatch")

    verify_agent(body.host_id, body.agent_secret)

    submit_task_result(body.task_id, body.host_id, body.result)

    # The controller's own SSH-terminal auto-enroll command reports back
    # through this same path - intercept its result to register the SSH
    # connection (or record sshd_missing) rather than showing it to the
    # operator as an ordinary command result.
    if get_task_kind(body.task_id) == "ssh_enable":
        _consume_ssh_enable_result(body.host_id, body.result)

    return {
        "status": "recorded"
    }


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


# =========================================================
# INVENTORY
# =========================================================
@app.get("/agents", dependencies=[Depends(require_api_key)])
def get_agents():

    agents = list_agents()
    for a in agents:
        st = get_agent_ssh_state(a.get("host_id"))
        # "enabled" | "pending" | "sshd_missing" | "error" | None
        a["ssh_terminal_state"] = (st or {}).get("status")

    return {
        "agents": agents
    }


# =========================================================
# DISENROLL
# =========================================================
@app.delete("/agents/{host_id}", dependencies=[Depends(require_api_key)])
def remove_agent(host_id: str):

    delete_agent(host_id)

    return {
        "status": "removed",
        "host_id": host_id
    }


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

    delete_agent(req.host_id)

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
@app.post("/agents/{host_id}/environment", dependencies=[Depends(require_api_key)])
def set_agent_environment_route(host_id: str, body: SetEnvironmentRequest):

    if not agent_exists(host_id):
        raise HTTPException(status_code=404, detail="Unknown host_id")

    set_agent_environment(host_id, body.environment)

    return {
        "host_id": host_id,
        "environment": body.environment
    }


# =========================================================
# ENVIRONMENTS (dev/stage/prod, etc. - editable registry shared by
# agent hosts and SSH hosts; admin-only)
# =========================================================
@app.get("/environments", dependencies=[Depends(require_api_key)])
def get_environments():

    return {
        "environments": list_environments()
    }


@app.post("/environments", dependencies=[Depends(require_api_key)])
def add_environment(body: CreateEnvironmentRequest):

    name = body.name.strip()

    if not name:
        raise HTTPException(status_code=400, detail="Environment name cannot be empty")

    create_environment(name)

    return {
        "environments": list_environments()
    }


@app.delete("/environments/{name}", dependencies=[Depends(require_api_key)])
def remove_environment(name: str):

    delete_environment(name)

    return {
        "environments": list_environments()
    }


# =========================================================
# CONTROLLER CONFIGURATION (admin-only)
# The hostname/port baked into agent bundles the Webserver Portal
# hands out - see backend/agent_bundle.py.
# =========================================================
@app.get("/controller-config", dependencies=[Depends(require_api_key)])
def get_controller_config_route():

    return get_controller_config()


@app.post("/controller-config", dependencies=[Depends(require_api_key)])
def set_controller_config_route(body: SetControllerConfigRequest):

    hostname = body.hostname.strip()
    ip = body.ip.strip()
    address_mode = body.address_mode if body.address_mode in ("hostname", "ip", "all") else "hostname"

    # "all" mode needs neither field - every detected local IP is what
    # ships in the bundle, computed fresh at download time (see
    # resolve_controller_addresses), not typed in here.
    if address_mode != "all" and not hostname and not ip:
        raise HTTPException(status_code=400, detail="Hostname and IP cannot both be empty")

    if address_mode == "hostname" and not hostname:
        raise HTTPException(status_code=400, detail="Hostname is selected but empty")

    if address_mode == "ip" and not ip:
        raise HTTPException(status_code=400, detail="IP Address is selected but empty")

    if address_mode == "all" and not detect_local_ips():
        raise HTTPException(
            status_code=400,
            detail="No local IP addresses were detected on this controller - check its network interfaces.",
        )

    if not (1 <= body.port <= 65535):
        raise HTTPException(status_code=400, detail="Port must be between 1 and 65535")

    set_controller_config(hostname, ip, address_mode, body.port)

    return get_controller_config()


@app.get("/license-config", dependencies=[Depends(require_api_key)])
def get_license_config_route():

    return get_license_config()


@app.post("/license-config", dependencies=[Depends(require_api_key)])
def set_license_config_route(body: SetLicenseKeyRequest):

    return set_license_config(body.license_key.strip())


@app.get("/controller-config/local-ips", dependencies=[Depends(require_api_key)])
def get_local_ips_route():
    """Every non-loopback IPv4 address found on this controller right
    now - powers the IP picker/"All Detected IPs" option in Controller
    Configuration so the admin never has to run `ip addr`/`ifconfig` by
    hand."""

    return {"ips": detect_local_ips()}


@app.get("/controller-config/agent-bundle", dependencies=[Depends(require_api_key)])
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

    enroll_token = secrets.token_hex(16)
    create_enroll_token(enroll_token)

    filename, zip_bytes = build_agent_bundle(
        addresses, config["port"], enroll_token
    )

    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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


@app.post("/controller-config/tls/install", dependencies=[Depends(require_api_key)])
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

    cert_pem = await cert_file.read()
    key_pem = await key_file.read()
    chain_pem = await chain_file.read() if chain_file is not None else None

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


@app.post("/environmental-policy", dependencies=[Depends(require_api_key)])
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


@app.post("/portal/start", dependencies=[Depends(require_api_key)])
def start_portal_route():

    return portal_manager.start()


@app.post("/portal/stop", dependencies=[Depends(require_api_key)])
def stop_portal_route():

    return portal_manager.stop()


@app.post("/portal/credentials", dependencies=[Depends(require_api_key)])
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


@app.delete("/portal/credentials", dependencies=[Depends(require_api_key)])
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
    return {"history": get_portal_login_history(limit)}


@app.get("/portal/sessions", dependencies=[Depends(require_api_key)])
def list_portal_sessions_route():
    return {"sessions": list_portal_sessions()}


@app.post("/portal/sessions/{session_id}/revoke", dependencies=[Depends(require_api_key)])
def revoke_portal_session_route(session_id: int):
    delete_portal_session(session_id)
    return {"status": "revoked"}


# =========================================================
# ADMINISTRATORS (gates the desktop GUI itself - the "Sysible
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
    created = add_administrator(
        username, password_hash, salt, must_change_password=0, created_by="setup"
    )
    if not created:
        raise HTTPException(status_code=409, detail="An administrator with that username already exists")

    log_admin_audit("administrator_added", username, "first-run setup")

    return {"username": username, "status": "created"}


@app.post("/admin/login", dependencies=[Depends(require_api_key)])
def admin_login(body: AdminLoginRequest):

    username = body.username.strip()
    admin = get_administrator(username)

    valid = admin is not None and portal_auth.verify_password(
        body.password, admin["password_salt"], admin["password_hash"]
    )

    if not valid:
        log_admin_audit("login_failed", username, "Invalid username or password")
        raise HTTPException(status_code=401, detail="Invalid username or password")

    record_administrator_login(username)
    log_admin_audit("login_success", username, "")

    return {
        "status": "ok",
        "username": username,
        "must_change_password": bool(admin["must_change_password"]),
    }


@app.get("/admin/administrators", dependencies=[Depends(require_api_key)])
def list_administrators_route():
    """Username, account metadata only - never password hash/salt."""

    return {"administrators": list_administrators()}


@app.post("/admin/administrators", dependencies=[Depends(require_api_key)])
def add_administrator_route(body: AddAdministratorRequest):

    username = body.username.strip()

    if not username:
        raise HTTPException(status_code=400, detail="Username cannot be empty")

    if not body.password:
        raise HTTPException(status_code=400, detail="Password cannot be empty")

    ok, message = validate_password_against_policy(body.password, get_admin_password_policy())
    if not ok:
        raise HTTPException(status_code=400, detail=message)

    salt, password_hash = portal_auth.hash_password(body.password)
    created = add_administrator(
        username, password_hash, salt, must_change_password=1, created_by=body.actor or None
    )

    if not created:
        raise HTTPException(status_code=409, detail="An administrator with that username already exists")

    log_admin_audit("administrator_added", username, f"added by {body.actor}" if body.actor else "")

    return {"username": username, "status": "added"}


@app.delete("/admin/administrators/{username}", dependencies=[Depends(require_api_key)])
def remove_administrator_route(username: str, actor: str = ""):

    if get_administrator(username) is None:
        raise HTTPException(status_code=404, detail="No such administrator")

    if count_administrators() <= 1:
        raise HTTPException(status_code=400, detail="Cannot remove the last remaining administrator")

    remove_administrator(username)
    log_admin_audit("administrator_removed", username, f"removed by {actor}" if actor else "")

    return {"username": username, "status": "removed"}


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

    new_username = body.new_username.strip()

    if not new_username:
        raise HTTPException(status_code=400, detail="Username cannot be empty")

    if not body.new_password:
        raise HTTPException(status_code=400, detail="Password cannot be empty")

    ok, message = validate_password_against_policy(body.new_password, get_admin_password_policy())
    if not ok:
        raise HTTPException(status_code=400, detail=message)

    if new_username != body.username:
        renamed = update_administrator_username(body.username, new_username)
        if not renamed:
            raise HTTPException(status_code=409, detail="An administrator with that username already exists")

    salt, password_hash = portal_auth.hash_password(body.new_password)
    update_administrator_password(new_username, password_hash, salt, must_change_password=0)

    log_admin_audit("password_changed", new_username, "")

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

    log_admin_audit("forced_password_change_completed", body.username, "")

    return {"username": body.username, "status": "updated"}


@app.get("/admin/audit-log", dependencies=[Depends(require_api_key)])
def get_admin_audit_log_route(limit: int = 200):

    return {"entries": get_admin_audit_log(limit)}


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


@app.post("/admin/password-policy", dependencies=[Depends(require_api_key)])
def set_admin_password_policy_route(body: SetAdminPasswordPolicyRequest):

    policy = body.model_dump()
    set_admin_password_policy(policy)

    return policy


@app.get("/portal/config", dependencies=[Depends(require_api_key)])
def get_portal_config_route():

    return get_portal_config()


@app.post("/portal/config", dependencies=[Depends(require_api_key)])
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


@app.delete("/portal/files/uploads/{filename}", dependencies=[Depends(require_api_key)])
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


@app.post("/portal/files/downloads", dependencies=[Depends(require_api_key)])
async def stage_portal_download_route(file: UploadFile = File(...)):

    data = await file.read()

    try:
        saved_as = portal_files.save_download(file.filename, data)
    except portal_files.InvalidFilename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    return {"status": "staged", "filename": saved_as}


@app.delete("/portal/files/downloads/{filename}", dependencies=[Depends(require_api_key)])
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
