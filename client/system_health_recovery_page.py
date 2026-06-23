"""Combined "System Health, Logs & Recovery" window.

System Health & Logs and System Boot & Recovery are both "is this host
healthy, and how do I fix it when it isn't?" tools, so they're presented
together as two top-level tabs of a single window rather than two
separate dashboard tiles. Each tab embeds the existing page unchanged
(client/system_health_logs_page.py and client/system_boot_recovery_page.py),
so all their behaviour - host checklist, action sub-tabs, per-host result
tabs - works exactly as before; this just hosts them side by side.
"""
from PySide6.QtWidgets import QWidget, QVBoxLayout, QTabWidget

from client.system_health_logs_page import SystemHealthLogsPage
from client.system_boot_recovery_page import SystemBootRecoveryPage


class SystemHealthRecoveryPage(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("System Health, Logs & Recovery")
        self.resize(1320, 820)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.tabs = QTabWidget()
        self.health_page = SystemHealthLogsPage()
        self.boot_page = SystemBootRecoveryPage()
        self.tabs.addTab(self.health_page, "Health && Logs")
        self.tabs.addTab(self.boot_page, "Boot && Recovery")
        layout.addWidget(self.tabs)

    # Let the dashboard's feature search jump straight to the right half.
    def show_health(self):
        self.tabs.setCurrentWidget(self.health_page)
        return self

    def show_boot(self):
        self.tabs.setCurrentWidget(self.boot_page)
        return self
