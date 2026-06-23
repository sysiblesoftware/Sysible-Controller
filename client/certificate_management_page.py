from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTabWidget,
)

from client import api
from client import theme
from client.tab_sizing import shrink_tabwidget_to_current_page
from client.fleet_tool_page import FleetToolPage


class CertificateManagementPage(FleetToolPage):
    """CSR generation, certificate install/renew/replace, chain handling,
    and TLS troubleshooting. See client/_api_certs.py."""

    def __init__(self):
        super().__init__("Certificate Management")

    def build_action_tabs(self):
        tabs = QTabWidget()
        tabs.addTab(self._certs_tab(), "Certificates")
        tabs.addTab(self._tls_tab(), "Chain && TLS")
        shrink_tabwidget_to_current_page(tabs)
        return tabs

    @staticmethod
    def _hint(text):
        lbl = QLabel(text)
        theme.style_hint_label(lbl)
        lbl.setWordWrap(True)
        return lbl

    # ---------------- Certificates ----------------
    def _certs_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        box, g = self.group("Generate CSR")
        row = QHBoxLayout()
        row.addWidget(QLabel("Common Name:"))
        self.csr_cn = QLineEdit()
        self.csr_cn.setPlaceholderText("e.g. www.example.com")
        row.addWidget(self.csr_cn, 1)
        row.addWidget(QLabel("Organization:"))
        self.csr_org = QLineEdit()
        self.csr_org.setPlaceholderText("optional")
        row.addWidget(self.csr_org, 1)
        b = QPushButton("Generate CSR")
        b.clicked.connect(lambda: self.run_with("Generate CSR", lambda: api.cmd_generate_csr(self.csr_cn.text(), self.csr_org.text())))
        row.addWidget(b)
        g.addLayout(row)
        g.addWidget(self._hint("Creates a 2048-bit key + CSR under /etc/ssl/sysible and prints the CSR to submit to a CA."))
        layout.addWidget(box)

        box2, g2 = self.group("Install Certificate")
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Cert file:"))
        self.inst_cert = QLineEdit()
        self.inst_cert.setPlaceholderText("e.g. /tmp/www.example.com.crt")
        row2.addWidget(self.inst_cert, 1)
        row2.addWidget(QLabel("Key file:"))
        self.inst_key = QLineEdit()
        self.inst_key.setPlaceholderText("optional")
        row2.addWidget(self.inst_key, 1)
        b2 = QPushButton("Install")
        b2.clicked.connect(lambda: self.run_with("Install Certificate", lambda: api.cmd_install_certificate(self.inst_cert.text(), self.inst_key.text())))
        row2.addWidget(b2)
        g2.addLayout(row2)
        g2.addWidget(self._hint("Validates the certificate, then installs it (mode 644) and the key (mode 600) into /etc/ssl/sysible."))
        layout.addWidget(box2)

        box3, g3 = self.group("Check / Renew / Replace")
        row3 = QHBoxLayout()
        row3.addWidget(QLabel("Certificate file:"))
        self.check_cert = QLineEdit()
        self.check_cert.setPlaceholderText("e.g. /etc/ssl/sysible/www.example.com.crt")
        row3.addWidget(self.check_cert, 1)
        b3 = QPushButton("Check Expiry")
        b3.clicked.connect(lambda: self.run_with("Check Certificate", lambda: api.cmd_check_certificate(self.check_cert.text())))
        row3.addWidget(b3)
        g3.addLayout(row3)
        row4 = QHBoxLayout()
        row4.addWidget(QLabel("certbot domain (blank = renew all):"))
        self.renew_domain = QLineEdit()
        self.renew_domain.setPlaceholderText("e.g. www.example.com")
        row4.addWidget(self.renew_domain, 1)
        b4 = QPushButton("Renew via certbot")
        b4.clicked.connect(lambda: self.run_with("Renew Certificate", lambda: api.cmd_renew_certbot(self.renew_domain.text())))
        row4.addWidget(b4)
        g3.addLayout(row4)
        g3.addWidget(self._hint("Check Expiry flags certificates that are expired or due within 30 days (replace those). "
                                "Renew uses certbot for Let's Encrypt certificates."))
        layout.addWidget(box3)

        layout.addStretch()
        return panel

    # ---------------- Chain & TLS ----------------
    def _tls_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        box, g = self.group("Certificate Chain")
        row = QHBoxLayout()
        row.addWidget(QLabel("Certificate:"))
        self.chain_cert = QLineEdit()
        self.chain_cert.setPlaceholderText("e.g. /etc/ssl/sysible/www.example.com.crt")
        row.addWidget(self.chain_cert, 1)
        row.addWidget(QLabel("Intermediate chain:"))
        self.chain_file = QLineEdit()
        self.chain_file.setPlaceholderText("optional, e.g. /etc/ssl/chain.pem")
        row.addWidget(self.chain_file, 1)
        b = QPushButton("Verify Chain")
        b.clicked.connect(lambda: self.run_with("Verify Certificate Chain", lambda: api.cmd_verify_chain(self.chain_cert.text(), self.chain_file.text())))
        row.addWidget(b)
        g.addLayout(row)
        g.addWidget(self._hint("Verifies the certificate (optionally against an intermediate chain) and lists the issuer chain."))
        layout.addWidget(box)

        box2, g2 = self.group("Troubleshoot TLS")
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Host:"))
        self.tls_host = QLineEdit()
        self.tls_host.setPlaceholderText("e.g. example.com")
        row2.addWidget(self.tls_host, 1)
        row2.addWidget(QLabel("Port:"))
        self.tls_port = QLineEdit("443")
        self.tls_port.setMaximumWidth(70)
        row2.addWidget(self.tls_port)
        b2 = QPushButton("Test TLS")
        b2.clicked.connect(lambda: self.run_with("Troubleshoot TLS", lambda: api.cmd_troubleshoot_tls(self.tls_host.text(), self.tls_port.text())))
        row2.addWidget(b2)
        g2.addLayout(row2)
        g2.addWidget(self._hint("Connects with openssl s_client and reports the presented chain, validity, and verification result."))
        layout.addWidget(box2)

        layout.addStretch()
        return panel
