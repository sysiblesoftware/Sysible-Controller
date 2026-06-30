"""
A self-service place for ANY administrator (superuser or sysadmin) to set,
update, or clear their own sudo ("become") password - the password Sysible
feeds to `sudo -S` on hosts whose sudo isn't passwordless, so dispatched
actions and the terminal's "Send sudo password" button can elevate.

The password is stored encrypted at rest on this workstation
(become_credentials, Fernet + 0600 key) and is namespaced to the logged-in
admin, so one admin's password is never used as another's. Nothing is ever
sent to or stored on the controller from here.

Two scopes:
  * Fleet default ("*") - used for every host that has no specific entry.
  * Per host - overrides the fleet default for one host.
"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QComboBox, QMessageBox,
)

from client import become_credentials, session
from client.branding import center_on_screen, make_page_header
from client import theme
from client.theme import STATUS_NEUTRAL_COLOR, STATUS_SUCCESS_COLOR, STATUS_ERROR_COLOR

_FLEET_DEFAULT = "*"
_FLEET_LABEL = "All hosts (fleet default)"


class SudoPasswordDialog(QDialog):
    def __init__(self, parent=None, host_labels=None):
        super().__init__(parent)
        self.setWindowTitle("My Sudo Password")
        self.setMinimumWidth(460)
        self._centered = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(10)

        layout.addWidget(make_page_header("My Sudo Password", font_size=16, logo_height=24))

        who = session.get_current_admin() or "this admin"
        intro = QLabel(
            f"Stored for <b>{who}</b> and used to elevate commands on hosts whose "
            "sudo requires a password (and by the terminal's “Send sudo password” "
            "button). It’s encrypted on this computer and never sent to the controller."
        )
        intro.setWordWrap(True)
        theme.style_hint_label(intro)
        layout.addWidget(intro)

        # ---- scope ----
        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("Applies to:"))
        self.scope_combo = QComboBox()
        self.scope_combo.addItem(_FLEET_LABEL, _FLEET_DEFAULT)
        for label in (host_labels or []):
            self.scope_combo.addItem(label, label)
        self.scope_combo.currentIndexChanged.connect(self._refresh_status)
        scope_row.addWidget(self.scope_combo, 1)
        layout.addLayout(scope_row)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        # ---- password fields ----
        pass_row = QHBoxLayout()
        pass_row.addWidget(QLabel("New password:"))
        self.pass_input = QLineEdit()
        self.pass_input.setEchoMode(QLineEdit.Password)
        self.pass_input.setPlaceholderText("your sudo password")
        pass_row.addWidget(self.pass_input, 1)
        layout.addLayout(pass_row)

        confirm_row = QHBoxLayout()
        confirm_row.addWidget(QLabel("Confirm:"))
        self.confirm_input = QLineEdit()
        self.confirm_input.setEchoMode(QLineEdit.Password)
        self.confirm_input.returnPressed.connect(self._save)
        confirm_row.addWidget(self.confirm_input, 1)
        layout.addLayout(confirm_row)

        self.show_check = QPushButton("Show")
        self.show_check.setCheckable(True)
        self.show_check.setMaximumWidth(70)
        self.show_check.toggled.connect(self._toggle_echo)
        show_row = QHBoxLayout()
        show_row.addStretch()
        show_row.addWidget(self.show_check)
        layout.addLayout(show_row)

        # ---- buttons ----
        buttons = QHBoxLayout()
        self.clear_btn = QPushButton("Clear stored")
        self.clear_btn.setToolTip("Remove the stored password for the selected scope.")
        self.clear_btn.clicked.connect(self._clear)
        buttons.addWidget(self.clear_btn)
        buttons.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        buttons.addWidget(close_btn)
        self.save_btn = QPushButton("Save")
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self._save)
        buttons.addWidget(self.save_btn)
        layout.addLayout(buttons)

        if not become_credentials.encryption_available():
            self.save_btn.setEnabled(False)
            self.clear_btn.setEnabled(False)
            self.pass_input.setEnabled(False)
            self.confirm_input.setEnabled(False)
            self.status_label.setText(
                "The encryption library isn’t available, so a sudo password can’t be "
                "stored securely on this computer.")
            self.status_label.setStyleSheet(f"color:{STATUS_ERROR_COLOR};")
        else:
            self._refresh_status()

    # ------------------------------------------------------------------
    def showEvent(self, event):
        super().showEvent(event)
        if not self._centered:
            self._centered = True
            center_on_screen(self)

    def _current_scope(self):
        return self.scope_combo.currentData()

    def _refresh_status(self):
        if not become_credentials.encryption_available():
            return
        scope = self._current_scope()
        if become_credentials.is_set(scope):
            self.status_label.setText("✓ A sudo password is currently stored for this scope.")
            self.status_label.setStyleSheet(f"color:{STATUS_SUCCESS_COLOR};")
            self.clear_btn.setEnabled(True)
        else:
            self.status_label.setText("No sudo password is stored for this scope yet.")
            self.status_label.setStyleSheet(f"color:{STATUS_NEUTRAL_COLOR};")
            self.clear_btn.setEnabled(False)
        self.pass_input.clear()
        self.confirm_input.clear()

    def _toggle_echo(self, shown):
        mode = QLineEdit.Normal if shown else QLineEdit.Password
        self.pass_input.setEchoMode(mode)
        self.confirm_input.setEchoMode(mode)
        self.show_check.setText("Hide" if shown else "Show")

    def _save(self):
        pw = self.pass_input.text()
        confirm = self.confirm_input.text()
        if not pw:
            QMessageBox.warning(self, "Missing password", "Enter a password to store.")
            return
        if pw != confirm:
            QMessageBox.warning(self, "Mismatch", "The two passwords don’t match.")
            return
        scope = self._current_scope()
        if become_credentials.set_password(pw, host=scope):
            QMessageBox.information(
                self, "Saved",
                "Your sudo password has been stored (encrypted) on this computer.")
            self._refresh_status()
        else:
            QMessageBox.critical(
                self, "Could not save",
                "The password could not be stored (encryption unavailable).")

    def _clear(self):
        scope = self._current_scope()
        if QMessageBox.question(
                self, "Clear stored password",
                "Remove the stored sudo password for this scope?",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        become_credentials.clear(scope)
        self._refresh_status()
