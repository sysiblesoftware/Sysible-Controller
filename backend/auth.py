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
        API_KEY_FILE.write_text(key + "\n")
        os.chmod(API_KEY_FILE, 0o600)
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
    from backend.db import resolve_admin_token, count_administrators

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
