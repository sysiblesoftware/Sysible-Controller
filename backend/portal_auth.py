"""Password hashing and session management for the Webserver Portal
login (backend/portal_app.py) - intentionally separate from the
admin/GUI login (which lives in backend/db.py's administrators table
and is checked directly in backend/app.py).

Sessions are persisted to backend/db.py's portal_sessions table
rather than kept in this module's memory, because the portal itself
runs as a separate subprocess (backend/portal_manager.py) from the
main admin API process - a plain in-memory dict here would only be
visible to whichever process happens to import this module, and the
admin GUI (talking to backend/app.py) needs to be able to list/revoke
sessions that were actually issued by the portal process.
"""

import hashlib
import os
import secrets
import time

from backend import db

PBKDF2_ITERATIONS = 200_000
SESSION_TTL_SECONDS = 2 * 60 * 60  # 2 hours


def hash_password(password: str, salt: str = None):
    """Returns (salt, hash) - salt is generated if not provided."""
    if salt is None:
        salt = secrets.token_hex(16)

    pw_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PBKDF2_ITERATIONS,
    ).hex()

    return salt, pw_hash


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    _, computed = hash_password(password, salt)
    return secrets.compare_digest(computed, expected_hash)


def create_session(ip: str = "") -> str:
    token = secrets.token_urlsafe(32)
    expires = time.time() + SESSION_TTL_SECONDS

    db.create_portal_session(token, expires, ip)

    return token


def validate_session(token: str) -> bool:
    if not token:
        return False

    session = db.get_portal_session(token)

    if session is None:
        return False

    if time.time() > session["expires"]:
        db.delete_portal_session_by_token(token)
        return False

    return True


def destroy_session(token: str):
    if token:
        db.delete_portal_session_by_token(token)
