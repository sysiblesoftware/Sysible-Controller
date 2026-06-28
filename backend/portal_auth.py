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

# Current work factor for NEW hashes (OWASP guidance for PBKDF2-HMAC-SHA256).
PBKDF2_ITERATIONS = 600_000
# Legacy work factor: hashes created before the bump don't record their
# iteration count, so a stored hash with no "iters$" prefix is assumed to be
# this. Verifying such a hash still succeeds; it's transparently upgraded to the
# new cost the next time the password is set (change/reset).
LEGACY_PBKDF2_ITERATIONS = 200_000
SESSION_TTL_SECONDS = 2 * 60 * 60  # 2 hours


# =========================================================
# PASSWORD HASHING
#
# Stored hash format is "<iterations>$<hexdigest>" for hashes created at or
# after the 600k bump. Older hashes are a bare hexdigest (no "$"), verified at
# the legacy cost. This lets the iteration count rise over time without locking
# anyone out and without a migration script.
# =========================================================
def hash_password(password: str):
    """Returns (salt_hex, hash_hex) for a brand-new credential."""
    salt = secrets.token_hex(16)
    digest = _pbkdf2(password, salt, PBKDF2_ITERATIONS)
    return salt, f"{PBKDF2_ITERATIONS}${digest}"


def verify_password(password: str, salt_hex: str, expected_hash_hex: str) -> bool:
    if not salt_hex or not expected_hash_hex:
        return False

    if "$" in expected_hash_hex:
        iters_str, _, expected_digest = expected_hash_hex.partition("$")
        try:
            iters = int(iters_str)
        except ValueError:
            return False
    else:
        iters, expected_digest = LEGACY_PBKDF2_ITERATIONS, expected_hash_hex

    actual_digest = _pbkdf2(password, salt_hex, iters)
    return hmac.compare_digest(actual_digest, expected_digest)


def _pbkdf2(password: str, salt_hex: str, iterations: int) -> str:
    salt_bytes = bytes.fromhex(salt_hex)
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt_bytes, iterations
    )
    return derived.hex()


# Back-compat shim: some callers/tests import _derive directly.
def _derive(password: str, salt_hex: str) -> str:
    return _pbkdf2(password, salt_hex, PBKDF2_ITERATIONS)


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
