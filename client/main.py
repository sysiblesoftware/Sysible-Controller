import signal
import sys

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QMessageBox, QSystemTrayIcon, QMenu, QStyle,
)
from PySide6.QtGui import QAction, QIcon
from PySide6.QtCore import QTimer

from home import HomeWindow
from theme import apply_theme
from client import api
from client.admin_login_dialog import AdminLoginDialog
from client.force_password_change_dialog import ForcePasswordChangeDialog
from client.branding import LOGO_PATH

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

    def _check_backend(self):
        if api.ping():
            self._backend_down_count = 0
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


def main():

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
    # ADMIN LOGIN GATE
    # Shown once per process launch, before the dashboard exists at
    # all - mirrors how any other admin console makes you log in
    # before showing anything. Quitting or closing this dialog exits
    # the app outright rather than falling through to the dashboard.
    # =========================================================
    login = AdminLoginDialog()
    if login.exec() != AdminLoginDialog.Accepted:
        sys.exit(0)

    # =========================================================
    # FORCED PASSWORD CHANGE
    # Set on the account that just logged in if it's the auto-seeded
    # default admin/admin, or one a fellow admin just (re)created
    # with a temporary password. No way to dismiss this short of
    # quitting - letting someone past this into the dashboard while
    # still on a known/temporary password is exactly what
    # must_change_password exists to prevent.
    # =========================================================
    if login.must_change_password:
        change = ForcePasswordChangeDialog(login.username, login.password)
        if change.exec() != ForcePasswordChangeDialog.Accepted:
            sys.exit(0)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
