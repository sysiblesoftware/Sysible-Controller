"""Single source of truth for the installed Sysible Controller version.

Lives at the project root (not under client/ or backend/) since both
sides need it and neither should import from the other - mirrors how
sysible_logo.png sits at the root for the same reason (see
client/branding.py). PYTHONPATH is set to the project root everywhere
this matters (install_sysible.sh's systemd unit, sysible_controller's
_start_gui), so `from version import VERSION` resolves the same way
from client/ and backend/ code alike.

Bump VERSION here on release - nothing else needs to change. The
License & Version section of Sysible Controller Settings
(client/admin_configuration_page.py) is the only current reader, but
anything added later (an admin API "what version is this controller"
route, About dialog, etc.) should import this rather than hardcoding
its own copy.
"""

VERSION = "3.0.0"
