from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTabWidget,
    QMessageBox,
)

from client import api
from client import theme
from client.tab_sizing import shrink_tabwidget_to_current_page
from client.fleet_tool_page import FleetToolPage


class QuickSystemActionsPage(FleetToolPage):
    """One-click common remediations against the checked hosts: reboot / power
    off, restart a service (NetworkManager, SSH, time sync, or any by name),
    flush DNS, clear failed units, and reload systemd. Every button delegates
    to an existing cmd_* builder — nothing new runs on the host that couldn't
    already be run from Service Management or a raw command."""

    def __init__(self):
        super().__init__("Quick System Actions")

    def build_action_tabs(self):
        tabs = QTabWidget()
        tabs.addTab(self._common_tab(), "Common Fixes")
        tabs.addTab(self._power_tab(), "Power")
        shrink_tabwidget_to_current_page(tabs, cap_height=True)
        return tabs

    @staticmethod
    def _hint(text):
        lbl = QLabel(text)
        theme.style_hint_label(lbl)
        lbl.setWordWrap(True)
        return lbl

    # ---------------- Common Fixes ----------------
    def _common_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        box, g = self.group("Restart a service")
        row = QHBoxLayout()
        row.addWidget(QLabel("Service name:"))
        self.svc_input = QLineEdit()
        self.svc_input.setPlaceholderText("e.g. nginx, docker, postgresql")
        row.addWidget(self.svc_input, 1)
        b = QPushButton("Restart Service")
        b.clicked.connect(lambda: self.run_with(
            "Restart Service", lambda: api.cmd_service_restart(self.svc_input.text())))
        row.addWidget(b)
        g.addLayout(row)
        g.addWidget(self._hint("Restarts any systemd service by name on every checked host."))
        layout.addWidget(box)

        box2, g2 = self.group("Networking")
        row2 = QHBoxLayout()
        b_nm = QPushButton("Restart NetworkManager")
        b_nm.clicked.connect(lambda: self.run_command(
            api.cmd_service_restart("NetworkManager"), "Restart NetworkManager"))
        row2.addWidget(b_nm)
        b_dns = QPushButton("Flush DNS Cache")
        b_dns.clicked.connect(lambda: self.run_command(api.cmd_flush_dns(), "Flush DNS Cache"))
        row2.addWidget(b_dns)
        b_ssh = QPushButton("Restart SSH Server")
        b_ssh.clicked.connect(lambda: self.run_command(api.cmd_restart_ssh(), "Restart SSH Server"))
        row2.addWidget(b_ssh)
        row2.addStretch()
        g2.addLayout(row2)
        layout.addWidget(box2)

        box3, g3 = self.group("Systemd housekeeping")
        row3 = QHBoxLayout()
        b_time = QPushButton("Restart Time Sync")
        b_time.clicked.connect(lambda: self.run_command(api.cmd_restart_timesync(), "Restart Time Sync"))
        row3.addWidget(b_time)
        b_rf = QPushButton("Clear Failed Units")
        b_rf.clicked.connect(lambda: self.run_command(api.cmd_reset_failed_units(), "Clear Failed Units"))
        row3.addWidget(b_rf)
        b_dr = QPushButton("Reload systemd")
        b_dr.clicked.connect(lambda: self.run_command(api.cmd_daemon_reload(), "Reload systemd"))
        row3.addWidget(b_dr)
        row3.addStretch()
        g3.addLayout(row3)
        g3.addWidget(self._hint("Clear Failed Units wipes stale 'failed' markers (it does not start "
                                "anything). Reload systemd re-reads unit files after they're edited."))
        layout.addWidget(box3)

        layout.addStretch()
        return panel

    # ---------------- Power ----------------
    def _power_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        box, g = self.group("Power (careful — affects the whole host)")
        row = QHBoxLayout()
        b_reboot = QPushButton("Reboot Host(s)")
        b_reboot.clicked.connect(lambda: self._confirm_power(
            api.cmd_reboot_host(), "Reboot Host",
            "Reboot every checked host now?"))
        row.addWidget(b_reboot)
        b_off = QPushButton("Power Off Host(s)")
        b_off.clicked.connect(lambda: self._confirm_power(
            api.cmd_poweroff_host(), "Power Off Host",
            "Power off every checked host now?\n\nThey will NOT come back until "
            "powered on out-of-band."))
        row.addWidget(b_off)
        row.addStretch()
        g.addLayout(row)
        g.addWidget(self._hint("Reboot schedules 'shutdown -r +0'; power off schedules "
                               "'shutdown -P +0'. The host reports the command was accepted before "
                               "it goes down."))
        layout.addWidget(box)

        layout.addStretch()
        return panel

    def _confirm_power(self, command, label, prompt):
        entries = self.checked_entries()
        if not entries:
            QMessageBox.information(self, "No hosts checked", "Check one or more target hosts first.")
            return
        names = ", ".join(e["label"] for e in entries[:8])
        if len(entries) > 8:
            names += f", … (+{len(entries) - 8} more)"
        resp = QMessageBox.question(
            self, label, f"{prompt}\n\nHosts: {names}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if resp == QMessageBox.Yes:
            self.run_command(command, label)
