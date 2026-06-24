# Tracks the currently logged-in administrator's username for this
# running GUI process. Deliberately kept separate from client/api.py
# (a thin HTTP wrapper with no state of its own) - this is local,
# in-memory client state, not anything the backend knows about.
#
# Used to:
#   - show "logged in as <username>" in the Administrator Configuration
#     page
#   - pass `actor=` on add/remove-administrator calls for the audit log
#   - pre-fill the username when an admin changes their own credentials

_current_username = None
_current_role = None


def set_current_admin(username: str, role: str = None):
    global _current_username, _current_role
    _current_username = username
    _current_role = role


def get_current_admin():
    return _current_username


def get_current_role():
    """'superuser' or 'sysadmin' (or None if unknown). The GUI uses this to
    hide superuser-only tiles; the backend enforces the real restriction."""
    return _current_role


def is_superuser():
    # Unknown role defaults to True so older flows that never set a role
    # aren't locked out of the UI - the backend still gates regardless.
    return _current_role in (None, "superuser")


def clear_current_admin():
    global _current_username, _current_role
    _current_username = None
    _current_role = None
