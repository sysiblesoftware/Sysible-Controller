from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTabWidget, QComboBox,
)

from client import api
from client import theme
from client.tab_sizing import shrink_tabwidget_to_current_page
from client.fleet_tool_page import FleetToolPage


class ContainersVMsPage(FleetToolPage):
    """Docker/Podman containers and images, plus libvirt virtual machines.
    See client/_api_containers.py."""

    def __init__(self):
        super().__init__("Containers & VMs")

    def build_action_tabs(self):
        tabs = QTabWidget()
        tabs.addTab(self._containers_tab(), "Containers")
        tabs.addTab(self._vms_tab(), "Virtual Machines")
        shrink_tabwidget_to_current_page(tabs, cap_height=True)
        return tabs

    @staticmethod
    def _hint(text):
        lbl = QLabel(text)
        theme.style_hint_label(lbl)
        lbl.setWordWrap(True)
        return lbl

    # ---------------- Containers ----------------
    def _containers_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        box, g = self.group("Overview")
        row = QHBoxLayout()
        b1 = QPushButton("Detect Runtime")
        b1.clicked.connect(lambda: self.run_command(api.cmd_container_runtime(), "Container Runtime"))
        row.addWidget(b1)
        b2 = QPushButton("List Containers")
        b2.clicked.connect(lambda: self.run_command(api.cmd_list_containers(True), "List Containers"))
        row.addWidget(b2)
        b3 = QPushButton("List Images")
        b3.clicked.connect(lambda: self.run_command(api.cmd_list_images(), "List Images"))
        row.addWidget(b3)
        b4 = QPushButton("Prune")
        b4.clicked.connect(lambda: self.run_command(api.cmd_container_prune(), "Prune Containers"))
        row.addWidget(b4)
        row.addStretch()
        g.addLayout(row)
        g.addWidget(self._hint("Uses docker if present, otherwise podman (CLI-compatible)."))
        layout.addWidget(box)

        box2, g2 = self.group("Container Actions")
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Container:"))
        self.container_name = QLineEdit()
        self.container_name.setPlaceholderText("name or ID")
        row2.addWidget(self.container_name, 1)
        row2.addWidget(QLabel("Action:"))
        self.container_action = QComboBox()
        self.container_action.addItems(["start", "stop", "restart", "pause", "unpause", "rm"])
        row2.addWidget(self.container_action)
        b = QPushButton("Run")
        b.clicked.connect(self.run_container_action)
        row2.addWidget(b)
        g2.addLayout(row2)
        row3 = QHBoxLayout()
        row3.addWidget(QLabel("Logs - tail:"))
        self.container_log_lines = QLineEdit("200")
        self.container_log_lines.setMaximumWidth(60)
        row3.addWidget(self.container_log_lines)
        b2 = QPushButton("View Container Logs")
        b2.clicked.connect(self.run_container_logs)
        row3.addWidget(b2)
        row3.addStretch()
        g2.addLayout(row3)
        layout.addWidget(box2)

        layout.addStretch()
        return panel

    def run_container_action(self):
        self.run_with(f"Container {self.container_action.currentText()}",
                      lambda: api.cmd_container_action(self.container_action.currentText(), self.container_name.text()))

    def run_container_logs(self):
        try:
            lines = int(self.container_log_lines.text() or "200")
        except ValueError:
            lines = 200
        self.run_with("Container Logs",
                      lambda: api.cmd_container_logs(self.container_name.text(), lines))

    # ---------------- VMs ----------------
    def _vms_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        box, g = self.group("Virtual Machines (libvirt)")
        row = QHBoxLayout()
        b = QPushButton("List VMs")
        b.clicked.connect(lambda: self.run_command(api.cmd_list_vms(), "List VMs"))
        row.addWidget(b)
        row.addStretch()
        g.addLayout(row)
        layout.addWidget(box)

        box2, g2 = self.group("VM Actions")
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("VM (domain):"))
        self.vm_name = QLineEdit()
        self.vm_name.setPlaceholderText("domain name")
        row2.addWidget(self.vm_name, 1)
        row2.addWidget(QLabel("Action:"))
        self.vm_action = QComboBox()
        self.vm_action.addItems(["start", "shutdown", "reboot", "suspend", "resume", "destroy"])
        row2.addWidget(self.vm_action)
        b2 = QPushButton("Run")
        b2.clicked.connect(self.run_vm_action)
        row2.addWidget(b2)
        b3 = QPushButton("VM Info")
        b3.clicked.connect(lambda: self.run_with("VM Info", lambda: api.cmd_vm_info(self.vm_name.text())))
        row2.addWidget(b3)
        g2.addLayout(row2)
        g2.addWidget(self._hint("shutdown = graceful; destroy = force power-off. Requires libvirt (virsh) on the host."))
        layout.addWidget(box2)

        layout.addStretch()
        return panel

    def run_vm_action(self):
        self.run_with(f"VM {self.vm_action.currentText()}",
                      lambda: api.cmd_vm_action(self.vm_action.currentText(), self.vm_name.text()))
