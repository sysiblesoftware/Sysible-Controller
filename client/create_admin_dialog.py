import requests
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
)
from PySide6.QtGui import QPixmap
from PySide6.QtCore import Qt

from client import api, session
from client import theme
from client.theme import STATUS_ERROR_COLOR
from client.branding import LOGO_PATH, center_on_screen


class CreateAdminDialog(QDialog):
    """First-run setup, shown by client/main.py when the controller reports
    no administrator exists yet (GET /admin/setup-required). There is no
    default account: the operator creates their own administrator with
    their own password here, and is logged straight in on success - no
    "log in with admin/admin, then change it" detour.

    Mirrors AdminLoginDialog's look. On accept(), self.username is the
    account that was created (also stashed in client/session.py), so
    main.py can go straight to the dashboard."""

    def __init__(self):
        super().__init__()

        self.setWindowTitle("Sysible Controller — Create Administrator")
        self.setFixedWidth(460)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

        layout = QVBoxLayout()
        layout.setContentsMargins(32, 28, 32, 24)
        layout.setSpacing(10)
        self.setLayout(layout)

        logo_pixmap = QPixmap(str(LOGO_PATH))
        if not logo_pixmap.isNull():
            logo_label = QLabel()
            logo_label.setAlignment(Qt.AlignCenter)
            logo_label.setPixmap(logo_pixmap.scaledToHeight(96, Qt.SmoothTransformation))
            layout.addWidget(logo_label)
            layout.addSpacing(18)

        title = QLabel("Create Administrator Account")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size:16px; font-weight:bold;")
        layout.addWidget(title)

        hint = QLabel(
            "Welcome to Sysible Controller. Set up your administrator "
            "account to get started - choose a username and password you'll "
            "use to log in from now on."
        )
        hint.setAlignment(Qt.AlignCenter)
        theme.style_hint_label(hint)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addSpacing(20)

        user_row = QHBoxLayout()
        user_row.addWidget(QLabel("Username:"))
        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("e.g. admin")
        user_row.addWidget(self.username_input)
        layout.addLayout(user_row)

        pass_row = QHBoxLayout()
        pass_row.addWidget(QLabel("Password:"))
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.Password)
        pass_row.addWidget(self.password_input)
        layout.addLayout(pass_row)

        confirm_row = QHBoxLayout()
        confirm_row.addWidget(QLabel("Confirm:"))
        self.confirm_input = QLineEdit()
        self.confirm_input.setEchoMode(QLineEdit.Password)
        self.confirm_input.returnPressed.connect(self._attempt_setup)
        confirm_row.addWidget(self.confirm_input)
        layout.addLayout(confirm_row)

        self.error_label = QLabel("")
        self.error_label.setStyleSheet(f"color:{STATUS_ERROR_COLOR};")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)

        buttons_row = QHBoxLayout()
        buttons_row.addStretch()

        quit_btn = QPushButton("Quit")
        quit_btn.clicked.connect(self.reject)
        buttons_row.addWidget(quit_btn)

        self.create_btn = QPushButton("Create Account")
        self.create_btn.setDefault(True)
        self.create_btn.clicked.connect(self._attempt_setup)
        buttons_row.addWidget(self.create_btn)

        layout.addLayout(buttons_row)

        self.username_input.setFocus()

        # Populated on success - see _attempt_setup. password is kept in
        # memory only (never logged), in case main.py wants it.
        self.username = None
        self.password = None
        self._centered = False

    def showEvent(self, event):
        super().showEvent(event)
        if not self._centered:
            self._centered = True
            center_on_screen(self)

    def _attempt_setup(self):
        username = self.username_input.text().strip()
        password = self.password_input.text()
        confirm = self.confirm_input.text()

        if not username or not password:
            self.error_label.setText("Username and password are both required.")
            return

        if password != confirm:
            self.error_label.setText("The two passwords don't match.")
            self.confirm_input.clear()
            self.confirm_input.setFocus()
            return

        self.create_btn.setEnabled(False)

        try:
            result = api.admin_setup(username, password)
        except requests.exceptions.HTTPError as e:
            # The backend enforces the password policy and rejects setup if
            # an account somehow already exists - surface its message.
            detail = "Could not create the account."
            try:
                detail = e.response.json().get("detail", detail)
            except Exception:
                pass
            self.error_label.setText(detail)
            return
        except Exception as e:
            self.error_label.setText(f"Could not reach Sysible Controller backend: {e}")
            return
        finally:
            self.create_btn.setEnabled(True)

        self.username = result.get("username", username)
        self.password = password
        session.set_current_admin(self.username)

        self.accept()
