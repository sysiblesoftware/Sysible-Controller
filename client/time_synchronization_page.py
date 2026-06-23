from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTabWidget,
)

from client import api
from client import theme
from client.tab_sizing import shrink_tabwidget_to_current_page
from client.fleet_tool_page import FleetToolPage


class TimeSynchronizationPage(FleetToolPage):
    """NTP/chrony configuration and verification, clock-drift
    troubleshooting, and time-zone management. See client/_api_timesync.py."""

    def __init__(self):
        super().__init__("Time Synchronization")

    def build_action_tabs(self):
        tabs = QTabWidget()
        tabs.addTab(self._ntp_tab(), "NTP && Chrony")
        tabs.addTab(self._timezone_tab(), "Time Zone")
        shrink_tabwidget_to_current_page(tabs)
        return tabs

    @staticmethod
    def _hint(text):
        lbl = QLabel(text)
        theme.style_hint_label(lbl)
        lbl.setWordWrap(True)
        return lbl

    # ---------------- NTP & Chrony ----------------
    def _ntp_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        box, g = self.group("Status & Verification")
        row = QHBoxLayout()
        b1 = QPushButton("Sync Status")
        b1.clicked.connect(lambda: self.run_command(api.cmd_timesync_status(), "Time Sync Status"))
        row.addWidget(b1)
        b2 = QPushButton("Verify Synchronization")
        b2.clicked.connect(lambda: self.run_command(api.cmd_verify_sync(), "Verify Synchronization"))
        row.addWidget(b2)
        b3 = QPushButton("Troubleshoot Clock Drift")
        b3.clicked.connect(lambda: self.run_command(api.cmd_troubleshoot_drift(), "Troubleshoot Clock Drift"))
        row.addWidget(b3)
        row.addStretch()
        g.addLayout(row)
        layout.addWidget(box)

        box2, g2 = self.group("Configure chrony / NTP")
        row2 = QHBoxLayout()
        b4 = QPushButton("Install & Enable chrony")
        b4.clicked.connect(lambda: self.run_command(api.cmd_configure_chrony(), "Configure chrony"))
        row2.addWidget(b4)
        row2.addStretch()
        g2.addLayout(row2)
        row3 = QHBoxLayout()
        row3.addWidget(QLabel("NTP servers:"))
        self.ntp_servers = QLineEdit()
        self.ntp_servers.setPlaceholderText("e.g. pool.ntp.org time.google.com (space-separated)")
        row3.addWidget(self.ntp_servers, 1)
        b5 = QPushButton("Set NTP Servers")
        b5.clicked.connect(lambda: self.run_with("Set NTP Servers", lambda: api.cmd_set_ntp_servers(self.ntp_servers.text())))
        row3.addWidget(b5)
        g2.addLayout(row3)
        g2.addWidget(self._hint("Install & Enable chrony first on a host that has neither chrony nor ntp. "
                                "Set NTP Servers rewrites chrony.conf's server list and restarts chrony "
                                "(a backup is kept alongside the config)."))
        layout.addWidget(box2)

        layout.addStretch()
        return panel

    # ---------------- Time Zone ----------------
    def _timezone_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        box, g = self.group("Time Zone")
        row = QHBoxLayout()
        row.addWidget(QLabel("Set time zone:"))
        self.tz_input = QLineEdit()
        self.tz_input.setPlaceholderText("e.g. America/New_York or UTC")
        row.addWidget(self.tz_input, 1)
        b = QPushButton("Set Time Zone")
        b.clicked.connect(lambda: self.run_with("Set Time Zone", lambda: api.cmd_set_timezone(self.tz_input.text())))
        row.addWidget(b)
        g.addLayout(row)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Find zone:"))
        self.tz_filter = QLineEdit()
        self.tz_filter.setPlaceholderText("e.g. york, london, utc")
        row2.addWidget(self.tz_filter, 1)
        b2 = QPushButton("List Time Zones")
        b2.clicked.connect(lambda: self.run_with("List Time Zones", lambda: api.cmd_list_timezones(self.tz_filter.text())))
        row2.addWidget(b2)
        g.addLayout(row2)
        g.addWidget(self._hint("List Time Zones with a filter to find the exact name, then paste it into Set Time Zone."))
        layout.addWidget(box)

        layout.addStretch()
        return panel
