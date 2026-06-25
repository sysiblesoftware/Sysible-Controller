import signal
import sys
import time

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QMessageBox, QSystemTrayIcon, QMenu, QStyle,
)
from PySide6.QtGui import QAction, QIcon
from PySide6.QtCore import QTimer, Qt

from home import HomeWindow
from theme import apply_theme
from client import api
from client.admin_login_dialog import AdminLoginDialog
from client.create_admin_dialog import CreateAdminDialog
from client.force_password_change_dialog import ForcePasswordChangeDialog
from client.branding import LOGO_PATH
from client.events import bus
from client import session

# Consecutive missed health checks before treating the backend as
# down. A couple intervals of slack avoids closing everything over
# one slow/dropped request - but kept short (worst case well under
# 10s with api.ping()'s own timeout) so the GUI doesn't sit around for
# ages showing stale data after the backend actually stops.
BACKEND_CHECK_INTERVAL_MS = 1500
BACKEND_FAILURE_THRESHOLD = 2


class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()

        self.setWindowTitle("Sysible Controller")
        self.setWindowIcon(QIcon(str(LOGO_PATH)))
        self.resize(1200, 800)

        self.setCentralWidget(HomeWindow())

        # =====================================================
        # SYSTEM TRAY (close-to-tray)
        # Clicking this window's [x] used to quit the whole GUI
        # process outright - with quitOnLastWindowClosed left at
        # Qt's default and nothing else keeping the app alive, that
        # ended the only event loop there was, and the sole way back
        # in was re-running `sysible_controller start` (which itself
        # first tries to kill any process still on port 9000). Now the
        # [x] just hides the dashboard; the process - and its backend
        # watchdog below - keeps running via the tray icon, and
        # "Open Dashboard" (or a left-click on the icon) brings the
        # window straight back. `sysible_controller stop` still works
        # exactly as before, since it kills this process by PID
        # regardless of whether the window is shown or hidden.
        # =====================================================
        self._tray_available = QSystemTrayIcon.isSystemTrayAvailable()
        self.tray_icon = None

        if self._tray_available:
            logo_icon = QIcon(str(LOGO_PATH))
            tray_icon_img = (
                logo_icon if not logo_icon.isNull()
                else self.style().standardIcon(QStyle.SP_ComputerIcon)
            )
            self.tray_icon = QSystemTrayIcon(tray_icon_img, self)
            self.tray_icon.setToolTip("Sysible Controller")

            tray_menu = QMenu()

            open_action = QAction("Open Dashboard", self)
            open_action.triggered.connect(self._restore_window)
            tray_menu.addAction(open_action)

            logout_action = QAction("Log Out", self)
            logout_action.triggered.connect(self._logout)
            tray_menu.addAction(logout_action)

            tray_menu.addSeparator()

            quit_action = QAction("Quit Sysible Controller", self)
            quit_action.triggered.connect(QApplication.instance().quit)
            tray_menu.addAction(quit_action)

            self.tray_icon.setContextMenu(tray_menu)
            self.tray_icon.activated.connect(self._on_tray_activated)
            self.tray_icon.show()

            QApplication.instance().aboutToQuit.connect(self.tray_icon.hide)
        else:
            # No system tray on this desktop (some bare Linux window
            # managers/GNOME without an extension) - there's no icon
            # to click to get the window back. SIGUSR1 below (not
            # quitting on close) is what makes "back" reachable here.
            print(
                "[main] no system tray detected on this desktop - closing "
                "the dashboard window hides it instead of quitting; run "
                "'sysible_controller gui' again, or click the application "
                "menu icon, to bring it back."
            )

        # =====================================================
        # SIGUSR1 (bring the window back, tray or not)
        # `sysible_controller gui` - also what the application menu
        # launcher's icon runs - sends this signal instead of just
        # printing "already running" and doing nothing whenever it
        # finds the GUI process already alive. Without this, a
        # closed/hidden window was a dead end any time the tray icon
        # wasn't available, *or* was available per Qt's
        # isSystemTrayAvailable() check but its host (a panel or
        # extension) wasn't actually rendering it - both leave the
        # process running with no UI and no way back short of
        # `sysible_controller stop` + `start`. A plain Python signal
        # handler only fires once control returns to the interpreter;
        # the backend watchdog timer just below already guarantees
        # that happens at least every BACKEND_CHECK_INTERVAL_MS, so
        # delivery here is bounded by that, not indefinite.
        # =====================================================
        signal.signal(signal.SIGUSR1, self._on_sigusr1)

        # =====================================================
        # BACKEND WATCHDOG
        # The Host Enrollment / User Administration / Remote
        # Administration popouts are independent top-level windows
        # (see home.py) - not children of this one -
        # so closing this window, or any one of them, doesn't close
        # the rest. If the backend goes away, whether
        # `sysible_controller stop` stopped it or it crashed on its
        # own, there's no reason to
        # leave any of those windows open showing stale data and
        # failing API calls. Poll the unauthenticated health check and
        # shut the whole app down after a few consecutive misses.
        # =====================================================
        self._backend_down_count = 0
        # Set by _suppress_watchdog (below) right before a deliberate
        # action that's expected to make the backend briefly
        # unreachable - currently just a TLS certificate install (see
        # client/events.py's backend_restart_expected signal). A bare
        # monotonic deadline rather than a bool so a second restart
        # request mid-grace-period just extends it instead of needing
        # its own timer to clear a flag.
        self._suppress_watchdog_until = 0.0
        bus.backend_restart_expected.connect(self._suppress_watchdog)

        # Set true only while tearing down for a real logout/quit, so
        # closeEvent lets the window actually close instead of hiding to tray.
        self._logging_out = False
        # Raised by the dashboard header's Log Out button (and the tray's
        # Log Out). Queued so the click handler returns before we tear the
        # window down underneath it.
        bus.logout_requested.connect(self._logout, Qt.QueuedConnection)

        self._backend_timer = QTimer(self)
        self._backend_timer.timeout.connect(self._check_backend)
        self._backend_timer.start(BACKEND_CHECK_INTERVAL_MS)

    def _restore_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason):
        # Trigger == a single left-click on the icon itself; a
        # right-click instead opens the context menu set up above.
        if reason == QSystemTrayIcon.Trigger:
            self._restore_window()

    def closeEvent(self, event):
        # Always ignore + hide, tray or not - app.setQuitOnLastWindowClosed(False)
        # (see main()) means accepting this wouldn't quit the process either,
        # just hide the window with no tray icon and no way back. SIGUSR1
        # (registered in __init__) is what actually makes "back" reachable in
        # every case; the tray, when one happens to be available and actually
        # rendered by the desktop, is just a faster/more discoverable shortcut
        # to the same _restore_window() call.
        if self._logging_out:
            # Deliberate teardown for a logout - let it close for real.
            event.accept()
            return

        event.ignore()
        self.hide()

        if self._tray_available:
            self.tray_icon.showMessage(
                "Sysible",
                "Still running in the background. Click the tray icon, or "
                "run 'sysible_controller gui' again, to reopen the "
                "dashboard.",
                QSystemTrayIcon.Information,
                3000,
            )

    def _on_sigusr1(self, signum, frame):
        self._restore_window()

    def _logout(self):
        """End the current admin session and return to the login screen,
        without quitting the whole process. Revokes the RBAC token, closes
        the dashboard and every popout tool window, then re-runs the login
        gate; a successful login swaps in a fresh dashboard, while cancelling
        the login quits the app. The backend keeps running throughout."""
        self._restore_window()
        resp = QMessageBox.question(
            self,
            "Log Out",
            "Log out of Sysible Controller? Any open tool windows will be "
            "closed. The controller keeps running in the background.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if resp != QMessageBox.Yes:
            return

        # Stop the watchdog first - tearing down windows and bouncing through
        # a modal login dialog shouldn't be mistaken for a backend outage.
        self._backend_timer.stop()

        # Detach this (soon-to-be-dead) window from the logout signal so a
        # later logout from the replacement window doesn't also fire here.
        try:
            bus.logout_requested.disconnect(self._logout)
        except (RuntimeError, TypeError):
            pass

        # Revoke this session's token server-side and clear it locally. Even
        # if the call fails, the local token is cleared so nothing stays
        # authenticated on the client.
        try:
            api.admin_logout()
        except Exception:
            pass
        session.clear_current_admin()

        app = QApplication.instance()

        # Close every other top-level window (the independent popout tool
        # windows opened from the dashboard) so none linger with a now-revoked
        # session. Iterate a copy since closing mutates the list.
        for w in list(app.topLevelWidgets()):
            if w is self or not w.isWindow():
                continue
            try:
                w.close()
            except Exception:
                pass

        if self.tray_icon is not None:
            self.tray_icon.hide()

        # Close this dashboard for real BEFORE showing the login gate, so the
        # only thing on screen during re-login is the (centered) login dialog -
        # not the old dashboard lingering behind a modal. _logging_out makes
        # closeEvent accept instead of hiding-to-tray.
        self._logging_out = True
        self.close()
        app.processEvents()

        # Re-run the login gate (login dialog + any forced password change).
        if _run_login_gate():
            new_window = MainWindow()
            new_window.show()
            _retain_window(new_window)
        else:
            app.quit()

    def _suppress_watchdog(self, grace_seconds):
        """Connected to bus.backend_restart_expected - holds off on
        treating failed health checks as a real outage for
        grace_seconds, since a deliberate restart (TLS cert install)
        can easily take longer than BACKEND_FAILURE_THRESHOLD intervals
        and would otherwise get mistaken for a crash. Normal counting
        resumes automatically once the deadline passes, so a restart
        that genuinely fails to come back up still eventually surfaces
        the usual "lost connection" message instead of hanging forever."""
        self._backend_down_count = 0
        self._suppress_watchdog_until = time.monotonic() + grace_seconds

    def _check_backend(self):
        if api.ping():
            self._backend_down_count = 0
            return

        if time.monotonic() < self._suppress_watchdog_until:
            return

        self._backend_down_count += 1

        if self._backend_down_count >= BACKEND_FAILURE_THRESHOLD:
            self._backend_timer.stop()
            self._restore_window()
            QMessageBox.critical(
                self,
                "Sysible Controller",
                "Lost connection to the Sysible Controller backend (stopped or crashed). "
                "Closing all windows.",
            )
            QApplication.instance().quit()


# Keeps live MainWindow instances from being garbage-collected after the
# local variable that created them goes out of scope (e.g. the old window's
# _logout method that spawns a replacement). Dead/hidden windows are pruned
# on each insert so this doesn't grow without bound across many logouts.
_LIVE_WINDOWS = []


def _retain_window(window):
    _LIVE_WINDOWS[:] = [w for w in _LIVE_WINDOWS if w is not window and _alive(w)]
    _LIVE_WINDOWS.append(window)


def _alive(window):
    try:
        return window.isVisible() or not window._logging_out
    except RuntimeError:
        # Underlying C++ object already deleted.
        return False


def _run_login_gate():
    """Run first-run setup OR the normal login, plus any forced password
    change. Returns True once an administrator is authenticated for this
    session, or False if the operator cancelled (caller should exit/quit).

    Shared by initial startup and re-login after a logout. On re-login an
    administrator already exists, so this always takes the login path.
    """
    try:
        needs_setup = api.admin_setup_required()
    except Exception:
        needs_setup = False

    if needs_setup:
        setup = CreateAdminDialog()
        if setup.exec() != CreateAdminDialog.Accepted:
            return False
        # Account created and session set - go straight to the dashboard.
        return True

    login = AdminLoginDialog()
    if login.exec() != AdminLoginDialog.Accepted:
        return False

    # Forced password change for an account still on a temporary password.
    if login.must_change_password:
        change = ForcePasswordChangeDialog(login.username, login.password)
        if change.exec() != ForcePasswordChangeDialog.Accepted:
            return False

    return True


def main():

    # High-DPI sharpness: on laptops/small screens running a fractional
    # display scale (125%, 150%, ...), Qt's default rounds the scale factor
    # and bitmap-scales the UI, which makes text and icons look soft/blurry.
    # PassThrough honors the exact fractional factor so everything renders
    # crisp at the real device pixel ratio. Must be set before QApplication
    # is constructed. (High-DPI scaling itself is already on by default in Qt6.)
    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    except Exception:
        pass

    app = QApplication(sys.argv)

    # Without this, the OS taskbar/dock/alt-tab entry for this process
    # shows a generic Python icon and labels itself after the entry
    # script ("main.py") instead of the product. setApplicationName
    # covers the text; setWindowIcon (applied at the QApplication level,
    # so every top-level window - login dialog, dashboard, popouts -
    # inherits it by default) covers the icon.
    app.setApplicationName("Sysible Controller")
    app.setApplicationDisplayName("Sysible Controller")
    # Must match the installed launcher's filename (sysible-controller.desktop,
    # see install_sysible.sh), not just the display name - this is what lets
    # the windowing system tie this running app's window back to that
    # launcher's icon (sysible_logo.png) on the application menu tile, the
    # dock/taskbar, and alt-tab, instead of falling back to a generic icon.
    app.setDesktopFileName("sysible-controller")
    app.setWindowIcon(QIcon(str(LOGO_PATH)))

    # Closing the dashboard (or any popout) should not by itself end
    # the process anymore - see MainWindow's tray-icon setup above.
    # The app now only quits via the tray's "Quit Sysible Controller", the
    # backend watchdog, or an external kill ('sysible_controller stop').
    app.setQuitOnLastWindowClosed(False)

    apply_theme(app)

    # =========================================================
    # ADMIN GATE (first-run setup OR login)
    # On a fresh install no administrator exists - there's no default
    # account - so the first launch makes the operator create their own
    # account (and is then logged straight in). Every later launch shows
    # the normal login. Cancelling exits the app outright rather than
    # falling through to the dashboard. The same gate is re-run on logout
    # (see MainWindow._logout) to return to the login screen without
    # quitting the process. If the setup check can't reach the backend,
    # it falls back to the login dialog, which surfaces the error itself.
    #
    # FORCED PASSWORD CHANGE is handled inside _run_login_gate(): an account
    # a fellow admin (re)created with a temporary password can't reach the
    # dashboard until it sets its own - exactly what must_change_password
    # exists to prevent.
    # =========================================================
    if not _run_login_gate():
        sys.exit(0)

    window = MainWindow()
    window.show()
    _retain_window(window)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
