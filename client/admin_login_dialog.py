import requests
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
)
from PySide6.QtGui import QPixmap
from PySide6.QtCore import Qt

from client import api, session
from client import theme
from client.theme import STATUS_ERROR_COLOR
from client.branding import LOGO_PATH


class AdminLoginDialog(QDialog):
    """Gate shown once at GUI startup (see client/main.py) before the
    dashboard appears - same idea as logging into any other admin
    console before it'll show you anything. Backed by the
    administrators table (backend/db.py). There is no default account:
    a fresh install has no administrators, so the first launch runs the
    create-administrator setup flow (CreateAdminDialog) instead of this
    login. Add more accounts, or change a password, from the Sysible
    Administrator Configuration page once you're in.

    This is a one-shot check, not a session/token system like the
    Webserver Portal's - the GUI is a single long-lived desktop
    process for one person at the keyboard, not a multi-request web
    server with many concurrent users, so there's nothing a session
    token would buy here that a successful return from this dialog
    doesn't already cover. The logged-in username is stashed in
    client/session.py for the rest of the process's lifetime (audit
    log attribution, forced password-change flow), and is also
    exposed here as self.username / self.must_change_password so
    client/main.py can act on it right after accept()."""

    def __init__(self):
        super().__init__()

        self.setWindowTitle("Sysible Controller Administrator Login")
        # 360 was too narrow: most window managers center the title in the
        # title bar, and "Sysible Controller Administrator Login" rendered
        # wider than that at normal title-bar font sizes, so a couple of
        # characters off each end got clipped instead of the title
        # shrinking or wrapping. Widened rather than shortening the title,
        # since the full name should actually be readable.
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
            logo_label.setPixmap(
                logo_pixmap.scaledToHeight(96, Qt.SmoothTransformation)
            )
            layout.addWidget(logo_label)
            layout.addSpacing(18)

        title = QLabel("Sysible Controller Administrator Login")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size:16px; font-weight:bold;")
        layout.addWidget(title)

        hint = QLabel("Enter your Sysible Controller administrator username and password.")
        hint.setAlignment(Qt.AlignCenter)
        theme.style_hint_label(hint)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addSpacing(20)

        user_row = QHBoxLayout()
        user_row.addWidget(QLabel("Username:"))
        self.username_input = QLineEdit()
        user_row.addWidget(self.username_input)
        layout.addLayout(user_row)

        pass_row = QHBoxLayout()
        pass_row.addWidget(QLabel("Password:"))
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.Password)
        self.password_input.returnPressed.connect(self._attempt_login)
        pass_row.addWidget(self.password_input)
        layout.addLayout(pass_row)

        self.error_label = QLabel("")
        self.error_label.setStyleSheet(f"color:{STATUS_ERROR_COLOR};")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)

        buttons_row = QHBoxLayout()
        buttons_row.addStretch()

        quit_btn = QPushButton("Quit")
        quit_btn.clicked.connect(self.reject)
        buttons_row.addWidget(quit_btn)

        self.login_btn = QPushButton("Log In")
        self.login_btn.setDefault(True)
        self.login_btn.clicked.connect(self._attempt_login)
        buttons_row.addWidget(self.login_btn)

        layout.addLayout(buttons_row)

        self.password_input.setFocus()

        # Populated on a successful login - see _attempt_login below.
        # self.password is kept (in memory only, never logged) purely
        # so client/main.py can hand it straight to
        # ForcePasswordChangeDialog as the "current password" without
        # making the admin type it a second time.
        self.username = None
        self.password = None
        self.must_change_password = False

    def _attempt_login(self):
        username = self.username_input.text().strip()
        password = self.password_input.text()

        if not username or not password:
            self.error_label.setText("Username and password are both required.")
            return

        self.login_btn.setEnabled(False)

        try:
            result = api.admin_login(username, password)
        except requests.exceptions.HTTPError:
            self.error_label.setText("Invalid username or password.")
            self.password_input.clear()
            self.password_input.setFocus()
            return
        except Exception as e:
            self.error_label.setText(f"Could not reach Sysible Controller backend: {e}")
            return
        finally:
            self.login_btn.setEnabled(True)

        self.username = result.get("username", username)
        self.password = password
        self.must_change_password = bool(result.get("must_change_password"))
        session.set_current_admin(self.username)

        self.accept()
