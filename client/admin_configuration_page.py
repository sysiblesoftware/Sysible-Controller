import datetime

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QMessageBox, QFrame, QComboBox, QTableWidget, QTableWidgetItem,
    QAbstractItemView, QScrollArea, QCheckBox,
)

from client import api, session
from client import theme
from client.branding import make_page_header
from version import VERSION


class AdminConfigurationPage(QWidget):
    """Sysible Controller Settings - single stacked page (Controller settings, then
    Administrators, then Administrator Password Policy, then the Audit
    Log) covering everything to do with running and securing this
    Sysible installation itself. Previously split across two dashboard
    cards (Sysible Administrator Configuration + Sysible Controller
    Configuration) - merged into one page since both are "configure
    Sysible itself" concerns an admin tends to visit together, unlike
    the per-host tools elsewhere on the dashboard. Formerly titled
    "Sysible Administrator Configuration" - renamed since it now covers
    more than just administrator accounts.

    Five sections:
      - Controller Configuration: hostname/IP/port baked into agent
        bundles (formerly client/controller_configuration_page.py).
      - Administrators: who can log into this dashboard. Replaces the
        old single shared admin/admin login with named accounts -
        add/remove others, and change your own username/password.
      - Administrator Password Policy: complexity rules for the
        accounts in the Administrators section above only - separate
        from System Administration > Environmental Policies, which
        governs accounts on managed target hosts instead.
      - Audit Log: logins (success/failure) and administrator account
        changes only - not a general infra-command history, that's
        covered by System Health & Logs / Service Management instead.
      - License & Version: a license key field (no licensing model is
        enforced against it yet - just somewhere to record one) plus
        the installed Sysible Controller version. Folded in here
        rather than kept as its own dashboard tile (formerly
        client/version_licensing_page.py) since it's a single glance,
        not a workflow.
    """

    def __init__(self):
        super().__init__()

        self.setWindowTitle("Sysible Controller Settings")

        outer = QVBoxLayout()
        self.setLayout(outer)

        outer.addLayout(make_page_header("Sysible Controller Settings", font_size=22, logo_height=32))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        outer.addWidget(scroll)

        content = QWidget()
        layout = QVBoxLayout(content)
        scroll.setWidget(content)

        # =====================================================
        # SECTION 1: CONTROLLER CONFIGURATION
        # =====================================================
        self._build_controller_section(layout)

        layout.addWidget(self._divider())

        # =====================================================
        # SECTION 2: ADMINISTRATORS
        # =====================================================
        self._build_administrators_section(layout)

        layout.addWidget(self._divider())

        # =====================================================
        # SECTION 3: ADMINISTRATOR PASSWORD POLICY
        # =====================================================
        self._build_admin_password_policy_section(layout)

        layout.addWidget(self._divider())

        # =====================================================
        # SECTION 4: AUDIT LOG
        # =====================================================
        self._build_audit_log_section(layout)

        layout.addWidget(self._divider())

        # =====================================================
        # SECTION 5: LICENSE & VERSION
        # =====================================================
        self._build_license_version_section(layout)

        layout.addStretch()

        self.refresh()

    @staticmethod
    def _divider():
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        return line

    @staticmethod
    def _section_label(text):
        label = QLabel(text)
        label.setStyleSheet("font-size:18px;font-weight:bold;")
        return label

    @staticmethod
    def _info_row(label_text, value_text):
        row = QHBoxLayout()

        label = QLabel(label_text)
        theme.style_hint_label(label)
        row.addWidget(label)

        value = QLabel(value_text)
        value.setStyleSheet("font-weight:bold;")
        row.addWidget(value)

        row.addStretch()
        return row

    # =========================================================
    # SECTION 1: CONTROLLER CONFIGURATION
    # =========================================================
    def _build_controller_section(self, layout):
        layout.addWidget(self._section_label("Controller Configuration"))

        info = QLabel(
            "This is what generated agent bundles (see Webserver Portal Configuration) "
            "will be configured to talk to. Set a Hostname, an IP Address, or both - then "
            "choose which one bundles should actually use below. \"All Detected IPs\" skips "
            "picking entirely: every address found on this controller is baked into the "
            "bundle, and the agent tries each one until one connects - useful when this "
            "controller has more than one network path (e.g. LAN + VPN) and you're not sure "
            "which managed hosts can reach which."
        )
        theme.style_hint_label(info)
        info.setWordWrap(True)
        layout.addWidget(info)

        host_row = QHBoxLayout()
        host_row.addWidget(QLabel("Hostname:"))
        self.hostname_input = QLineEdit()
        self.hostname_input.setMaximumWidth(280)
        host_row.addWidget(self.hostname_input)
        layout.addLayout(host_row)

        ip_row = QHBoxLayout()
        ip_row.addWidget(QLabel("IP Address:"))
        # Editable combo instead of a plain text field: Detect Local IPs
        # below populates it with every address actually found on this
        # machine, so the admin can pick one instead of having to know
        # it offhand - typing a value not in the list still works too.
        self.ip_input = QComboBox()
        self.ip_input.setEditable(True)
        self.ip_input.setInsertPolicy(QComboBox.NoInsert)
        self.ip_input.setMaximumWidth(220)
        ip_row.addWidget(self.ip_input)
        self.detect_ips_btn = QPushButton("Detect Local IPs")
        self.detect_ips_btn.clicked.connect(self.detect_local_ips)
        ip_row.addWidget(self.detect_ips_btn)
        layout.addLayout(ip_row)

        self.detected_ips_label = QLabel("")
        theme.style_hint_label(self.detected_ips_label)
        self.detected_ips_label.setWordWrap(True)
        layout.addWidget(self.detected_ips_label)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Use for agent bundles:"))
        self.address_mode_combo = QComboBox()
        self.address_mode_combo.addItem("Hostname", "hostname")
        self.address_mode_combo.addItem("IP Address", "ip")
        self.address_mode_combo.addItem("All Detected IPs (failover)", "all")
        self.address_mode_combo.currentIndexChanged.connect(self._update_address_fields_enabled)
        self.address_mode_combo.setMaximumWidth(260)
        mode_row.addWidget(self.address_mode_combo)
        layout.addLayout(mode_row)

        port_hint = QLabel(
            "Must match the port the sysible-backend service actually binds the HTTPS API "
            "to (9000 unless changed). This is NOT the Webserver Portal's port (set on its "
            "own page) - an agent built with the wrong number here will fail to connect."
        )
        theme.style_hint_label(port_hint)
        port_hint.setWordWrap(True)
        layout.addWidget(port_hint)

        port_row = QHBoxLayout()
        port_row.addWidget(QLabel("Port:"))
        self.port_input = QLineEdit()
        self.port_input.setMaximumWidth(100)
        port_row.addWidget(self.port_input)
        layout.addLayout(port_row)

        controller_buttons = QHBoxLayout()

        controller_refresh_btn = QPushButton("Refresh")
        controller_refresh_btn.clicked.connect(self.refresh_controller_config)

        self.controller_save_btn = QPushButton("Save Controller Configuration")
        self.controller_save_btn.clicked.connect(self.save_controller_config)

        controller_buttons.addWidget(controller_refresh_btn)
        controller_buttons.addWidget(self.controller_save_btn)
        layout.addLayout(controller_buttons)

        self.controller_status_label = QLabel("")
        layout.addWidget(self.controller_status_label)

    def refresh_controller_config(self):
        try:
            config = api.get_controller_config()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        self.hostname_input.setText(config.get("hostname") or "")
        self.ip_input.setCurrentText(config.get("ip") or "")

        address_mode = config.get("address_mode") or "hostname"
        index = self.address_mode_combo.findData(address_mode)
        self.address_mode_combo.setCurrentIndex(index if index >= 0 else 0)

        self.port_input.setText(str(config.get("port") or 9000))
        self.controller_status_label.setText("")
        self._update_address_fields_enabled()

        # Pre-populate the IP dropdown / "all mode" preview on load so
        # there's something useful in it before the admin even thinks
        # to click Detect Local IPs - failures here are non-fatal (e.g.
        # psutil hiccup), so stay quiet instead of popping a dialog on
        # every page open.
        self.detect_local_ips(show_errors=False)

    def detect_local_ips(self, show_errors=True):
        try:
            ips = api.get_local_ips()
        except Exception as e:
            if show_errors:
                QMessageBox.critical(self, "Error", str(e))
            return

        current = self.ip_input.currentText().strip()
        self.ip_input.clear()
        self.ip_input.addItems(ips)

        if current:
            self.ip_input.setCurrentText(current)
        elif ips:
            self.ip_input.setCurrentIndex(0)

        self.detected_ips_label.setText(
            "Detected on this controller: " + ", ".join(ips) if ips
            else "No local IP addresses were detected on this controller."
        )

    def _update_address_fields_enabled(self):
        """Purely cosmetic - greys out whichever field "Use for agent
        bundles" isn't currently pointed at, so it's obvious at a
        glance which one (if any) actually matters. "All Detected IPs"
        needs neither: the real list is computed fresh from this
        controller's NICs every time a bundle is built."""
        mode = self.address_mode_combo.currentData()
        self.hostname_input.setEnabled(mode == "hostname")
        self.ip_input.setEnabled(mode == "ip")
        self.detect_ips_btn.setEnabled(mode in ("ip", "all"))

    def save_controller_config(self):
        hostname = self.hostname_input.text().strip()
        ip = self.ip_input.currentText().strip()
        address_mode = self.address_mode_combo.currentData()

        if address_mode != "all" and not hostname and not ip:
            QMessageBox.warning(self, "Missing address", "Set a Hostname, an IP Address, or both.")
            return

        if address_mode == "hostname" and not hostname:
            QMessageBox.warning(
                self, "Hostname required",
                "\"Use for agent bundles\" is set to Hostname, but the Hostname field is empty."
            )
            return

        if address_mode == "ip" and not ip:
            QMessageBox.warning(
                self, "IP Address required",
                "\"Use for agent bundles\" is set to IP Address, but the IP Address field is empty."
            )
            return

        try:
            port = int(self.port_input.text().strip())
        except ValueError:
            QMessageBox.warning(self, "Invalid port", "Port must be a number.")
            return

        try:
            config = api.set_controller_config(hostname, ip, address_mode, port)
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        self.hostname_input.setText(config.get("hostname") or "")
        self.ip_input.setCurrentText(config.get("ip") or "")

        index = self.address_mode_combo.findData(config.get("address_mode") or "hostname")
        self.address_mode_combo.setCurrentIndex(index if index >= 0 else 0)
        self._update_address_fields_enabled()

        self.port_input.setText(str(config.get("port") or 9000))

        if config.get("address_mode") == "all":
            self.controller_status_label.setText(
                f"Saved - bundles will use every detected IP on port {config.get('port')}, with failover."
            )
        else:
            used = config.get("address") or "(not set)"
            self.controller_status_label.setText(f"Saved - bundles will use: {used}:{config.get('port')}")

    # =========================================================
    # SECTION 2: ADMINISTRATORS
    # =========================================================
    def _build_administrators_section(self, layout):
        layout.addWidget(self._section_label("Administrators"))

        hint = QLabel(
            "Accounts that can log into this dashboard. A new administrator is forced to "
            "change their password on first login."
        )
        theme.style_hint_label(hint)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.admin_table = QTableWidget(0, 4)
        self.admin_table.setHorizontalHeaderLabels(["Username", "Created By", "Last Login", "Must Change Password"])
        self.admin_table.verticalHeader().setVisible(False)
        self.admin_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.admin_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.admin_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.admin_table.horizontalHeader().setStretchLastSection(True)
        self.admin_table.setFixedHeight(160)
        layout.addWidget(self.admin_table)

        admin_buttons = QHBoxLayout()

        admin_refresh_btn = QPushButton("Refresh")
        admin_refresh_btn.clicked.connect(self.refresh_administrators)

        self.remove_admin_btn = QPushButton("Remove Selected Administrator")
        self.remove_admin_btn.clicked.connect(self.remove_selected_administrator)

        admin_buttons.addWidget(admin_refresh_btn)
        admin_buttons.addWidget(self.remove_admin_btn)
        layout.addLayout(admin_buttons)

        # -- Add Administrator --
        layout.addWidget(QLabel("Add Administrator"))

        add_user_row = QHBoxLayout()
        add_user_row.addWidget(QLabel("Username:"))
        self.add_username_input = QLineEdit()
        self.add_username_input.setMaximumWidth(260)
        add_user_row.addWidget(self.add_username_input)
        layout.addLayout(add_user_row)

        add_pass_row = QHBoxLayout()
        add_pass_row.addWidget(QLabel("Temporary Password:"))
        self.add_password_input = QLineEdit()
        self.add_password_input.setEchoMode(QLineEdit.Password)
        self.add_password_input.setMaximumWidth(220)
        add_pass_row.addWidget(self.add_password_input)
        layout.addLayout(add_pass_row)

        self.add_admin_btn = QPushButton("Add Administrator")
        self.add_admin_btn.clicked.connect(self.add_administrator)
        layout.addWidget(self.add_admin_btn)

        layout.addWidget(self._divider())

        # -- Change My Own Credentials --
        layout.addWidget(QLabel("Change My Own Credentials"))

        self.current_user_label = QLabel("")
        theme.style_hint_label(self.current_user_label)
        layout.addWidget(self.current_user_label)

        current_pass_row = QHBoxLayout()
        current_pass_row.addWidget(QLabel("Current Password:"))
        self.current_password_input = QLineEdit()
        self.current_password_input.setEchoMode(QLineEdit.Password)
        self.current_password_input.setMaximumWidth(220)
        current_pass_row.addWidget(self.current_password_input)
        layout.addLayout(current_pass_row)

        new_user_row = QHBoxLayout()
        new_user_row.addWidget(QLabel("New Username:"))
        self.new_username_input = QLineEdit()
        self.new_username_input.setMaximumWidth(260)
        new_user_row.addWidget(self.new_username_input)
        layout.addLayout(new_user_row)

        new_pass_row = QHBoxLayout()
        new_pass_row.addWidget(QLabel("New Password:"))
        self.new_password_input = QLineEdit()
        self.new_password_input.setEchoMode(QLineEdit.Password)
        self.new_password_input.setMaximumWidth(220)
        new_pass_row.addWidget(self.new_password_input)
        layout.addLayout(new_pass_row)

        confirm_pass_row = QHBoxLayout()
        confirm_pass_row.addWidget(QLabel("Confirm New Password:"))
        self.confirm_password_input = QLineEdit()
        self.confirm_password_input.setEchoMode(QLineEdit.Password)
        self.confirm_password_input.setMaximumWidth(220)
        confirm_pass_row.addWidget(self.confirm_password_input)
        layout.addLayout(confirm_pass_row)

        self.save_credentials_btn = QPushButton("Save My Credentials")
        self.save_credentials_btn.clicked.connect(self.save_credentials)
        layout.addWidget(self.save_credentials_btn)

    def refresh_administrators(self):
        try:
            admins = api.list_administrators()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        self.admin_table.setRowCount(0)
        for row, admin in enumerate(admins):
            self.admin_table.insertRow(row)
            self.admin_table.setItem(row, 0, QTableWidgetItem(admin.get("username", "")))
            self.admin_table.setItem(row, 1, QTableWidgetItem(admin.get("created_by") or ""))
            self.admin_table.setItem(row, 2, QTableWidgetItem(self._format_timestamp(admin.get("last_login"))))
            self.admin_table.setItem(
                row, 3, QTableWidgetItem("Yes" if admin.get("must_change_password") else "No")
            )

        current = session.get_current_admin()
        if current:
            self.current_user_label.setText(f"Logged in as: {current}")
            self.new_username_input.setPlaceholderText(current)

    def add_administrator(self):
        username = self.add_username_input.text().strip()
        password = self.add_password_input.text()

        if not username:
            QMessageBox.warning(self, "Missing field", "Username is required.")
            return

        if not password:
            QMessageBox.warning(self, "Missing field", "Temporary password is required.")
            return

        try:
            api.add_administrator(username, password, actor=session.get_current_admin() or "")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        self.add_username_input.clear()
        self.add_password_input.clear()
        self.refresh_administrators()
        QMessageBox.information(self, "Added", f"Administrator '{username}' added.")

    def remove_selected_administrator(self):
        selected = self.admin_table.selectedItems()
        if not selected:
            QMessageBox.warning(self, "No selection", "Select an administrator to remove.")
            return

        username = self.admin_table.item(selected[0].row(), 0).text()

        confirm = QMessageBox.question(
            self, "Confirm",
            f"Remove administrator '{username}'? They will no longer be able to log into this dashboard."
        )
        if confirm != QMessageBox.Yes:
            return

        try:
            api.remove_administrator(username, actor=session.get_current_admin() or "")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        self.refresh_administrators()

    def save_credentials(self):
        acting_username = session.get_current_admin()
        if not acting_username:
            QMessageBox.critical(self, "Error", "No administrator is currently logged in.")
            return

        current_password = self.current_password_input.text()
        new_username = self.new_username_input.text().strip() or acting_username
        new_password = self.new_password_input.text()
        confirm_password = self.confirm_password_input.text()

        if not current_password:
            QMessageBox.warning(self, "Missing field", "Current password is required.")
            return

        if not new_password:
            QMessageBox.warning(self, "Missing field", "New password is required.")
            return

        if new_password != confirm_password:
            QMessageBox.warning(self, "Passwords don't match", "New password and confirmation must match.")
            return

        try:
            api.change_admin_credentials(acting_username, current_password, new_username, new_password)
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        session.set_current_admin(new_username)

        self.current_password_input.clear()
        self.new_password_input.clear()
        self.confirm_password_input.clear()
        self.refresh_administrators()
        QMessageBox.information(self, "Saved", "Your credentials have been updated.")

    # =========================================================
    # SECTION 3: ADMINISTRATOR PASSWORD POLICY
    # =========================================================
    def _build_admin_password_policy_section(self, layout):
        layout.addWidget(self._section_label("Administrator Password Policy"))

        hint = QLabel(
            "Complexity rules for the dashboard administrator accounts listed above "
            "only - enforced on Add Administrator, Save My Credentials, and the "
            "forced first-login password change. Does not affect accounts on managed "
            "hosts - see System Administration > Environmental Policies for that."
        )
        theme.style_hint_label(hint)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        minlen_row = QHBoxLayout()
        minlen_row.addWidget(QLabel("Minimum length:"))
        self.admin_pw_minlen_input = QLineEdit()
        self.admin_pw_minlen_input.setMaximumWidth(60)
        minlen_row.addWidget(self.admin_pw_minlen_input)
        layout.addLayout(minlen_row)

        require_row = QHBoxLayout()
        self.admin_pw_require_upper = QCheckBox("Require uppercase")
        self.admin_pw_require_lower = QCheckBox("Require lowercase")
        self.admin_pw_require_digit = QCheckBox("Require digit")
        self.admin_pw_require_symbol = QCheckBox("Require symbol")
        for cb in (
            self.admin_pw_require_upper, self.admin_pw_require_lower,
            self.admin_pw_require_digit, self.admin_pw_require_symbol,
        ):
            require_row.addWidget(cb)
        layout.addLayout(require_row)

        buttons = QHBoxLayout()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh_admin_password_policy)
        self.save_admin_password_policy_btn = QPushButton("Save Administrator Password Policy")
        self.save_admin_password_policy_btn.clicked.connect(self.save_admin_password_policy)
        buttons.addWidget(refresh_btn)
        buttons.addWidget(self.save_admin_password_policy_btn)
        layout.addLayout(buttons)

        self.admin_password_policy_status_label = QLabel("")
        layout.addWidget(self.admin_password_policy_status_label)

    def refresh_admin_password_policy(self):
        try:
            policy = api.get_admin_password_policy()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        self.admin_pw_minlen_input.setText(str(policy.get("minlen", 12)))
        self.admin_pw_require_upper.setChecked(policy.get("ucredit", -1) < 0)
        self.admin_pw_require_lower.setChecked(policy.get("lcredit", -1) < 0)
        self.admin_pw_require_digit.setChecked(policy.get("dcredit", -1) < 0)
        self.admin_pw_require_symbol.setChecked(policy.get("ocredit", -1) < 0)
        self.admin_password_policy_status_label.setText("")

    def save_admin_password_policy(self):
        try:
            minlen = int(self.admin_pw_minlen_input.text().strip())
        except ValueError:
            QMessageBox.warning(self, "Invalid value", "Minimum length must be a whole number.")
            return

        policy = {
            "minlen": minlen,
            "ucredit": -1 if self.admin_pw_require_upper.isChecked() else 0,
            "lcredit": -1 if self.admin_pw_require_lower.isChecked() else 0,
            "dcredit": -1 if self.admin_pw_require_digit.isChecked() else 0,
            "ocredit": -1 if self.admin_pw_require_symbol.isChecked() else 0,
        }

        try:
            api.set_admin_password_policy(policy)
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        self.refresh_admin_password_policy()
        self.admin_password_policy_status_label.setText(
            "Saved - now enforced on new/changed administrator passwords."
        )

    # =========================================================
    # SECTION 4: AUDIT LOG
    # =========================================================
    def _build_audit_log_section(self, layout):
        layout.addWidget(self._section_label("Audit Log"))

        hint = QLabel(
            "Logins (success/failure) and administrator account changes only - not a "
            "general command history."
        )
        theme.style_hint_label(hint)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.audit_table = QTableWidget(0, 4)
        self.audit_table.setHorizontalHeaderLabels(["Time", "Event", "Username", "Detail"])
        self.audit_table.verticalHeader().setVisible(False)
        self.audit_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.audit_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.audit_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.audit_table.horizontalHeader().setStretchLastSection(True)
        self.audit_table.setFixedHeight(220)
        layout.addWidget(self.audit_table)

        audit_refresh_btn = QPushButton("Refresh Audit Log")
        audit_refresh_btn.clicked.connect(self.refresh_audit_log)
        layout.addWidget(audit_refresh_btn)

    def refresh_audit_log(self):
        try:
            entries = api.get_admin_audit_log()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        self.audit_table.setRowCount(0)
        for row, entry in enumerate(entries):
            self.audit_table.insertRow(row)
            self.audit_table.setItem(row, 0, QTableWidgetItem(self._format_timestamp(entry.get("timestamp"))))
            self.audit_table.setItem(row, 1, QTableWidgetItem(entry.get("event", "")))
            self.audit_table.setItem(row, 2, QTableWidgetItem(entry.get("username", "")))
            self.audit_table.setItem(row, 3, QTableWidgetItem(entry.get("detail") or ""))

    @staticmethod
    def _format_timestamp(value):
        if not value:
            return "Never"
        try:
            return datetime.datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, OSError):
            return str(value)

    # =========================================================
    # SECTION 5: LICENSE & VERSION
    # =========================================================
    def _build_license_version_section(self, layout):
        layout.addWidget(self._section_label("License & Version"))

        layout.addLayout(self._info_row("Sysible Controller:", f"v{VERSION}"))

        hint = QLabel(
            "Licensing is not yet enforced on this build - this field is just "
            "somewhere to record a license key ahead of that being built out."
        )
        theme.style_hint_label(hint)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        license_row = QHBoxLayout()
        license_row.addWidget(QLabel("License Key:"))
        self.license_key_input = QLineEdit()
        self.license_key_input.setMaximumWidth(320)
        license_row.addWidget(self.license_key_input)
        layout.addLayout(license_row)

        self.save_license_btn = QPushButton("Save License Key")
        self.save_license_btn.clicked.connect(self.save_license_config)
        layout.addWidget(self.save_license_btn)

        self.license_status_label = QLabel("")
        layout.addWidget(self.license_status_label)

    def refresh_license_config(self):
        try:
            config = api.get_license_config()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        self.license_key_input.setText(config.get("license_key") or "")
        self.license_status_label.setText("")

    def save_license_config(self):
        license_key = self.license_key_input.text().strip()

        try:
            config = api.set_license_config(license_key)
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        self.license_key_input.setText(config.get("license_key") or "")
        self.license_status_label.setText("Saved.")

    def refresh(self):
        self.refresh_controller_config()
        self.refresh_administrators()
        self.refresh_admin_password_policy()
        self.refresh_audit_log()
        self.refresh_license_config()
