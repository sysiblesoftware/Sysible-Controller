"""
Authentication for the Webserver Portal - the host-facing surface
that lets a remote host operator log in with a username/password and
download a ready-to-run agent bundle.

Deliberately separate from:
  - backend/auth.py's admin API key (that's for the GUI/admin, not a
    human typing credentials into a browser)
  - the enroll_tokens system in backend/db.py (single-use, minted per
    download by the portal itself - see backend/agent_bundle.py)

Two responsibilities live here:
  1. Password hashing for the one portal username/password pair
     (PBKDF2-HMAC-SHA256, random per-credential salt - never store or
     compare plaintext).
  2. The post-login session: validated on every portal request, and
     backed by backend/db.py's portal_sessions table (not an
     in-memory dict, as this used to be) specifically so the admin
     GUI - which talks only to backend/app.py, a *different process*
     from this portal subprocess - can list and revoke active
     sessions from the Webserver Portal Configuration page. A session
     row outliving the portal process (e.g. the portal gets stopped
     and restarted before a session's TTL is up) is harmless:
     stopping the portal already makes the cookie unusable since
     nothing is listening, and starting it again just resumes
     honoring whatever hasn't expired yet, same as before this moved
     to the DB.
"""

import hashlib
import hmac
import secrets
import time

from backend import db

PBKDF2_ITERATIONS = 200_000
SESSION_TTL_SECONDS = 2 * 60 * 60  # 2 hours


# =========================================================
# PASSWORD HASHING
# =========================================================
def hash_password(password: str):
    """Returns (salt_hex, hash_hex) for a brand-new credential."""
    salt = secrets.token_hex(16)
    return salt, _derive(password, salt)


def verify_password(password: str, salt_hex: str, expected_hash_hex: str) -> bool:
    if not salt_hex or not expected_hash_hex:
        return False

    actual_hash_hex = _derive(password, salt_hex)
    return hmac.compare_digest(actual_hash_hex, expected_hash_hex)


def _derive(password: str, salt_hex: str) -> str:
    salt_bytes = bytes.fromhex(salt_hex)
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt_bytes, PBKDF2_ITERATIONS
    )
    return derived.hex()


# =========================================================
# SESSIONS
# =========================================================
def create_session(ip: str = "") -> str:
    token = secrets.token_hex(32)
    db.create_portal_session(token, time.time() + SESSION_TTL_SECONDS, ip)
    return token


def validate_session(token: str) -> bool:
    if not token:
        return False

    row = db.get_portal_session(token)

    if row is None:
        return False

    if time.time() > row["expires"]:
        db.delete_portal_session_by_token(token)
        return False

    return True


def revoke_session(token: str):
    db.delete_portal_session_by_token(token)
