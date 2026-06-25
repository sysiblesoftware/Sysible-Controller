"""
RDP connect dialog for Sysible Connect: collects host/credentials/display
options, launches a local RDP client, and (optionally) remembers the
credentials per host with the password encrypted at rest.
"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QComboBox, QCheckBox, QMessageBox, QApplication,
)
from PySide6.QtCore import Qt

from client import rdp_launcher, rdp_credentials

_SIZES = [
    ("Fit my screen (recommended)", "dynamic"),
    ("Full screen", "fullscreen"),
    ("1920 × 1080 (windowed)", "1920x1080"),
    ("1280 × 800 (windowed)", "1280x800"),
    ("1024 × 768 (windowed)", "1024x768"),
]


class RdpConnectDialog(QDialog):
    def __init__(self, host="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Open RDP Session")
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)

        intro = QLabel("Connect to a host over RDP using a local RDP client.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # Editable dropdown: free-form to type a new address, but also lists
        # every host whose RDP details were remembered, so reconnecting is a
        # pick rather than a retype. Selecting one prefills its credentials.
        self.host_input = QComboBox()
        self.host_input.setEditable(True)
        self.host_input.lineEdit().setPlaceholderText("hostname or IP (optionally host:port)")
        saved = rdp_credentials.list_hosts()
        self.host_input.addItems(saved)
        self.host_input.setCurrentText(host or "")
        self.host_input.currentTextChanged.connect(self._on_host_changed)
        layout.addLayout(self._row("Host / address:", self.host_input))

        self.user_input = QLineEdit()
        self.user_input.setPlaceholderText("e.g. Administrator")
        layout.addLayout(self._row("Username:", self.user_input))

        self.domain_input = QLineEdit()
        self.domain_input.setPlaceholderText("optional, e.g. CORP")
        layout.addLayout(self._row("Domain:", self.domain_input))

        self.pass_input = QLineEdit()
        self.pass_input.setEchoMode(QLineEdit.Password)
        self.pass_input.setPlaceholderText("leave blank to be prompted by the client")
        layout.addLayout(self._row("Password:", self.pass_input))

        self.size_combo = QComboBox()
        for label, value in _SIZES:
            self.size_combo.addItem(label, value)
        layout.addLayout(self._row("Display:", self.size_combo))

        self.remember = QCheckBox("Remember for this host (password encrypted)")
        self.remember.setEnabled(rdp_credentials.encryption_available() or True)
        layout.addWidget(self.remember)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#9aa5b1; font-size:11px;")
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setDefault(True)
        self.connect_btn.clicked.connect(self._connect)
        buttons.addWidget(cancel)
        buttons.addWidget(self.connect_btn)
        layout.addLayout(buttons)

        # Prefill from remembered credentials for this host.
        self._prefill(host)

        if rdp_launcher.available_client() is None:
            self.status.setText(
                "No RDP client detected. Install FreeRDP (xfreerdp) or Remmina on this "
                "machine to use RDP.")
        elif not rdp_credentials.encryption_available():
            self.status.setText(
                "Note: encryption library unavailable - a remembered password will not "
                "be stored (username/domain still will).")

    @staticmethod
    def _row(label, widget):
        row = QHBoxLayout()
        lbl = QLabel(label)
        lbl.setMinimumWidth(110)
        row.addWidget(lbl)
        row.addWidget(widget, 1)
        return row

    def _prefill(self, host):
        if not host:
            return
        saved = rdp_credentials.load(host)
        if saved:
            self.user_input.setText(saved.get("username", ""))
            self.domain_input.setText(saved.get("domain", ""))
            self.pass_input.setText(saved.get("password", ""))
            self.remember.setChecked(True)

    def _on_host_changed(self, text):
        # When the typed/picked host matches a remembered one, fill in its
        # saved username/domain/password automatically.
        if rdp_credentials.is_remembered((text or "").strip()):
            self._prefill(text.strip())

    def _connect(self):
        host = self.host_input.currentText().strip()
        if not host:
            QMessageBox.warning(self, "Missing host", "Enter a host or address.")
            return
        username = self.user_input.text().strip()
        domain = self.domain_input.text().strip()
        password = self.pass_input.text()
        size = self.size_combo.currentData()

        if self.remember.isChecked():
            rdp_credentials.save(host, username, domain, password)
        else:
            rdp_credentials.forget(host)

        # launch() watches the client for a couple of seconds to catch an
        # immediate failure, so give feedback and keep the UI responsive
        # rather than appearing to freeze.
        self.connect_btn.setEnabled(False)
        self.status.setText(f"Connecting to {host}…")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        # Local screen size → FreeRDP opens the desktop at full *physical*
        # resolution (crisp) instead of a smaller default that gets upscaled
        # blurry. availableGeometry() is in LOGICAL pixels, so on a HiDPI
        # display (devicePixelRatio > 1) it's smaller than the real panel - we
        # multiply by the ratio to get true pixels, which is the resolution the
        # remote desktop should actually render at.
        screen_size = None
        try:
            scr = QApplication.primaryScreen()
            if scr is not None:
                g = scr.availableGeometry()
                dpr = scr.devicePixelRatio() or 1.0
                w = int(round(g.width() * dpr))
                h = int(round(g.height() * dpr))
                screen_size = f"{w}x{h}"
        except Exception:
            screen_size = None
        try:
            ok, message = rdp_launcher.launch(host, username, domain, password, size,
                                              screen_size=screen_size)
        finally:
            QApplication.restoreOverrideCursor()
            self.connect_btn.setEnabled(True)

        if not ok:
            self.status.setText("")
            QMessageBox.critical(self, "RDP connection failed", message)
            return
        self.accept()
