"""
Shared API-key authentication for admin/GUI-facing endpoints.

This protects the human-facing surface of the API (agent inventory,
user/group administration, remote SSH execution). Agents themselves
do NOT use this key - they authenticate with a per-host secret that's
issued at enrollment time (see backend/app.py).

The key lives on disk at SYSIBLE_API_KEY_FILE (default
/opt/sysible/api_key.txt, mode 600) and is generated automatically on
first run if it doesn't exist yet. It can also be supplied via the
SYSIBLE_API_KEY environment variable, which always takes priority -
handy for tests/containers.
"""

import os
import secrets
from pathlib import Path

from fastapi import Header, HTTPException

API_KEY_ENV = "SYSIBLE_API_KEY"
API_KEY_FILE = Path(os.getenv("SYSIBLE_API_KEY_FILE", "/opt/sysible/api_key.txt"))


def _read_existing_key():
    env_key = os.getenv(API_KEY_ENV)
    if env_key:
        return env_key.strip()

    if API_KEY_FILE.exists():
        content = API_KEY_FILE.read_text().strip()
        if content:
            return content

    return None


def get_or_create_api_key():
    key = _read_existing_key()

    if key:
        return key

    key = secrets.token_hex(32)

    try:
        API_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Create 0600 atomically (O_EXCL|O_NOFOLLOW) instead of write_text()+chmod:
        # the old pattern left the admin API key world-readable at the umask
        # default between create and chmod, so a local unprivileged user racing
        # first-run could read it. O_EXCL also loses the race safely if another
        # process created it first (we re-read theirs below).
        fd = os.open(str(API_KEY_FILE), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, (key + "\n").encode())
        finally:
            os.close(fd)
    except FileExistsError:
        existing = _read_existing_key()
        if existing:
            return existing
    except OSError:
        # Couldn't persist (e.g. no permission to /opt/sysible outside
        # a real install). Still usable for this process's lifetime.
        pass

    return key


# Resolved once at import time. Re-import or restart the process to
# pick up a key rotated on disk.
_API_KEY = get_or_create_api_key()


def require_api_key(x_api_key: str = Header(default=None, alias="X-API-Key")):
    """FastAPI dependency - raise 401 unless a valid admin API key is presented."""

    if not x_api_key or not secrets.compare_digest(x_api_key, _API_KEY):
        raise HTTPException(status_code=401, detail="Missing or invalid API key")


def require_superuser(x_admin_token: str = Header(default=None, alias="X-Sysible-Admin-Token")):
    """RBAC gate for superuser-only actions (managing admins, enrolling/
    removing hosts, controller config, viewing the activity/controller logs).

    A present token is validated: an invalid/expired token is 401, a non-
    superuser (sysadmin) token is 403.

    A request with NO token is allowed ONLY during first-run bootstrap, i.e.
    before any administrator exists (the first admin is created via
    /admin/setup, which doesn't pass through here). Once at least one admin
    exists a valid superuser token is mandatory - otherwise the superuser /
    sysadmin split could be bypassed by simply omitting the header while
    holding the install-time API key, collapsing the role separation to
    nothing. The hard, unspoofable control is still on-host (run-as-user +
    local sudo); this enforces the controller-side separation of duties too.

    db is imported lazily to avoid an import cycle (db has no dependency on
    auth, but importing it at module load would still couple the two)."""
    from backend.db import resolve_admin_token, count_administrators, get_administrator

    if not x_admin_token:
        # Bootstrap only: no admins yet => allow (so the very first account can
        # be set up). After that, a superuser token is required.
        if count_administrators() == 0:
            return
        raise HTTPException(
            status_code=401,
            detail="A superuser login token is required for this action.",
        )

    admin = resolve_admin_token(x_admin_token)
    if not admin:
        raise HTTPException(status_code=401, detail="Invalid or expired admin token")
    if admin.get("role") != "superuser":
        raise HTTPException(status_code=403, detail="This action requires a superuser account.")

    # Defence-in-depth for the forced first-login/reset password change: a superuser
    # still carrying a temporary credential (must_change_password) must not be able
    # to drive privileged superuser routes until they rotate it — regardless of which
    # front end sends the token. The self-service credential-change endpoints
    # (/admin/credentials, /admin/force-password-change) are api-key-only, NOT gated
    # by this dependency, so they stay reachable to clear the flag. The BFF enforces
    # the same gate (require_login_changed / require_operator); this closes the
    # controller-side hole so a raw-API call can't bypass it either.
    acct = get_administrator(admin["username"])
    if acct and acct.get("must_change_password"):
        raise HTTPException(
            status_code=403,
            detail="You must change your temporary password before performing this action.")


def acting_admin_name(x_admin_token: str = Header(default=None, alias="X-Sysible-Admin-Token")):
    """The username behind the presented admin token — for UNFORGEABLE audit
    attribution of *who* performed a privileged account change. Resolve it from
    the validated token here rather than trusting a client-supplied 'actor'
    field (which the caller could set to any name). Returns 'system' during
    first-run bootstrap (no token yet), 'unknown' for an unresolvable token
    (the route's own require_superuser gate has already rejected an invalid one
    on the paths that use this)."""
    from backend.db import resolve_admin_token
    if not x_admin_token:
        return "system"
    admin = resolve_admin_token(x_admin_token)
    return (admin or {}).get("username") or "unknown"


def require_activity_viewer(x_admin_token: str = Header(default=None, alias="X-Sysible-Admin-Token")):
    """RBAC gate for the activity feed: allowed for a superuser OR the read-only
    'auditor' role (oversight without any ability to act). Same token-resolution
    and first-run bootstrap rules as require_superuser; only the allowed-role set
    differs. The controller service log stays superuser-only (require_superuser)."""
    from backend.db import resolve_admin_token, count_administrators

    if not x_admin_token:
        if count_administrators() == 0:
            return
        raise HTTPException(
            status_code=401,
            detail="A login token is required for this action.",
        )

    admin = resolve_admin_token(x_admin_token)
    if not admin:
        raise HTTPException(status_code=401, detail="Invalid or expired admin token")
    if admin.get("role") not in ("superuser", "auditor"):
        raise HTTPException(status_code=403, detail="This action requires a superuser or auditor account.")
