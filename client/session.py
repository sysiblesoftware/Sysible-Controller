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


def set_current_admin(username: str):
    global _current_username
    _current_username = username


def get_current_admin():
    return _current_username


def clear_current_admin():
    global _current_username
    _current_username = None
