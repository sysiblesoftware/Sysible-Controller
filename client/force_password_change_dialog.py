import requests
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
)
from PySide6.QtCore import Qt

from client import api
from client import theme
from client.theme import STATUS_ERROR_COLOR


class ForcePasswordChangeDialog(QDialog):
    """Shown immediately after AdminLoginDialog accepts, when the
    account that just logged in has must_change_password set - the
    freshly-seeded default admin/admin account, or one a fellow
    admin just (re)created with a temporary password. Has no Cancel
    path: client/main.py keeps this on screen until a new password
    is actually set, since letting someone into the dashboard while
    still on a known/temporary password defeats the point.

    current_password is supplied by the caller (the password the
    admin just typed into AdminLoginDialog) so it isn't typed twice."""

    def __init__(self, username: str, current_password: str):
        super().__init__()

        self._username = username
        self._current_password = current_password

        self.setWindowTitle("Change Your Password")
        self.setFixedWidth(380)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setWindowFlag(Qt.WindowCloseButtonHint, False)

        layout = QVBoxLayout()
        self.setLayout(layout)

        title = QLabel("Change Your Password")
        title.setStyleSheet("font-size:16px; font-weight:bold;")
        layout.addWidget(title)

        hint = QLabel(
            f"The account \"{username}\" must set a new password before continuing."
        )
        theme.style_hint_label(hint)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        new_row = QHBoxLayout()
        new_row.addWidget(QLabel("New Password:"))
        self.new_password_input = QLineEdit()
        self.new_password_input.setEchoMode(QLineEdit.Password)
        new_row.addWidget(self.new_password_input)
        layout.addLayout(new_row)

        confirm_row = QHBoxLayout()
        confirm_row.addWidget(QLabel("Confirm Password:"))
        self.confirm_password_input = QLineEdit()
        self.confirm_password_input.setEchoMode(QLineEdit.Password)
        self.confirm_password_input.returnPressed.connect(self._attempt_change)
        confirm_row.addWidget(self.confirm_password_input)
        layout.addLayout(confirm_row)

        self.error_label = QLabel("")
        self.error_label.setStyleSheet(f"color:{STATUS_ERROR_COLOR};")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)

        buttons_row = QHBoxLayout()
        buttons_row.addStretch()

        self.change_btn = QPushButton("Set Password")
        self.change_btn.setDefault(True)
        self.change_btn.clicked.connect(self._attempt_change)
        buttons_row.addWidget(self.change_btn)

        layout.addLayout(buttons_row)

        self.new_password_input.setFocus()

    def _attempt_change(self):
        new_password = self.new_password_input.text()
        confirm_password = self.confirm_password_input.text()

        if not new_password:
            self.error_label.setText("New password cannot be empty.")
            return

        if new_password != confirm_password:
            self.error_label.setText("Passwords do not match.")
            self.confirm_password_input.clear()
            self.confirm_password_input.setFocus()
            return

        self.change_btn.setEnabled(False)

        try:
            api.force_admin_password_change(self._username, self._current_password, new_password)
        except requests.exceptions.HTTPError as e:
            self.error_label.setText(str(e))
            return
        except Exception as e:
            self.error_label.setText(f"Could not reach Sysible Controller backend: {e}")
            return
        finally:
            self.change_btn.setEnabled(True)

        self.accept()
