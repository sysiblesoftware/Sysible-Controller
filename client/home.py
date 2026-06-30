from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QPushButton,
)
from PySide6.QtGui import QPixmap
from PySide6.QtCore import Qt

from client.dashboard_card import DashboardCard
from client.host_enrollment_page import HostEnrollmentPage
from client.admin_configuration_page import AdminConfigurationPage
from client.remote_administration_page import RemoteAdministrationPage
from client.system_administration_page import SystemAdministrationPage
from client.fleet_dashboard_page import FleetDashboardPage
from client.fleet_performance_page import FleetPerformancePage
from client.live_log_page import LiveLogPage
from client.branding import LOGO_PATH
from client.theme_toggle import ThemeToggle
from client.events import bus
from client import feature_search, theme, api, session


class HomeWindow(QWidget):

    def __init__(self):
        super().__init__()

        # A single column of tiles - narrower and taller.
        self.resize(620, 780)
        self.setMinimumWidth(440)

        outer = QVBoxLayout()
        outer.setContentsMargins(40, 32, 40, 32)
        outer.setSpacing(8)
        self.setLayout(outer)

        # =========================================================
        # HEADER
        # =========================================================
        header_row = QHBoxLayout()
        header_row.setSpacing(14)

        logo_pixmap = QPixmap(str(LOGO_PATH))
        if not logo_pixmap.isNull():
            logo_label = QLabel()
            logo_label.setPixmap(
                logo_pixmap.scaledToHeight(56, Qt.SmoothTransformation)
            )
            header_row.addWidget(logo_label)

        header_text = QVBoxLayout()
        header_text.setSpacing(2)

        self.title_label = QLabel("Sysible Controller")
        header_text.addWidget(self.title_label)

        self.subtitle_label = QLabel("Select a tool below to get started.")
        header_text.addWidget(self.subtitle_label)

        header_row.addLayout(header_text)
        header_row.addStretch()

        # Edition badge - shows "Community Edition · N/10 hosts" so the host
        # cap is visible at a glance from the main screen, not just on the
        # Host Enrollment page. Hidden on an unlimited (Enterprise) build.
        self.edition_badge = QLabel()
        self.edition_badge.setVisible(False)
        header_row.addWidget(self.edition_badge)
        self._refresh_edition_badge()

        # Signed-in identity + Log Out. Shows who's currently logged in and
        # gives a one-click way to end the session (revokes the RBAC token and
        # returns to the login screen) without quitting the whole app.
        self.signed_in_label = QLabel()
        self.signed_in_label.setVisible(False)
        header_row.addWidget(self.signed_in_label)

        # Self-service sudo-password store - available to every admin
        # (including sysadmins, who don't see the Settings tile), since it's a
        # personal credential needed to elevate on password-sudo hosts.
        self.sudo_pw_button = QPushButton("Sudo Password")
        self.sudo_pw_button.setCursor(Qt.PointingHandCursor)
        self.sudo_pw_button.setToolTip(
            "Set or clear your sudo password for hosts that require one (stored "
            "encrypted on this computer).")
        self.sudo_pw_button.clicked.connect(self.open_sudo_password)
        header_row.addWidget(self.sudo_pw_button)

        self.logout_button = QPushButton("Log Out")
        self.logout_button.setCursor(Qt.PointingHandCursor)
        self.logout_button.setToolTip(
            "End this session and return to the login screen"
        )
        self.logout_button.clicked.connect(bus.logout_requested.emit)
        header_row.addWidget(self.logout_button)
        self._refresh_signed_in()

        # Dark/light mode switch - lives here rather than buried in
        # Sysible Controller Settings since it's a personal display
        # preference an admin will want to flip without hunting for
        # it, not a piece of controller configuration.
        header_row.addWidget(ThemeToggle())

        outer.addLayout(header_row)

        self._apply_header_theme()
        theme.add_theme_listener(self._apply_header_theme)

        outer.addSpacing(18)

        # =========================================================
        # FEATURE SEARCH
        # Lets an admin type what they want to do ("create a user",
        # "add a repository", "restart a service") instead of having
        # to already know which tile - or, for anything under System
        # Administration, which of its seven sub-tiles - it lives
        # under. Matches against client/feature_search.py's
        # hand-curated registry; opens (and, where the destination has
        # named tabs, focuses) the right page directly.
        # =========================================================
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(
            "Search for a task, e.g. \"create a user\" or \"add a repository\"..."
        )
        self.search_input.textChanged.connect(self._update_search_results)
        self.search_input.returnPressed.connect(self._open_top_search_result)
        outer.addWidget(self.search_input)

        self.search_results = QListWidget()
        self.search_results.setMaximumHeight(150)
        self.search_results.itemClicked.connect(self._open_search_result_item)
        self.search_results.hide()
        outer.addWidget(self.search_results)

        outer.addSpacing(10)

        # =========================================================
        # DASHBOARD CARDS
        # User/group administration across enrolled hosts lives under
        # the System Administration tile (User & Group Administration)
        # - there's no separate top-level card for it here anymore, to
        # avoid the same workflow showing up in two places. The
        # "Sysible Controller Settings" card below covers both the admin login
        # that gates this dashboard AND the controller hostname/IP/
        # port settings - previously two separate cards, merged into
        # one page since both are "configure Sysible itself" concerns.
        # Formerly labeled "Sysible Administrator Configuration" -
        # renamed since the page now covers more than just
        # administrator accounts (controller config, admin password
        # policy, audit log). License key entry and the installed
        # version (formerly their own "Version & Licensing" card /
        # client/version_licensing_page.py) live there too now, as a
        # License & Version section - a single glance, not a workflow
        # that needed its own tile.
        # =========================================================
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(16)

        # The last field is superuser_only: tiles that manage the
        # controller itself (enroll/remove hosts, administrators + config,
        # the host-facing portal) are hidden from sysadmins, who can run
        # tools on hosts but not change the controller. The backend gates
        # these regardless (require_superuser); this just keeps a sysadmin's
        # dashboard from showing tiles whose pages would only 403.
        cards = [
            ("Fleet Health & Compliance",
             "Live fleet-health rollup per environment plus a read-only "
             "posture/compliance scan — drill into any host's full posture.",
             self.open_fleet_dashboard, "fa5s.heartbeat", "green", False),
            ("Fleet Performance",
             "CPU, memory, swap, disk, network and I/O time-series per "
             "environment — drill into a host for all its metrics and live detail.",
             self.open_fleet_performance, "fa5s.chart-line", "sky", False),
            ("Sysible Controller Host Enrollment",
             "Download the agent bundle and manage the enrolled host fleet.",
             self.open_hosts, "fa5s.server", "teal", True),
            ("Sysible Controller Settings",
             "Manage dashboard administrators, their password policy, the controller's address/port, and the audit log.",
             self.open_admin_config, "fa5s.cog", "slate", True),
            ("Sysible Connect",
             "Pop-out SSH/agent terminals (run as your own role user), RDP to a Windows host, "
             "file upload & download, run a script on all hosts, plus fleet power controls: "
             "reboot, power off, or restart the agent everywhere.",
             self.open_remote, "fa5s.terminal", "purple", False),
            ("System Administration",
             "User & group administration and system health/log checks across agent and SSH hosts.",
             self.open_system_admin, "fa5s.th-large", "amber", False),
            ("Live Activity & Logs",
             "Live, attributed feed of who did what across the fleet, plus the controller's own log.",
             self.open_live_log, "fa5s.stream", "sky", True),
        ]

        superuser = session.is_superuser()

        # Single column of tiles.
        grid.setColumnStretch(0, 1)
        row = 0
        for card_title, description, handler, icon, color, superuser_only in cards:
            if superuser_only and not superuser:
                continue
            grid.addWidget(
                DashboardCard(card_title, description, handler, icon, color),
                row, 0,
            )
            row += 1

        outer.addLayout(grid)
        outer.addStretch()

        # popouts
        self.fleet_dashboard_window = None
        self.fleet_performance_window = None
        self.host_window = None
        self.admin_config_window = None
        self.remote_window = None
        self.system_admin_window = None
        self.live_log_window = None

    def _apply_header_theme(self):
        """Re-color the title/subtitle for the current mode. They set
        their own color (rather than inheriting the app-level QSS's
        QLabel{color}) so they can be a couple shades softer/bolder
        than ordinary body text - which means, unlike a plain QLabel,
        they need this explicit refresh when the mode changes."""
        if theme.get_theme_mode() == "light":
            title_color, subtitle_color = "#1F2430", "#6B7280"
        else:
            title_color, subtitle_color = "#EAEAEA", "#9aa5b1"

        self.title_label.setStyleSheet(
            f"font-size:24px; font-weight:bold; color:{title_color};"
        )
        self.subtitle_label.setStyleSheet(
            f"font-size:11px; color:{subtitle_color};"
        )

        if theme.get_theme_mode() == "light":
            self.signed_in_label.setStyleSheet("font-size:11px; color:#6B7280;")
            chip = (
                "QPushButton{font-size:12px; padding:6px 14px; border-radius:6px;"
                "border:1px solid #C7CDD6; color:#1F2430; background:#F3F5F8;}"
                "QPushButton:hover{background:#E7EBF0;}"
            )
        else:
            self.signed_in_label.setStyleSheet("font-size:11px; color:#9aa5b1;")
            chip = (
                "QPushButton{font-size:12px; padding:6px 14px; border-radius:6px;"
                "border:1px solid #3A4250; color:#EAEAEA; background:#262C38;}"
                "QPushButton:hover{background:#313947;}"
            )
        self.logout_button.setStyleSheet(chip)
        self.sudo_pw_button.setStyleSheet(chip)

    def _refresh_signed_in(self):
        """Update the 'Signed in as <user>' label from the current session."""
        username = session.get_current_admin()
        if username:
            role = session.get_current_role()
            suffix = f" ({role})" if role else ""
            self.signed_in_label.setText(f"Signed in as {username}{suffix}")
            self.signed_in_label.setVisible(True)
        else:
            self.signed_in_label.setVisible(False)

    def _refresh_edition_badge(self):
        """Show a 'Community Edition' badge in the header. This is the Community
        build, so the badge is shown by default; the live host count is appended
        when the backend answers. Only an *explicit* unlimited signal from the
        backend (host_limit is None) hides it - a missing/failed /edition call
        (e.g. older backend) still shows the edition, just without the count."""
        try:
            info = api.get_edition()
        except Exception:
            info = {}

        # "host_limit" present and explicitly None => unlimited/Enterprise build.
        if "host_limit" in info and info["host_limit"] is None:
            self.edition_badge.setVisible(False)
            return

        limit = info.get("host_limit")
        count = info.get("host_count")
        text = "Community Edition"
        if isinstance(limit, int) and isinstance(count, int):
            text += f" · {count}/{limit} hosts"
        elif isinstance(limit, int):
            text += f" · up to {limit} hosts"

        self.edition_badge.setText(text)
        self.edition_badge.setStyleSheet(
            "background-color:#f5a623; color:#1b1b1b; font-weight:bold; "
            "padding:4px 12px; border-radius:11px; font-size:11px;"
        )
        self.edition_badge.setToolTip(
            "This is the Community edition. Contact Sysible for an Enterprise "
            "edition to manage more hosts."
        )
        self.edition_badge.setVisible(True)

    def open_fleet_dashboard(self):
        if self.fleet_dashboard_window is None:
            self.fleet_dashboard_window = FleetDashboardPage()
        self.fleet_dashboard_window.show()
        self.fleet_dashboard_window.raise_()
        return self.fleet_dashboard_window

    def open_fleet_performance(self):
        if self.fleet_performance_window is None:
            self.fleet_performance_window = FleetPerformancePage()
        self.fleet_performance_window.show()
        self.fleet_performance_window.raise_()
        return self.fleet_performance_window

    def open_hosts(self):
        if self.host_window is None:
            self.host_window = HostEnrollmentPage()
        self.host_window.show()
        self.host_window.raise_()
        return self.host_window

    def open_admin_config(self):
        if self.admin_config_window is None:
            self.admin_config_window = AdminConfigurationPage()
        self.admin_config_window.show()
        self.admin_config_window.raise_()
        return self.admin_config_window

    def open_remote(self):
        if self.remote_window is None:
            self.remote_window = RemoteAdministrationPage()
        self.remote_window.show()
        self.remote_window.raise_()
        return self.remote_window

    def open_portal(self):
        # The Webserver Portal admin is now embedded in the Host Enrollment
        # page (parity with the web console), so there's no standalone portal
        # window - feature-search "Webserver Portal" jumps there instead.
        return self.open_hosts()

    def open_live_log(self):
        if self.live_log_window is None:
            self.live_log_window = LiveLogPage()
        self.live_log_window.show()
        self.live_log_window.raise_()
        return self.live_log_window

    def open_system_admin(self):
        if self.system_admin_window is None:
            self.system_admin_window = SystemAdministrationPage()
        self.system_admin_window.show()
        self.system_admin_window.raise_()
        return self.system_admin_window

    def open_sudo_password(self):
        """Personal sudo-password store - reachable by every admin from the
        header, since sysadmins don't have the Settings tile. Host labels (for
        per-host overrides) are fetched best-effort; fleet-default works
        regardless."""
        from client.sudo_password_dialog import SudoPasswordDialog
        try:
            labels = sorted(h["label"] for h in api.list_merged_hosts(agent_only=False))
        except Exception:
            labels = []
        dlg = SudoPasswordDialog(self, host_labels=labels)
        dlg.exec()

    # =========================================================
    # FEATURE SEARCH
    # =========================================================
    def _update_search_results(self, text):
        matches = feature_search.search(text)
        self.search_results.clear()
        for entry in matches:
            item = QListWidgetItem(entry["title"])
            item.setData(Qt.UserRole, entry)
            self.search_results.addItem(item)
        self.search_results.setVisible(bool(matches))

    def _open_search_result_item(self, item):
        entry = item.data(Qt.UserRole)
        if entry:
            self.open_feature(entry)

    def _open_top_search_result(self):
        matches = feature_search.search(self.search_input.text())
        if matches:
            self.open_feature(matches[0])

    def open_feature(self, entry):
        """Open (and, if the entry names a tab, focus) the page a
        client/feature_search.py registry entry points to. Shared by
        clicking a search result and pressing Return in the search box."""
        window = getattr(self, entry["open"])()

        if entry.get("sub_open") and window is not None:
            window = getattr(window, entry["sub_open"])()

        if entry.get("tab") and window is not None and hasattr(window, "tabs"):
            tabs = window.tabs
            for i in range(tabs.count()):
                if tabs.tabText(i) == entry["tab"]:
                    tabs.setCurrentIndex(i)
                    break

        if window is not None:
            window.show()
            window.raise_()
            window.activateWindow()

        self.search_results.hide()
        return window
