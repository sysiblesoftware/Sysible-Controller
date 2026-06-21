import datetime

from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QMessageBox,
    QFrame,
    QListWidget,
    QFileDialog,
    QTableWidget,
    QTableWidgetItem,
    QAbstractItemView,
    QScrollArea,
    QTextEdit,
)

from client import api
from client import theme
from client.theme import STATUS_NEUTRAL_COLOR, STATUS_SUCCESS_COLOR, STATUS_ERROR_COLOR, STATUS_WARNING_COLOR
from client.branding import make_page_header


def _format_timestamp(value):
    if not value:
        return "Never"
    try:
        return datetime.datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return str(value)


def _human_size(num_bytes):
    size = float(num_bytes)

    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024


class WebserverPortalPage(QWidget):
    """Start/stop the Webserver Portal (a separate process - see
    backend/portal_manager.py), set which port it listens on, and
    manage the username/password a remote host operator logs in with
    to download an agent bundle."""

    def __init__(self):
        super().__init__()

        self.setWindowTitle("Webserver Portal Configuration")
        self.resize(820, 760)

        # Whether the Reset Login Credentials form below needs to
        # require + verify a current password - true once any
        # credentials exist, false only on a fresh install that's
        # never had any (see refresh()).
        self._credentials_configured = False

        outer = QVBoxLayout()
        self.setLayout(outer)

        outer.addLayout(make_page_header("Webserver Portal Configuration", font_size=22, logo_height=32))

        # Everything below the title lives inside a scroll area instead
        # of directly in the page's layout. With 8 stacked sections (two
        # of them tables) and no explicit size here before, the window's
        # natural minimum height came out taller than most screens - the
        # maximize button had no room left to expand into (looked dead)
        # and dragging the border down hit that same oversized minimum
        # almost immediately (looked unresizable). Wrapping the content
        # in a QScrollArea, like Sysible Controller Settings and the other long
        # stacked pages already do, decouples the window's size from the
        # content's total height.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        outer.addWidget(scroll)

        content = QWidget()
        layout = QVBoxLayout(content)
        scroll.setWidget(content)

        warning = QLabel(
            "The portal serves HTTPS using the same self-signed cert as\n"
            "the Sysible Controller. A host operator's browser will show\n"
            "an untrusted-certificate warning the first time - that's\n"
            "expected (they have nothing to verify it against yet) and\n"
            "safe to click through. Only start it while actively\n"
            "provisioning hosts, on a network you trust."
        )
        warning.setStyleSheet(f"color:{STATUS_WARNING_COLOR};")
        layout.addWidget(warning)

        # =====================================================
        # STATUS
        # =====================================================
        self.status_label = QLabel("Status: unknown")
        self.status_label.setStyleSheet("font-size:16px;font-weight:bold;")
        layout.addWidget(self.status_label)

        self.url_label = QLabel("")
        layout.addWidget(self.url_label)

        # Hidden unless Controller Configuration genuinely hasn't been
        # saved yet (see refresh()) - the portal will refuse to start
        # at all in that state (backend/portal_manager.py), but this
        # surfaces the same problem before the admin even clicks Start.
        self.config_warning_label = QLabel("")
        self.config_warning_label.setStyleSheet(f"color:{STATUS_ERROR_COLOR};font-weight:bold;")
        self.config_warning_label.setVisible(False)
        layout.addWidget(self.config_warning_label)

        status_buttons = QHBoxLayout()

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)

        self.start_btn = QPushButton("Start Portal")
        self.start_btn.clicked.connect(self.start_portal)

        self.stop_btn = QPushButton("Stop Portal")
        self.stop_btn.clicked.connect(self.stop_portal)

        status_buttons.addWidget(self.refresh_btn)
        status_buttons.addWidget(self.start_btn)
        status_buttons.addWidget(self.stop_btn)

        layout.addLayout(status_buttons)

        layout.addWidget(self._divider())

        # =====================================================
        # COMMAND-LINE BUNDLE DOWNLOAD (curl)
        # =====================================================
        curl_label = QLabel("Command-Line Bundle Download (curl)")
        curl_label.setStyleSheet("font-size:18px;font-weight:bold;")
        layout.addWidget(curl_label)

        curl_hint = QLabel(
            "For terminal-only or headless systems that can't open a browser: "
            "logs into the portal and downloads the agent bundle in one shot. "
            "-k skips certificate verification (same self-signed cert warning "
            "as above) and the cookie jar carries the session from login to "
            "download. Swap in the real password before running."
        )
        theme.style_hint_label(curl_hint)
        curl_hint.setWordWrap(True)
        layout.addWidget(curl_hint)

        self.curl_text = QTextEdit()
        self.curl_text.setReadOnly(True)
        self.curl_text.setStyleSheet("font-family: monospace;")
        self.curl_text.setFixedHeight(90)
        layout.addWidget(self.curl_text)

        copy_curl_btn = QPushButton("Copy to Clipboard")
        copy_curl_btn.clicked.connect(self.copy_curl_command)
        layout.addWidget(copy_curl_btn)

        layout.addWidget(self._divider())

        # =====================================================
        # PORTAL PORT
        # =====================================================
        port_label = QLabel("Portal Port")
        port_label.setStyleSheet("font-size:18px;font-weight:bold;")
        layout.addWidget(port_label)

        self.port_hint = QLabel(
            "Which port the portal listens on. Takes effect the next time "
            "it's started - restart it after saving if it's already running."
        )
        theme.style_hint_label(self.port_hint)
        layout.addWidget(self.port_hint)

        port_row = QHBoxLayout()
        port_row.addWidget(QLabel("Port:"))
        self.port_input = QLineEdit()
        self.port_input.setMaximumWidth(100)
        port_row.addWidget(self.port_input)
        self.save_port_btn = QPushButton("Save Port")
        self.save_port_btn.clicked.connect(self.save_port)
        port_row.addWidget(self.save_port_btn)
        layout.addLayout(port_row)

        layout.addWidget(self._divider())

        # =====================================================
        # CURRENT CREDENTIALS (read-only)
        # =====================================================
        current_creds_label = QLabel("Current Credentials")
        current_creds_label.setStyleSheet("font-size:18px;font-weight:bold;")
        layout.addWidget(current_creds_label)

        self.current_user_label = QLabel("")
        layout.addWidget(self.current_user_label)

        self.last_login_label = QLabel("")
        theme.style_hint_label(self.last_login_label)
        layout.addWidget(self.last_login_label)

        self.last_changed_label = QLabel("")
        theme.style_hint_label(self.last_changed_label)
        layout.addWidget(self.last_changed_label)

        layout.addWidget(self._divider())

        # =====================================================
        # RESET LOGIN CREDENTIALS
        # =====================================================
        reset_creds_label = QLabel("Reset Login Credentials")
        reset_creds_label.setStyleSheet("font-size:18px;font-weight:bold;")
        layout.addWidget(reset_creds_label)

        self.reset_creds_hint = QLabel(
            "Enter the current password to confirm this change."
        )
        theme.style_hint_label(self.reset_creds_hint)
        layout.addWidget(self.reset_creds_hint)

        current_pass_row = QHBoxLayout()
        current_pass_row.addWidget(QLabel("Current Password:"))
        self.current_password_input = QLineEdit()
        self.current_password_input.setEchoMode(QLineEdit.Password)
        self.current_password_input.setMaximumWidth(260)
        current_pass_row.addWidget(self.current_password_input)
        layout.addLayout(current_pass_row)

        user_row = QHBoxLayout()
        user_row.addWidget(QLabel("New Username:"))
        self.username_input = QLineEdit()
        self.username_input.setMaximumWidth(260)
        user_row.addWidget(self.username_input)
        layout.addLayout(user_row)

        pass_row = QHBoxLayout()
        pass_row.addWidget(QLabel("New Password:"))
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.Password)
        self.password_input.setMaximumWidth(260)
        pass_row.addWidget(self.password_input)
        layout.addLayout(pass_row)

        confirm_pass_row = QHBoxLayout()
        confirm_pass_row.addWidget(QLabel("Confirm New Password:"))
        self.confirm_password_input = QLineEdit()
        self.confirm_password_input.setEchoMode(QLineEdit.Password)
        self.confirm_password_input.setMaximumWidth(260)
        confirm_pass_row.addWidget(self.confirm_password_input)
        layout.addLayout(confirm_pass_row)

        self.set_creds_btn = QPushButton("Save Credentials")
        self.set_creds_btn.clicked.connect(self.set_credentials)
        layout.addWidget(self.set_creds_btn)

        remove_creds_hint = QLabel(
            "Removing login access wipes the account outright - nobody can log "
            "into the portal until new credentials are saved above. Uses the "
            "same current password field as a reset."
        )
        theme.style_hint_label(remove_creds_hint)
        remove_creds_hint.setWordWrap(True)
        layout.addWidget(remove_creds_hint)

        self.remove_creds_btn = QPushButton("Remove Login Access")
        self.remove_creds_btn.setStyleSheet(f"color:{STATUS_ERROR_COLOR};")
        self.remove_creds_btn.clicked.connect(self.remove_credentials)
        layout.addWidget(self.remove_creds_btn)

        layout.addWidget(self._divider())

        # =====================================================
        # LOGIN HISTORY
        # =====================================================
        history_label = QLabel("Login History")
        history_label.setStyleSheet("font-size:18px;font-weight:bold;")
        layout.addWidget(history_label)

        history_hint = QLabel(
            "Every login attempt against the portal account, successful or not, "
            "plus credential-reset events."
        )
        theme.style_hint_label(history_hint)
        history_hint.setWordWrap(True)
        layout.addWidget(history_hint)

        self.history_table = QTableWidget(0, 4)
        self.history_table.setHorizontalHeaderLabels(["Time", "Event", "Username", "IP Address"])
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.history_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.history_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.history_table.horizontalHeader().setStretchLastSection(True)
        self.history_table.setFixedHeight(180)
        layout.addWidget(self.history_table)

        history_refresh_btn = QPushButton("Refresh Login History")
        history_refresh_btn.clicked.connect(self.refresh_login_history)
        layout.addWidget(history_refresh_btn)

        layout.addWidget(self._divider())

        # =====================================================
        # ACTIVE SESSIONS
        # =====================================================
        sessions_label = QLabel("Active Sessions")
        sessions_label.setStyleSheet("font-size:18px;font-weight:bold;")
        layout.addWidget(sessions_label)

        sessions_hint = QLabel(
            "Every host operator currently logged into the portal. Revoking one "
            "logs that browser out immediately - it'll have to log in again."
        )
        theme.style_hint_label(sessions_hint)
        sessions_hint.setWordWrap(True)
        layout.addWidget(sessions_hint)

        self.sessions_table = QTableWidget(0, 3)
        self.sessions_table.setHorizontalHeaderLabels(["Logged In", "Expires", "IP Address"])
        self.sessions_table.verticalHeader().setVisible(False)
        self.sessions_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.sessions_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.sessions_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.sessions_table.horizontalHeader().setStretchLastSection(True)
        self.sessions_table.setFixedHeight(140)
        layout.addWidget(self.sessions_table)

        self._sessions_data = []

        sessions_buttons = QHBoxLayout()

        sessions_refresh_btn = QPushButton("Refresh Sessions")
        sessions_refresh_btn.clicked.connect(self.refresh_sessions)

        self.revoke_session_btn = QPushButton("Revoke Selected")
        self.revoke_session_btn.clicked.connect(self.revoke_selected_session)

        sessions_buttons.addWidget(sessions_refresh_btn)
        sessions_buttons.addWidget(self.revoke_session_btn)

        layout.addLayout(sessions_buttons)

        layout.addWidget(self._divider())

        # =====================================================
        # FILES UPLOADED BY HOSTS
        # =====================================================
        uploads_label = QLabel("Files Uploaded By Hosts")
        uploads_label.setStyleSheet("font-size:18px;font-weight:bold;")
        layout.addWidget(uploads_label)

        uploads_hint = QLabel(
            "Sent in by a host operator through the portal's upload form.\n"
            "One shared pool, not split out per host."
        )
        theme.style_hint_label(uploads_hint)
        layout.addWidget(uploads_hint)

        self.uploads_list = QListWidget()
        self._uploads_data = []
        layout.addWidget(self.uploads_list)

        uploads_buttons = QHBoxLayout()

        self.refresh_uploads_btn = QPushButton("Refresh List")
        self.refresh_uploads_btn.clicked.connect(self.refresh_uploads)

        self.save_upload_btn = QPushButton("Save To Computer...")
        self.save_upload_btn.clicked.connect(self.save_selected_upload)

        self.delete_upload_btn = QPushButton("Delete")
        self.delete_upload_btn.clicked.connect(self.delete_selected_upload)

        uploads_buttons.addWidget(self.refresh_uploads_btn)
        uploads_buttons.addWidget(self.save_upload_btn)
        uploads_buttons.addWidget(self.delete_upload_btn)

        layout.addLayout(uploads_buttons)

        layout.addWidget(self._divider())

        # =====================================================
        # FILES STAGED FOR DOWNLOAD
        # =====================================================
        downloads_label = QLabel("Files Staged For Download")
        downloads_label.setStyleSheet("font-size:18px;font-weight:bold;")
        layout.addWidget(downloads_label)

        downloads_hint = QLabel(
            "Shown to a host operator next time they log into the portal,\n"
            "with a Download link for each."
        )
        theme.style_hint_label(downloads_hint)
        layout.addWidget(downloads_hint)

        self.downloads_list = QListWidget()
        self._downloads_data = []
        layout.addWidget(self.downloads_list)

        downloads_buttons = QHBoxLayout()

        self.refresh_downloads_btn = QPushButton("Refresh List")
        self.refresh_downloads_btn.clicked.connect(self.refresh_downloads)

        self.add_download_btn = QPushButton("Add File...")
        self.add_download_btn.clicked.connect(self.add_download_file)

        self.delete_download_btn = QPushButton("Delete")
        self.delete_download_btn.clicked.connect(self.delete_selected_download)

        downloads_buttons.addWidget(self.refresh_downloads_btn)
        downloads_buttons.addWidget(self.add_download_btn)
        downloads_buttons.addWidget(self.delete_download_btn)

        layout.addLayout(downloads_buttons)

        layout.addStretch()

        self.refresh()
        self.refresh_login_history()
        self.refresh_sessions()
        self.refresh_uploads()
        self.refresh_downloads()

    @staticmethod
    def _divider():
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        return line

    # =====================================================
    # STATUS
    # =====================================================
    def refresh(self):
        try:
            status = api.get_portal_status()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        running = status.get("running")
        port = status.get("port")
        configured_port = status.get("configured_port")
        error = status.get("error")

        if running:
            self.status_label.setText(f"Status: Running (port {port})")
        else:
            self.status_label.setText("Status: Stopped")

        self.status_label.setStyleSheet(
            f"font-size:16px;font-weight:bold;color:{STATUS_SUCCESS_COLOR if running else STATUS_NEUTRAL_COLOR};"
        )

        try:
            config = api.get_controller_config()
        except Exception:
            config = {}

        if running:
            host = config.get("address") or "<this machine's address>"
            self.url_label.setText(f"Reachable at: https://{host}:{port}")
        else:
            self.url_label.setText("")

        if not config.get("configured"):
            self.config_warning_label.setText(
                "Controller Configuration hasn't been set yet - the portal "
                "can't start (and agent bundles can't be built) until "
                "you set a reachable Hostname or IP Address there."
            )
            self.config_warning_label.setVisible(True)
        else:
            self.config_warning_label.setVisible(False)

        curl_host = config.get("address") or "<this machine's address>"
        curl_port = configured_port or port or 443
        curl_user = status.get("username") if status.get("credentials_configured") else "<username>"
        self.curl_text.setPlainText(
            f'curl -k -c /tmp/sysible_portal_cookies.txt '
            f'-d "username={curl_user}" --data-urlencode "password=<password>" '
            f'"https://{curl_host}:{curl_port}/login" '
            f'&& curl -k -b /tmp/sysible_portal_cookies.txt -OJ '
            f'"https://{curl_host}:{curl_port}/files/bundle"'
        )

        if error:
            QMessageBox.critical(self, "Portal failed to start", error)

        # Don't clobber a port the user is actively typing into.
        if not self.port_input.hasFocus():
            self.port_input.setText(str(configured_port or ""))

        configured = status.get("credentials_configured")
        username = status.get("username")
        self._credentials_configured = bool(configured)

        if configured:
            self.current_user_label.setText(f"Username: {username}")
        else:
            self.current_user_label.setText("No login credentials configured yet.")

        last_login = status.get("last_login")
        if last_login:
            self.last_login_label.setText(
                f"Last successful login: {_format_timestamp(last_login['timestamp'])} "
                f"from {last_login.get('ip') or 'unknown IP'}"
            )
        else:
            self.last_login_label.setText("Last successful login: never")

        last_changed = status.get("last_changed")
        self.last_changed_label.setText(f"Credentials last changed: {_format_timestamp(last_changed)}")

        self.reset_creds_hint.setText(
            "Enter the current password to confirm this change."
            if self._credentials_configured
            else "First-time setup - no current password needed yet."
        )

    def start_portal(self):
        try:
            result = api.start_portal()
            if not result.get("running") and result.get("error"):
                QMessageBox.critical(self, "Portal failed to start", result["error"])
            self.refresh()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def stop_portal(self):
        try:
            api.stop_portal()
            self.refresh()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def copy_curl_command(self):
        QApplication.clipboard().setText(self.curl_text.toPlainText())

    # =====================================================
    # PORTAL PORT
    # =====================================================
    def save_port(self):
        try:
            port = int(self.port_input.text().strip())
        except ValueError:
            QMessageBox.warning(self, "Invalid port", "Port must be a number.")
            return

        if not (1 <= port <= 65535):
            QMessageBox.warning(self, "Invalid port", "Port must be between 1 and 65535.")
            return

        try:
            api.set_portal_port(port)
            self.refresh()
            QMessageBox.information(self, "Saved", f"Portal port set to {port}.")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    # =====================================================
    # CREDENTIALS
    # =====================================================
    def set_credentials(self):
        username = self.username_input.text().strip()
        password = self.password_input.text()
        confirm_password = self.confirm_password_input.text()
        current_password = self.current_password_input.text()

        if not username or not password:
            QMessageBox.warning(self, "Missing fields", "Username and password are both required.")
            return

        if password != confirm_password:
            QMessageBox.warning(self, "Passwords don't match", "New Password and Confirm New Password must match.")
            return

        if self._credentials_configured and not current_password:
            QMessageBox.warning(self, "Missing fields", "Enter the current password to confirm this change.")
            return

        try:
            api.set_portal_credentials(username, password, current_password)
            self.current_password_input.clear()
            self.password_input.clear()
            self.confirm_password_input.clear()
            self.refresh()
            self.refresh_login_history()
            self.refresh_sessions()
            QMessageBox.information(
                self, "Saved",
                "Portal login credentials updated. Any existing sessions have been logged out."
            )
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def remove_credentials(self):
        if not self._credentials_configured:
            QMessageBox.information(self, "Nothing to remove", "No login credentials are configured.")
            return

        current_password = self.current_password_input.text()

        if not current_password:
            QMessageBox.warning(self, "Missing fields", "Enter the current password to confirm this change.")
            return

        reply = QMessageBox.question(
            self,
            "Remove login access?",
            "This wipes the portal login outright - nobody will be able to log "
            "in (and any active sessions are ended immediately) until new "
            "credentials are saved. Continue?",
        )

        if reply != QMessageBox.Yes:
            return

        try:
            api.remove_portal_credentials(current_password)
            self.current_password_input.clear()
            self.refresh()
            self.refresh_login_history()
            self.refresh_sessions()
            QMessageBox.information(self, "Removed", "Portal login access has been removed.")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    # =====================================================
    # LOGIN HISTORY
    # =====================================================
    def refresh_login_history(self):
        try:
            entries = api.get_portal_login_history()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        self.history_table.setRowCount(0)
        for row, entry in enumerate(entries):
            self.history_table.insertRow(row)
            self.history_table.setItem(row, 0, QTableWidgetItem(_format_timestamp(entry.get("timestamp"))))
            self.history_table.setItem(row, 1, QTableWidgetItem(entry.get("event", "")))
            self.history_table.setItem(row, 2, QTableWidgetItem(entry.get("username", "")))
            self.history_table.setItem(row, 3, QTableWidgetItem(entry.get("ip") or ""))

    # =====================================================
    # ACTIVE SESSIONS
    # =====================================================
    def refresh_sessions(self):
        try:
            sessions = api.get_portal_sessions()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        self._sessions_data = sessions
        self.sessions_table.setRowCount(0)
        for row, s in enumerate(sessions):
            self.sessions_table.insertRow(row)
            self.sessions_table.setItem(row, 0, QTableWidgetItem(_format_timestamp(s.get("created"))))
            self.sessions_table.setItem(row, 1, QTableWidgetItem(_format_timestamp(s.get("expires"))))
            self.sessions_table.setItem(row, 2, QTableWidgetItem(s.get("ip") or ""))

    def revoke_selected_session(self):
        row = self.sessions_table.currentRow()

        if row < 0 or row >= len(self._sessions_data):
            QMessageBox.warning(self, "No selection", "Select a session first.")
            return

        session_id = self._sessions_data[row]["id"]

        try:
            api.revoke_portal_session(session_id)
            self.refresh_sessions()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    # =====================================================
    # FILES UPLOADED BY HOSTS
    # =====================================================
    @staticmethod
    def _selected_filename(list_widget, data):
        row = list_widget.currentRow()

        if row < 0 or row >= len(data):
            return None

        return data[row]["filename"]

    def refresh_uploads(self):
        try:
            files = api.list_portal_uploads()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        self._uploads_data = files
        self.uploads_list.clear()

        for f in files:
            self.uploads_list.addItem(f"{f['filename']}  ({_human_size(f['size'])})")

    def save_selected_upload(self):
        filename = self._selected_filename(self.uploads_list, self._uploads_data)

        if not filename:
            QMessageBox.warning(self, "No selection", "Select a file first.")
            return

        save_path, _ = QFileDialog.getSaveFileName(self, "Save File", filename)

        if not save_path:
            return

        try:
            api.download_portal_upload(filename, save_path)
            QMessageBox.information(self, "Saved", f"Saved to {save_path}")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def delete_selected_upload(self):
        filename = self._selected_filename(self.uploads_list, self._uploads_data)

        if not filename:
            QMessageBox.warning(self, "No selection", "Select a file first.")
            return

        reply = QMessageBox.question(self, "Confirm", f"Delete '{filename}'?")

        if reply != QMessageBox.Yes:
            return

        try:
            api.delete_portal_upload(filename)
            self.refresh_uploads()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    # =====================================================
    # FILES STAGED FOR DOWNLOAD
    # =====================================================
    def refresh_downloads(self):
        try:
            files = api.list_portal_downloads()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        self._downloads_data = files
        self.downloads_list.clear()

        for f in files:
            self.downloads_list.addItem(f"{f['filename']}  ({_human_size(f['size'])})")

    def add_download_file(self):
        local_path, _ = QFileDialog.getOpenFileName(self, "Select File To Stage")

        if not local_path:
            return

        try:
            api.stage_portal_download(local_path)
            self.refresh_downloads()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def delete_selected_download(self):
        filename = self._selected_filename(self.downloads_list, self._downloads_data)

        if not filename:
            QMessageBox.warning(self, "No selection", "Select a file first.")
            return

        reply = QMessageBox.question(self, "Confirm", f"Delete '{filename}'?")

        if reply != QMessageBox.Yes:
            return

        try:
            api.delete_portal_download(filename)
            self.refresh_downloads()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
