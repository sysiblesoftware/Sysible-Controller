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
