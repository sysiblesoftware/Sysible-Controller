"""Single source of truth for the installed Sysible Controller version.

Lives at the project root (not under client/ or backend/) since both
sides need it and neither should import from the other - mirrors how
sysible_logo.png sits at the root for the same reason. PYTHONPATH is set
to the project root everywhere this matters (install_sysible.sh's systemd
unit), so `from version import VERSION` resolves the same way from
client/ and backend/ code alike.

Bump VERSION here on release - nothing else needs to change. Anything
that needs to report the controller version (the web console's License &
Version view, an admin API route, etc.) should import this rather than
hardcoding its own copy.
"""

VERSION = "3.0.2"
