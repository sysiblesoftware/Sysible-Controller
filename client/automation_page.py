from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTextEdit, QMessageBox, QTabWidget,
    QLineEdit,
)

from client import theme
from client.tab_sizing import shrink_tabwidget_to_current_page
from client.fleet_tool_page import FleetToolPage


class AutomationPage(FleetToolPage):
    """Ad-hoc command / script runner across the fleet - the general-purpose
    "automate a repetitive task" tool. Runs exactly what's entered, as root
    via the agent (or over SSH), on every checked host, with per-host output
    and exit code."""

    def __init__(self):
        super().__init__("Run A Script Across All Hosts")

    def build_action_tabs(self):
        tabs = QTabWidget()
        tabs.addTab(self._run_tab(), "Run Command / Script")
        shrink_tabwidget_to_current_page(tabs, cap_height=True)
        return tabs

    def _run_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        box, g = self.group("Run a Command or Script on Checked Hosts")
        self.script_input = QTextEdit()
        self.script_input.setPlaceholderText(
            "Enter a shell command or a multi-line script, e.g.\n\n"
            "#!/bin/sh\nuptime\ndf -h /\nsystemctl is-active nginx"
        )
        self.script_input.setStyleSheet("font-family: monospace;")
        self.script_input.setMinimumHeight(180)
        g.addWidget(self.script_input)

        sudo_row = QHBoxLayout()
        sudo_label = QLabel("Sudo password (optional):")
        self.sudo_input = QLineEdit()
        self.sudo_input.setEchoMode(QLineEdit.Password)
        self.sudo_input.setPlaceholderText("Only for hosts that require a sudo password — blank uses your stored one")
        sudo_row.addWidget(sudo_label)
        sudo_row.addWidget(self.sudo_input, 1)
        g.addLayout(sudo_row)

        row = QHBoxLayout()
        btn = QPushButton("Run on Checked Hosts")
        btn.setStyleSheet("font-weight: bold;")
        btn.clicked.connect(self.run_script)
        row.addWidget(btn)
        row.addStretch()
        g.addLayout(row)

        hint = QLabel(
            "Runs exactly what you enter, as root via the agent (or over SSH), on every checked "
            "host - output and exit code come back per host. Use it for one-off tasks and for "
            "automating repetitive commands across the fleet. Double-check before running anything "
            "destructive; it does precisely what you type."
        )
        theme.style_hint_label(hint)
        hint.setWordWrap(True)
        g.addWidget(hint)
        layout.addWidget(box)
        layout.addStretch()
        return panel

    def run_script(self):
        script = self.script_input.toPlainText().strip()
        if not script:
            QMessageBox.information(self, "Nothing to run", "Enter a command or script first.")
            return
        n = len(self.checked_entries())
        if n == 0:
            QMessageBox.information(self, "No hosts checked", "Check one or more target hosts first.")
            return
        preview = script if len(script) <= 240 else script[:240] + " ..."
        confirm = QMessageBox.question(
            self, "Run on checked hosts?",
            f"Run the following on {n} host(s) as root?\n\n{preview}",
        )
        if confirm != QMessageBox.Yes:
            return
        # Optional per-run sudo password for password-sudo hosts; blank falls
        # back to the workstation-local store inside api.run_on_entry.
        become = self.sudo_input.text() or None
        self.run_command(script, "Ran ad-hoc command/script", become_password=become)
