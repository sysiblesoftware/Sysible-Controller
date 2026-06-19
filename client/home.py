from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem,
)
from PySide6.QtGui import QPixmap
from PySide6.QtCore import Qt

from client.dashboard_card import DashboardCard
from client.host_enrollment_page import HostEnrollmentPage
from client.admin_configuration_page import AdminConfigurationPage
from client.remote_administration_page import RemoteAdministrationPage
from client.webserver_portal_page import WebserverPortalPage
from client.system_administration_page import SystemAdministrationPage
from client.branding import LOGO_PATH
from client.theme_toggle import ThemeToggle
from client import feature_search, theme


class HomeWindow(QWidget):

    def __init__(self):
        super().__init__()

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
        for col in range(2):
            grid.setColumnStretch(col, 1)

        cards = [
            ("Sysible Controller Host Enrollment",
             "Download the agent bundle and manage the enrolled host fleet.",
             self.open_hosts, "fa5s.server", "teal"),
            ("Sysible Controller Settings",
             "Manage dashboard administrators, their password policy, the controller's address/port, and the audit log.",
             self.open_admin_config, "fa5s.cog", "slate"),
            ("Remote Host Administration",
             "SSH and agent terminal access, plus environment tagging for managed hosts.",
             self.open_remote, "fa5s.terminal", "purple"),
            ("Webserver Portal Configuration",
             "Run the host-facing portal for agent downloads and file transfers.",
             self.open_portal, "fa5s.globe", "coral"),
            ("System Administration",
             "User & group administration and system health/log checks across agent and SSH hosts.",
             self.open_system_admin, "fa5s.th-large", "amber"),
        ]

        for index, (card_title, description, handler, icon, color) in enumerate(cards):
            row, col = divmod(index, 2)
            grid.addWidget(
                DashboardCard(card_title, description, handler, icon, color),
                row, col,
            )

        outer.addLayout(grid)
        outer.addStretch()

        # popouts
        self.host_window = None
        self.admin_config_window = None
        self.remote_window = None
        self.portal_window = None
        self.system_admin_window = None

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
        if self.portal_window is None:
            self.portal_window = WebserverPortalPage()
        self.portal_window.show()
        self.portal_window.raise_()
        return self.portal_window

    def open_system_admin(self):
        if self.system_admin_window is None:
            self.system_admin_window = SystemAdministrationPage()
        self.system_admin_window.show()
        self.system_admin_window.raise_()
        return self.system_admin_window

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
