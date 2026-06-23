from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTabWidget,
    QCheckBox, QComboBox,
)

from client import api
from client import theme
from client.tab_sizing import shrink_tabwidget_to_current_page
from client.fleet_tool_page import FleetToolPage


class DirectoryServicesPage(FleetToolPage):
    """Join hosts to Active Directory (realmd/SSSD), manage realm status and
    login permits, and configure/test LDAP/LDAPS. See client/_api_directory.py."""

    def __init__(self):
        super().__init__("Directory Services (AD / LDAP)")

    def build_action_tabs(self):
        tabs = QTabWidget()
        tabs.addTab(self._ad_tab(), "Active Directory")
        tabs.addTab(self._ldap_tab(), "LDAP / LDAPS")
        shrink_tabwidget_to_current_page(tabs, cap_height=True)
        return tabs

    @staticmethod
    def _hint(text):
        lbl = QLabel(text)
        theme.style_hint_label(lbl)
        lbl.setWordWrap(True)
        return lbl

    # ---------------- Active Directory ----------------
    def _ad_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        box, g = self.group("Status")
        row = QHBoxLayout()
        b = QPushButton("Realm / Domain Status")
        b.clicked.connect(lambda: self.run_command(api.cmd_realm_status(), "Realm Status"))
        row.addWidget(b)
        b2 = QPushButton("Enable Home-Dir Creation")
        b2.clicked.connect(lambda: self.run_command(api.cmd_enable_mkhomedir(), "Enable mkhomedir"))
        row.addWidget(b2)
        row.addStretch()
        g.addLayout(row)
        layout.addWidget(box)

        box2, g2 = self.group("Join Active Directory")
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Domain:"))
        self.ad_domain = QLineEdit()
        self.ad_domain.setPlaceholderText("e.g. corp.example.com")
        row2.addWidget(self.ad_domain, 1)
        row2.addWidget(QLabel("Join account:"))
        self.ad_user = QLineEdit()
        self.ad_user.setPlaceholderText("e.g. Administrator")
        row2.addWidget(self.ad_user, 1)
        g2.addLayout(row2)
        row3 = QHBoxLayout()
        row3.addWidget(QLabel("Password:"))
        self.ad_pass = QLineEdit()
        self.ad_pass.setEchoMode(QLineEdit.Password)
        row3.addWidget(self.ad_pass, 1)
        row3.addWidget(QLabel("Computer OU:"))
        self.ad_ou = QLineEdit()
        self.ad_ou.setPlaceholderText("optional, e.g. OU=Servers,DC=corp,DC=example,DC=com")
        row3.addWidget(self.ad_ou, 1)
        btn_join = QPushButton("Join Domain")
        btn_join.setStyleSheet("font-weight: bold;")
        btn_join.clicked.connect(self.run_join_ad)
        row3.addWidget(btn_join)
        g2.addLayout(row3)
        g2.addWidget(self._hint(
            "Installs the AD client tooling (realmd, SSSD, adcli, Kerberos) and joins the domain. "
            "Requires working DNS for the domain and the host clock in sync with the DC. The join "
            "password is fed to realm via a transient root-only file, never the command line."
        ))
        layout.addWidget(box2)

        box3, g3 = self.group("Logins & Leave")
        row4 = QHBoxLayout()
        row4.addWidget(QLabel("Permit user/group:"))
        self.ad_permit = QLineEdit()
        self.ad_permit.setPlaceholderText("e.g. jdoe@corp.example.com  or a group name")
        row4.addWidget(self.ad_permit, 1)
        self.ad_permit_group = QCheckBox("Is a group")
        row4.addWidget(self.ad_permit_group)
        btn_permit = QPushButton("Permit Login")
        btn_permit.clicked.connect(self.run_permit)
        row4.addWidget(btn_permit)
        g3.addLayout(row4)
        row5 = QHBoxLayout()
        row5.addWidget(QLabel("Leave domain:"))
        self.ad_leave_domain = QLineEdit()
        self.ad_leave_domain.setPlaceholderText("e.g. corp.example.com")
        row5.addWidget(self.ad_leave_domain, 1)
        btn_leave = QPushButton("Leave Domain")
        btn_leave.clicked.connect(self.run_leave_ad)
        row5.addWidget(btn_leave)
        g3.addLayout(row5)
        g3.addWidget(self._hint("After a join, realm denies all logins by default - permit the AD users or "
                                "groups that should be allowed to log in."))
        layout.addWidget(box3)

        layout.addStretch()
        return panel

    def run_join_ad(self):
        self.run_with("Join Active Directory", lambda: api.cmd_join_ad(
            self.ad_domain.text(), self.ad_user.text(), self.ad_pass.text(), self.ad_ou.text()))

    def run_permit(self):
        self.run_with("Permit Login", lambda: api.cmd_realm_permit(
            self.ad_permit.text(), self.ad_permit_group.isChecked()))

    def run_leave_ad(self):
        self.run_with("Leave Domain", lambda: api.cmd_leave_ad(self.ad_leave_domain.text()))

    # ---------------- LDAP / LDAPS ----------------
    def _ldap_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        box, g = self.group("Test LDAPS Connectivity")
        row = QHBoxLayout()
        row.addWidget(QLabel("Server:"))
        self.ldaps_server = QLineEdit()
        self.ldaps_server.setPlaceholderText("e.g. ldap.example.com")
        row.addWidget(self.ldaps_server, 1)
        row.addWidget(QLabel("Port:"))
        self.ldaps_port = QLineEdit("636")
        self.ldaps_port.setMaximumWidth(70)
        row.addWidget(self.ldaps_port)
        row.addWidget(QLabel("Base DN:"))
        self.ldaps_base = QLineEdit()
        self.ldaps_base.setPlaceholderText("optional, e.g. dc=example,dc=com")
        row.addWidget(self.ldaps_base, 1)
        b = QPushButton("Test")
        b.clicked.connect(lambda: self.run_with("Test LDAPS", lambda: api.cmd_test_ldaps(
            self.ldaps_server.text(), self.ldaps_port.text(), self.ldaps_base.text())))
        row.addWidget(b)
        g.addLayout(row)
        g.addWidget(self._hint("Checks the TLS handshake and certificate, and (with a base DN) runs an "
                               "anonymous LDAP search."))
        layout.addWidget(box)

        box2, g2 = self.group("Configure LDAP(S) Client (SSSD)")
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Server:"))
        self.ldap_server = QLineEdit()
        self.ldap_server.setPlaceholderText("e.g. ldap.example.com")
        row2.addWidget(self.ldap_server, 1)
        row2.addWidget(QLabel("Base DN:"))
        self.ldap_base = QLineEdit()
        self.ldap_base.setPlaceholderText("e.g. dc=example,dc=com")
        row2.addWidget(self.ldap_base, 1)
        row2.addWidget(QLabel("Protocol:"))
        self.ldap_scheme = QComboBox()
        self.ldap_scheme.addItems(["ldaps", "ldap+starttls"])
        row2.addWidget(self.ldap_scheme)
        b2 = QPushButton("Apply")
        b2.clicked.connect(self.run_configure_ldap)
        row2.addWidget(b2)
        g2.addLayout(row2)
        g2.addWidget(self._hint("Writes a basic /etc/sssd/sssd.conf for a generic (non-AD) LDAP directory and "
                                "restarts SSSD. For Active Directory, use Join Active Directory instead."))
        layout.addWidget(box2)

        layout.addStretch()
        return panel

    def run_configure_ldap(self):
        use_ldaps = self.ldap_scheme.currentText() == "ldaps"
        self.run_with("Configure LDAP Client", lambda: api.cmd_configure_ldap_client(
            self.ldap_server.text(), self.ldap_base.text(), use_ldaps))
