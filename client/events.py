"""Tiny in-process event bus so independent dashboard popout windows
(Host Enrollment, User Administration, Remote Administration) can tell
each other when a host disappears, instead of each waiting on its own
refresh timer (or never refreshing at all) to notice.

Qt signals are the natural fit here since every page already runs on
the same Qt event loop - no polling, no extra threads."""

from PySide6.QtCore import QObject, Signal


class _Events(QObject):
    # Emitted with the host's identifier (host_id for an agent, name
    # for an SSH host) right after it's successfully disenrolled/removed
    # from the backend - any open window can connect to this to purge
    # cached data and reset its view instead of showing stale state.
    host_removed = Signal(str)

    # Emitted with a grace-period in seconds right before a deliberate
    # action that's expected to make the backend briefly unreachable -
    # currently just installing a new TLS certificate (Sysible Settings,
    # see client/admin_configuration_page.py), which makes the backend
    # restart itself so uvicorn picks up the new cert/key. main.py's
    # backend watchdog normally force-closes every window within ~3
    # seconds of the backend going quiet (see BACKEND_FAILURE_THRESHOLD)
    # - it subscribes to this to suppress that for the given window
    # instead of mistaking a deliberate restart for a crash.
    backend_restart_expected = Signal(int)


bus = _Events()
