"""Subscription & Licensing management for the commercial Linux distros -
Red Hat (subscription-manager / RHSM), Canonical/Ubuntu (Ubuntu Pro), and
SUSE (SUSEConnect) - dispatched across the checked hosts like every other
System Administration tool. See client/_api_subscriptions.py for the
command builders.
"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTabWidget, QComboBox, QCheckBox,
)

from client import api
from client import theme
from client.tab_sizing import shrink_tabwidget_to_current_page
from client.fleet_tool_page import FleetToolPage


class SubscriptionManagementPage(FleetToolPage):
    def __init__(self):
        super().__init__("Distro Subscription & Licensing")

    def build_action_tabs(self):
        tabs = QTabWidget()
        tabs.addTab(self._overview_tab(), "Overview")
        tabs.addTab(self._redhat_tab(), "Red Hat (RHSM)")
        tabs.addTab(self._ubuntu_tab(), "Ubuntu Pro")
        tabs.addTab(self._suse_tab(), "SUSE (SCC)")
        shrink_tabwidget_to_current_page(tabs, cap_height=True)
        return tabs

    @staticmethod
    def _hint(text):
        lbl = QLabel(text)
        theme.style_hint_label(lbl)
        lbl.setWordWrap(True)
        return lbl

    # ---------------- Overview ----------------
    def _overview_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        box, g = self.group("What is this host?")
        row = QHBoxLayout()
        b = QPushButton("Detect Distro && Subscription Tools")
        b.setStyleSheet("font-weight: bold;")
        b.clicked.connect(lambda: self.run_command(api.cmd_subscription_detect(), "Detect Subscription Tools"))
        row.addWidget(b)
        row.addStretch()
        g.addLayout(row)
        g.addWidget(self._hint(
            "Each vendor tab only works on its own distro: Red Hat (RHSM) on RHEL-family hosts, "
            "Ubuntu Pro on Ubuntu, SUSE (SCC) on SLES/openSUSE. Detect first to see which applies. "
            "Registration secrets (RHSM password, Pro token, SUSE reg-code) are passed to the "
            "vendor tool as it requires; prefer RHSM activation keys where you can."
        ))
        layout.addWidget(box)
        layout.addStretch()
        return panel

    # ---------------- Red Hat ----------------
    def _redhat_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        box, g = self.group("Status")
        row = QHBoxLayout()
        for label, cmd, lbl in [
            ("Subscription Status", api.cmd_rhsm_status, "RHSM Status"),
            ("Auto-Attach", api.cmd_rhsm_auto_attach, "RHSM Auto-Attach"),
            ("Refresh", api.cmd_rhsm_refresh, "RHSM Refresh"),
        ]:
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, c=cmd, l=lbl: self.run_command(c(), l))
            row.addWidget(b)
        row.addStretch()
        g.addLayout(row)
        layout.addWidget(box)

        box2, g2 = self.group("Register")
        g2.addWidget(self._hint(
            "Preferred: Organization (org ID) + one Activation Key — no password. "
            "Or use a Username + Password. Auto-attach pulls entitlements after registering."
        ))
        r1 = QHBoxLayout()
        r1.addWidget(QLabel("Organization:"))
        self.rhsm_org = QLineEdit(); self.rhsm_org.setPlaceholderText("org ID")
        r1.addWidget(self.rhsm_org, 1)
        r1.addWidget(QLabel("Activation Key:"))
        self.rhsm_ak = QLineEdit(); self.rhsm_ak.setPlaceholderText("activation key (preferred)")
        r1.addWidget(self.rhsm_ak, 1)
        g2.addLayout(r1)
        r2 = QHBoxLayout()
        r2.addWidget(QLabel("Username:"))
        self.rhsm_user = QLineEdit(); self.rhsm_user.setPlaceholderText("Red Hat login (optional)")
        r2.addWidget(self.rhsm_user, 1)
        r2.addWidget(QLabel("Password:"))
        self.rhsm_pass = QLineEdit(); self.rhsm_pass.setEchoMode(QLineEdit.Password)
        r2.addWidget(self.rhsm_pass, 1)
        self.rhsm_auto = QCheckBox("Auto-attach"); self.rhsm_auto.setChecked(True)
        r2.addWidget(self.rhsm_auto)
        b_reg = QPushButton("Register")
        b_reg.clicked.connect(self.run_rhsm_register)
        r2.addWidget(b_reg)
        g2.addLayout(r2)
        layout.addWidget(box2)

        box3, g3 = self.group("Subscriptions & Repositories")
        r3 = QHBoxLayout()
        for label, cmd, lbl in [
            ("List Consumed", api.cmd_rhsm_list_consumed, "RHSM Consumed"),
            ("List Available", api.cmd_rhsm_list_available, "RHSM Available"),
            ("List Repositories", api.cmd_rhsm_repos, "RHSM Repos"),
            ("Unregister", api.cmd_rhsm_unregister, "RHSM Unregister"),
        ]:
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, c=cmd, l=lbl: self.run_command(c(), l))
            r3.addWidget(b)
        g3.addLayout(r3)
        layout.addWidget(box3)
        layout.addStretch()
        return panel

    def run_rhsm_register(self):
        try:
            cmd = api.cmd_rhsm_register(
                self.rhsm_org.text(), self.rhsm_ak.text(),
                self.rhsm_user.text(), self.rhsm_pass.text(),
                self.rhsm_auto.isChecked(),
            )
        except ValueError as e:
            self.status_label.setText(str(e))
            return
        self.run_command(cmd, "RHSM Register")

    # ---------------- Ubuntu Pro ----------------
    def _ubuntu_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        box, g = self.group("Status & Attachment")
        r1 = QHBoxLayout()
        b_status = QPushButton("Pro Status")
        b_status.clicked.connect(lambda: self.run_command(api.cmd_pro_status(), "Pro Status"))
        r1.addWidget(b_status)
        b_refresh = QPushButton("Refresh")
        b_refresh.clicked.connect(lambda: self.run_command(api.cmd_pro_refresh(), "Pro Refresh"))
        r1.addWidget(b_refresh)
        b_detach = QPushButton("Detach")
        b_detach.clicked.connect(lambda: self.run_command(api.cmd_pro_detach(), "Pro Detach"))
        r1.addWidget(b_detach)
        r1.addStretch()
        g.addLayout(r1)
        r2 = QHBoxLayout()
        r2.addWidget(QLabel("Pro token:"))
        self.pro_token = QLineEdit(); self.pro_token.setEchoMode(QLineEdit.Password)
        self.pro_token.setPlaceholderText("token from ubuntu.com/pro/dashboard")
        r2.addWidget(self.pro_token, 1)
        b_attach = QPushButton("Attach")
        b_attach.clicked.connect(self.run_pro_attach)
        r2.addWidget(b_attach)
        g.addLayout(r2)
        layout.addWidget(box)

        box2, g2 = self.group("Services")
        g2.addWidget(self._hint(
            "Enable/disable Ubuntu Pro services on the checked hosts (the host must be attached first)."
        ))
        r3 = QHBoxLayout()
        r3.addWidget(QLabel("Service:"))
        self.pro_service = QComboBox()
        self.pro_service.addItems(api.PRO_SERVICES)
        r3.addWidget(self.pro_service)
        b_en = QPushButton("Enable")
        b_en.clicked.connect(lambda: self.run_pro_service(True))
        r3.addWidget(b_en)
        b_dis = QPushButton("Disable")
        b_dis.clicked.connect(lambda: self.run_pro_service(False))
        r3.addWidget(b_dis)
        r3.addStretch()
        g2.addLayout(r3)
        layout.addWidget(box2)
        layout.addStretch()
        return panel

    def run_pro_attach(self):
        try:
            cmd = api.cmd_pro_attach(self.pro_token.text())
        except ValueError as e:
            self.status_label.setText(str(e))
            return
        self.run_command(cmd, "Pro Attach")

    def run_pro_service(self, enable):
        service = self.pro_service.currentText()
        cmd = api.cmd_pro_enable(service) if enable else api.cmd_pro_disable(service)
        self.run_command(cmd, f"Pro {'Enable' if enable else 'Disable'} {service}")

    # ---------------- SUSE ----------------
    def _suse_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        box, g = self.group("Status")
        r1 = QHBoxLayout()
        b_status = QPushButton("Registration Status")
        b_status.clicked.connect(lambda: self.run_command(api.cmd_suse_status(), "SUSE Status"))
        r1.addWidget(b_status)
        b_ext = QPushButton("List Extensions / Modules")
        b_ext.clicked.connect(lambda: self.run_command(api.cmd_suse_list_extensions(), "SUSE Extensions"))
        r1.addWidget(b_ext)
        b_dereg = QPushButton("Deregister")
        b_dereg.clicked.connect(lambda: self.run_command(api.cmd_suse_deregister(), "SUSE Deregister"))
        r1.addWidget(b_dereg)
        r1.addStretch()
        g.addLayout(r1)
        layout.addWidget(box)

        box2, g2 = self.group("Register")
        g2.addWidget(self._hint("Register the system (and optionally its modules) with the SUSE Customer Center."))
        r2 = QHBoxLayout()
        r2.addWidget(QLabel("Registration code:"))
        self.suse_regcode = QLineEdit(); self.suse_regcode.setEchoMode(QLineEdit.Password)
        self.suse_regcode.setPlaceholderText("reg-code from scc.suse.com")
        r2.addWidget(self.suse_regcode, 1)
        r2.addWidget(QLabel("Email (optional):"))
        self.suse_email = QLineEdit(); self.suse_email.setPlaceholderText("account email")
        r2.addWidget(self.suse_email, 1)
        b_reg = QPushButton("Register")
        b_reg.clicked.connect(self.run_suse_register)
        r2.addWidget(b_reg)
        g2.addLayout(r2)
        layout.addWidget(box2)
        layout.addStretch()
        return panel

    def run_suse_register(self):
        try:
            cmd = api.cmd_suse_register(self.suse_regcode.text(), self.suse_email.text())
        except ValueError as e:
            self.status_label.setText(str(e))
            return
        self.run_command(cmd, "SUSE Register")
