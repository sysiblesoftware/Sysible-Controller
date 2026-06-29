"""Server-side password-policy enforcement for Sysible Controller admin
accounts (the GUI's own login, not target-host Linux accounts - those
are validated client-side in client/api.py against the same
environmental_policy shape, but pushed to hosts via pwquality.conf
instead of checked in Python).

Kept separate from client/api.py's check_password_strength because the
admin account routes in backend/app.py must enforce this regardless of
what client is talking to them - relying on the desktop GUI to have
already checked would let anything bypass it.
"""

import re
import secrets
import string


def generate_compliant_password(policy: dict = None, length: int = 16) -> str:
    """Generate a random password that ALWAYS satisfies `policy` (same
    minlen/dcredit/ucredit/lcredit/ocredit shape as
    validate_password_against_policy): one guaranteed character from each
    required class, padded from the full pool to at least the policy's minlen,
    then shuffled. Used for the seeded default admin, `reset-admin`, and any
    server-side generated admin password, so a generated value never fails the
    policy check that immediately follows."""
    # An empty/None policy defaults to "require all four classes" (the default
    # admin policy) so a generated password is always strong even if the caller
    # somehow has no policy to hand.
    policy = policy or {"minlen": 12, "dcredit": -1, "ucredit": -1, "lcredit": -1, "ocredit": -1}
    minlen = policy.get("minlen", 12)
    length = max(length, minlen)

    lower, upper, digits = string.ascii_lowercase, string.ascii_uppercase, string.digits
    symbols = "!@#$%^&*()-_=+[]{}"
    pool = lower + upper + digits + symbols

    required = []
    if policy.get("lcredit", 0) < 0:
        required.append(lower)
    if policy.get("ucredit", 0) < 0:
        required.append(upper)
    if policy.get("dcredit", 0) < 0:
        required.append(digits)
    if policy.get("ocredit", 0) < 0:
        required.append(symbols)

    chars = [secrets.choice(p) for p in required]
    chars += [secrets.choice(pool) for _ in range(max(length - len(chars), 0))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def validate_password_against_policy(password: str, policy: dict):
    """Returns (ok, message). message explains the first unmet
    requirement when ok is False, "" when ok is True. policy uses the
    same minlen/dcredit/ucredit/lcredit/ocredit shape as
    PasswordPolicyFields - a credit value < 0 means "at least one
    character of that class is required", 0 means "not required"."""

    minlen = policy.get("minlen", 12)
    if len(password) < minlen:
        return False, f"Password must be at least {minlen} characters long."

    if policy.get("lcredit", 0) < 0 and not re.search(r"[a-z]", password):
        return False, "Password must include at least one lowercase letter."

    if policy.get("ucredit", 0) < 0 and not re.search(r"[A-Z]", password):
        return False, "Password must include at least one uppercase letter."

    if policy.get("dcredit", 0) < 0 and not re.search(r"[0-9]", password):
        return False, "Password must include at least one digit."

    if policy.get("ocredit", 0) < 0 and not re.search(r"[^A-Za-z0-9]", password):
        return False, "Password must include at least one symbol (e.g. ! @ # $ %)."

    return True, ""
