from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QListWidget,
    QListWidgetItem,
    QComboBox,
    QInputDialog,
    QLineEdit,
    QTextEdit,
    QMessageBox,
    QFileDialog,
    QAbstractItemView,
    QGroupBox,
    QApplication,
)

from client import api
from client import theme
from client.events import bus
from client.branding import make_page_header
from client.collapsible_groups import (
    make_group_header_item, apply_collapse_state, get_collapsed_groups,
    connect_group_toggle, add_collapse_expand_buttons,
)

_NEW_ENV_OPTION = "+ New environment..."
_UNASSIGNED_LABEL = "Unassigned"

# How long to wait for an online host to acknowledge the
# uninstall-systemd-service task queued by disenroll_host() below
# before giving up and falling back to a DB-only removal (host is
# presumably offline - the operator can still run disenroll_agent.sh
# on it directly, the bundle script remains independent of this).
_DISENROLL_POLL_MS = 2000
_DISENROLL_MAX_POLLS = 6


class HostEnrollmentPage(QWidget):

    def __init__(self):
        super().__init__()

        self.setWindowTitle("Sysible Controller Host Enrollment")
        # Open at a usable size rather than Qt's tiny default. Freely
        # resizable.
        self.resize(900, 720)
        self.setMinimumSize(680, 500)

        self.agents = []
        self.environments = []
        self.selected_agent = None

        self.pending_disenroll = None  # {"host_id", "task_id", "attempts"}
        self.disenroll_poll_timer = QTimer()
        self.disenroll_poll_timer.timeout.connect(self._poll_disenroll)

        layout = QVBoxLayout()

        # =====================================================
        # TITLE
        # =====================================================
        layout.addLayout(make_page_header("Sysible Controller Host Enrollment", font_size=22, logo_height=32))

        # Community-edition host cap, shown so the limit is visible up front
        # rather than only surfacing as an error when the (N+1)th host enrolls.
        self.edition_label = QLabel("")
        self.edition_label.setAlignment(Qt.AlignCenter)
        theme.style_hint_label(self.edition_label)
        layout.addWidget(self.edition_label)
        self._refresh_edition_label()

        # =====================================================
        # AGENT BUNDLE
        # The bundle already bakes in a fresh one-time enrollment token
        # at build time (see backend/agent_bundle.py), so there's no
        # separate "generate a token" step to expose here anymore - just
        # download a ready-to-run bundle and put it on the target host.
        # =====================================================
        bundle_label = QLabel("Agent Bundle")
        bundle_label.setStyleSheet("font-size:18px;font-weight:bold;")
        layout.addWidget(bundle_label)

        bundle_hint = QLabel(
            "Same ready-to-run bundle the Webserver Portal hands out, built\n"
            "on demand here instead - useful when the portal isn't running.\n"
            "Each download bakes in a fresh, one-time enrollment token."
        )
        theme.style_hint_label(bundle_hint)
        layout.addWidget(bundle_hint)

        self.download_bundle_btn = QPushButton("Download Agent Bundle")
        self.download_bundle_btn.clicked.connect(self.download_bundle)
        layout.addWidget(self.download_bundle_btn)

        # =====================================================
        # COMMAND-LINE BUNDLE DOWNLOAD (curl)
        # Lives here, alongside the GUI download, since both are "get the
        # agent onto a host" - the way you enroll a headless/terminal-only
        # box. It pulls from the Webserver Portal's /cli/bundle endpoint, so
        # the portal must be running with login credentials set (configure
        # that on the Webserver Portal page).
        # =====================================================
        # =====================================================
        # WEBSERVER PORTAL (for the curl download)
        # The curl one-liner below pulls the agent bundle from the portal's
        # /cli/bundle endpoint, so the portal must be running on the right
        # port. Manage that right here instead of a separate page.
        # =====================================================
        portal_label = QLabel("Webserver Portal (for the curl download)")
        portal_label.setStyleSheet("font-size:18px;font-weight:bold;")
        layout.addWidget(portal_label)

        portal_hint = QLabel(
            "The curl one-liner below downloads the agent bundle from the "
            "Webserver Portal, so the portal must be running and reachable on "
            "this port. Start it while provisioning hosts; stop it when you're done."
        )
        theme.style_hint_label(portal_hint)
        portal_hint.setWordWrap(True)
        layout.addWidget(portal_hint)

        self.portal_status_label = QLabel("Portal: …")
        layout.addWidget(self.portal_status_label)

        portal_buttons = QHBoxLayout()
        self.portal_start_btn = QPushButton("Start Portal")
        self.portal_start_btn.clicked.connect(self.start_portal)
        self.portal_stop_btn = QPushButton("Stop Portal")
        self.portal_stop_btn.clicked.connect(self.stop_portal)
        portal_buttons.addWidget(self.portal_start_btn)
        portal_buttons.addWidget(self.portal_stop_btn)
        portal_buttons.addStretch()
        layout.addLayout(portal_buttons)

        portal_port_row = QHBoxLayout()
        portal_port_row.addWidget(QLabel("Port:"))
        self.portal_port_input = QLineEdit()
        self.portal_port_input.setMaximumWidth(100)
        portal_port_row.addWidget(self.portal_port_input)
        self.portal_save_port_btn = QPushButton("Save Port")
        self.portal_save_port_btn.clicked.connect(self.save_portal_port)
        portal_port_row.addWidget(self.portal_save_port_btn)
        portal_port_row.addStretch()
        layout.addLayout(portal_port_row)

        portal_admin_row = QHBoxLayout()
        self.portal_admin_btn = QPushButton("Manage Portal Login & Files…")
        self.portal_admin_btn.clicked.connect(self.open_portal_admin)
        portal_admin_row.addWidget(self.portal_admin_btn)
        portal_admin_hint = QLabel("Set the host-operator login, review login history and sessions, and manage the file pools.")
        theme.style_hint_label(portal_admin_hint)
        portal_admin_hint.setWordWrap(True)
        portal_admin_row.addWidget(portal_admin_hint, 1)
        layout.addLayout(portal_admin_row)

        curl_label = QLabel("Command-Line Bundle Download (curl)")
        curl_label.setStyleSheet("font-size:18px;font-weight:bold;")
        layout.addWidget(curl_label)

        curl_hint = QLabel(
            "For terminal-only or headless hosts that can't open a browser: "
            "downloads the agent bundle, unzips it, and runs the installer in "
            "one shot, authenticating with the Webserver Portal's login over "
            "HTTP Basic auth (curl -u). Needs the Webserver Portal running with "
            "credentials configured (see the Webserver Portal page). Replace "
            "<password> between the single quotes with the real portal password. "
            "-k skips the self-signed-cert check; the final install step needs sudo."
        )
        theme.style_hint_label(curl_hint)
        curl_hint.setWordWrap(True)
        layout.addWidget(curl_hint)

        self.curl_text = QTextEdit()
        self.curl_text.setReadOnly(True)
        self.curl_text.setStyleSheet("font-family: monospace;")
        self.curl_text.setFixedHeight(140)
        layout.addWidget(self.curl_text)

        self.copy_curl_btn = QPushButton("Copy to Clipboard")
        self.copy_curl_btn.clicked.connect(self.copy_curl_command)
        layout.addWidget(self.copy_curl_btn)

        # =====================================================
        # INVENTORY LABEL
        # =====================================================
        inventory_label = QLabel("Enrolled Hosts (grouped by environment)")
        inventory_label.setStyleSheet("font-size:18px;font-weight:bold;")
        layout.addWidget(inventory_label)

        # =====================================================
        # LIST
        # =====================================================
        self.agent_list = QListWidget()
        # Multi-select so several hosts can be assigned to one environment at
        # once (Ctrl/Shift-click). Single selection still works as before.
        self.agent_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.agent_list.itemSelectionChanged.connect(self.load_details)
        connect_group_toggle(self.agent_list)

        collapse_row = QHBoxLayout()
        collapse_row.addStretch()
        btn_collapse_all, btn_expand_all = add_collapse_expand_buttons(self.agent_list)
        collapse_row.addWidget(btn_collapse_all)
        collapse_row.addWidget(btn_expand_all)
        layout.addLayout(collapse_row)

        layout.addWidget(self.agent_list)

        # =====================================================
        # DETAILS
        # =====================================================
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        layout.addWidget(self.details)

        # =====================================================
        # ENVIRONMENT ASSIGNMENT
        # =====================================================
        env_row = QHBoxLayout()

        env_row.addWidget(QLabel("Environment:"))

        self.env_combo = QComboBox()
        self.env_combo.currentTextChanged.connect(self._handle_combo_change)
        self.env_combo.setMaximumWidth(220)
        env_row.addWidget(self.env_combo)

        self.set_env_btn = QPushButton("Set Environment")
        self.set_env_btn.setToolTip(
            "Assign the chosen environment to every selected host "
            "(Ctrl/Shift-click to select several).")
        self.set_env_btn.clicked.connect(self.set_environment)
        env_row.addWidget(self.set_env_btn)

        layout.addLayout(env_row)

        # --- Sudo policy (a host attribute: does its sudo need a password?) ---
        # This box sets *how* the host's sudo works - controller policy that
        # belongs with the host. The operator's actual sudo *password* is a
        # personal credential and lives on the dashboard header ("Sudo
        # Password"), not here, so it isn't duplicated per page.
        sudo_box = QGroupBox("Sudo policy (selected host / environment)")
        sudo_v = QVBoxLayout(sudo_box)

        sudo_row = QHBoxLayout()
        sudo_row.addWidget(QLabel("Selected host(s):"))
        btn_pw_sudo = QPushButton("Requires Password")
        btn_pw_sudo.setToolTip(
            "Mark the selected host(s) as forbidding passwordless sudo. Dispatched "
            "commands will then supply your stored sudo password (agent uses 'sudo -S').")
        btn_pw_sudo.clicked.connect(lambda: self.set_sudo_mode(True))
        sudo_row.addWidget(btn_pw_sudo)
        btn_nopw_sudo = QPushButton("Passwordless (NOPASSWD)")
        btn_nopw_sudo.setToolTip(
            "Mark the selected host(s) as allowing passwordless sudo (agent uses 'sudo -n'). Default.")
        btn_nopw_sudo.clicked.connect(lambda: self.set_sudo_mode(False))
        sudo_row.addWidget(btn_nopw_sudo)
        sudo_row.addStretch()
        sudo_v.addLayout(sudo_row)

        # Per-environment default: hosts inherit this when assigned to the
        # environment chosen in the combo above.
        env_default_row = QHBoxLayout()
        self.env_sudo_label = QLabel("")
        theme.style_hint_label(self.env_sudo_label)
        env_default_row.addWidget(self.env_sudo_label)
        env_default_row.addStretch()
        btn_env_default = QPushButton("Set Environment's Sudo Default…")
        btn_env_default.setToolTip(
            "Set whether the environment selected above defaults to password-sudo. Hosts "
            "inherit it when assigned to that environment.")
        btn_env_default.clicked.connect(self.set_environment_sudo_default)
        env_default_row.addWidget(btn_env_default)
        sudo_v.addLayout(env_default_row)
        self.env_combo.currentTextChanged.connect(self._update_env_sudo_label)

        sudo_hint = QLabel(
            "For password-sudo hosts, store your own sudo password from the "
            "“Sudo Password” button in the dashboard header.")
        theme.style_hint_label(sudo_hint)
        sudo_hint.setWordWrap(True)
        sudo_v.addWidget(sudo_hint)

        layout.addWidget(sudo_box)

        # =====================================================
        # BUTTONS
        # =====================================================
        buttons = QHBoxLayout()

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)

        self.remove_btn = QPushButton("Disenroll Host")
        self.remove_btn.clicked.connect(self.disenroll_host)

        buttons.addWidget(self.refresh_btn)
        buttons.addWidget(self.remove_btn)

        layout.addLayout(buttons)

        self.disenroll_status = QLabel("")
        theme.style_hint_label(self.disenroll_status)
        layout.addWidget(self.disenroll_status)

        self.setLayout(layout)

        bus.host_removed.connect(self.refresh)

        self.refresh()

    # =====================================================
    # AGENT BUNDLE
    # =====================================================
    def download_bundle(self):
        save_path, _ = QFileDialog.getSaveFileName(
            self, "Save Agent Bundle", "sysible-agent-bundle.zip", "Zip files (*.zip)"
        )

        if not save_path:
            return

        try:
            api.download_agent_bundle(save_path)
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        QMessageBox.information(
            self, "Bundle downloaded",
            f"Saved to {save_path}.\n\n"
            "This bundle's enrollment token is one-time use. If the host "
            "running it already has a state file from a previous "
            "enrollment, it will keep using that stale state and ignore "
            "this token - clear that host's agent_state.json first (or "
            "let the agent's self-heal do it automatically next time the "
            "controller rejects it as an unknown host)."
        )

    # =====================================================
    # INVENTORY
    # =====================================================
    def _refresh_edition_label(self):
        try:
            info = api.get_edition()
        except Exception:
            info = {}
        limit = info.get("host_limit")
        count = info.get("host_count", 0)
        if limit is None:
            self.edition_label.setText("")
            self.edition_label.setVisible(False)
            return
        self.edition_label.setVisible(True)
        at_cap = count >= limit
        self.edition_label.setText(
            f"Community edition — {count}/{limit} hosts used"
            + ("  ·  limit reached; remove a host to enroll another" if at_cap else "")
        )

    def refresh(self):
        self._refresh_edition_label()
        previously_selected = self.selected_agent.get("host_id") if self.selected_agent else None

        try:
            self.environments = api.list_environments()
        except Exception:
            self.environments = []

        try:
            self._env_sudo_defaults = api.get_environment_sudo_defaults()
        except Exception:
            self._env_sudo_defaults = {}

        try:
            self.agents = api.get_agents()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        self._refresh_portal_status()
        self._refresh_curl_command()
        self._populate_combo()
        self._update_env_sudo_label()
        self._populate_list()

        current_ids = {a.get("host_id") for a in self.agents}

        if previously_selected and previously_selected in current_ids:
            self._reselect(previously_selected)
        else:
            self.selected_agent = None
            self.details.clear()

    def _populate_list(self):
        collapsed_envs = get_collapsed_groups(self.agent_list)

        self.agent_list.clear()

        groups = {}
        for agent in self.agents:
            env = agent.get("environment") or ""
            groups.setdefault(env, []).append(agent)

        known_envs = [e for e in self.environments if e in groups]
        extra_envs = sorted(e for e in groups if e and e not in self.environments)
        unassigned = groups.get("", [])

        for env in known_envs + extra_envs:
            self._add_header(env, collapsed_envs)
            for agent in groups[env]:
                self._add_agent_item(agent)

        if unassigned:
            self._add_header(_UNASSIGNED_LABEL, collapsed_envs)
            for agent in unassigned:
                self._add_agent_item(agent)

        apply_collapse_state(self.agent_list)

    def _add_header(self, text, collapsed_envs):
        item = make_group_header_item(text, collapsed=text in collapsed_envs)
        self.agent_list.addItem(item)

    def _add_agent_item(self, agent):
        name = agent.get("hostname") or agent["host_id"]
        item = QListWidgetItem(f"    {name}")
        item.setData(Qt.UserRole, agent)
        self.agent_list.addItem(item)

    def _reselect(self, host_id):
        for i in range(self.agent_list.count()):
            item = self.agent_list.item(i)
            data = item.data(Qt.UserRole)
            if data and data.get("host_id") == host_id:
                self.agent_list.setCurrentItem(item)
                return

    # =====================================================
    # ENVIRONMENT COMBO
    # =====================================================
    def _populate_combo(self):
        self.env_combo.blockSignals(True)
        self.env_combo.clear()
        self.env_combo.addItems(self.environments)
        self.env_combo.addItem(_NEW_ENV_OPTION)
        self.env_combo.blockSignals(False)

    def _handle_combo_change(self, text):
        if text != _NEW_ENV_OPTION:
            return

        name, ok = QInputDialog.getText(self, "New Environment", "Environment name:")

        if not ok or not name.strip():
            self._populate_combo()
            return

        name = name.strip()

        try:
            self.environments = api.create_environment(name)
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            self._populate_combo()
            return

        self._populate_combo()
        self.env_combo.setCurrentText(name)

    def _sync_combo_to_selected(self):
        if not self.selected_agent:
            return

        env = self.selected_agent.get("environment") or ""
        idx = self.env_combo.findText(env)

        if idx >= 0:
            self.env_combo.setCurrentIndex(idx)

    # =====================================================
    # DETAILS
    # =====================================================
    def load_details(self):
        item = self.agent_list.currentItem()

        if item is None:
            return

        agent = item.data(Qt.UserRole)

        if agent is None:
            # Selected a non-selectable environment header row.
            return

        self.selected_agent = agent

        selected = self._selected_agents()
        text = ""
        if len(selected) > 1:
            text += (f"{len(selected)} hosts selected — Set Environment will apply to all "
                     f"of them.\n\nDetails (last selected):\n")
        text += f"Hostname: {agent.get('hostname')}\n"
        text += f"Host ID: {agent.get('host_id')}\n"
        text += f"Platform: {agent.get('platform')}\n"
        text += f"Kernel: {agent.get('kernel')}\n"
        text += f"Status: {agent.get('status')}\n"
        text += f"Environment: {agent.get('environment') or '(unassigned)'}\n"
        text += ("Sudo: requires password (sudo -S)\n"
                 if agent.get("requires_sudo_password")
                 else "Sudo: passwordless (sudo -n)\n")

        self.details.setPlainText(text)

        self._sync_combo_to_selected()

    # =====================================================
    # SET ENVIRONMENT
    # =====================================================
    def set_sudo_mode(self, requires_password):
        agents = self._selected_agents()
        if not agents:
            QMessageBox.information(self, "No host selected", "Select one or more hosts first.")
            return
        failures = []
        for agent in agents:
            try:
                api.set_sudo_password_required(agent["host_id"], requires_password)
            except Exception as e:
                failures.append(f"{agent.get('hostname') or agent['host_id']}: {e}")
        self.refresh()
        if failures:
            QMessageBox.critical(self, "Some hosts failed", "\n".join(failures))
        elif requires_password:
            QMessageBox.information(
                self, "Password sudo",
                f"{len(agents)} host(s) set to require a sudo password. Make sure you've "
                "stored your sudo password (the “Sudo Password” button in the dashboard "
                "header) so dispatched commands can elevate.")

    def _update_env_sudo_label(self):
        env = self.env_combo.currentText()
        if not env or env == _NEW_ENV_OPTION:
            self.env_sudo_label.setText("")
            return
        required = getattr(self, "_env_sudo_defaults", {}).get(env, False)
        self.env_sudo_label.setText(
            f"Environment '{env}' default: "
            + ("requires sudo password" if required else "passwordless sudo"))

    def set_environment_sudo_default(self):
        env = self.env_combo.currentText()
        if not env or env == _NEW_ENV_OPTION:
            QMessageBox.information(self, "No environment", "Choose an environment first.")
            return
        reply = QMessageBox.question(
            self, "Environment sudo default",
            f"Should hosts in '{env}' default to REQUIRING a sudo password?\n\n"
            "Yes = password-sudo (agent uses 'sudo -S').\n"
            "No = passwordless (NOPASSWD, 'sudo -n').\n\n"
            "Hosts inherit this when assigned to the environment.",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
        if reply == QMessageBox.Cancel:
            return
        try:
            api.set_environment_sudo_default(env, reply == QMessageBox.Yes)
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return
        self.refresh()

    # =====================================================
    # WEBSERVER PORTAL CONTROLS (rolled into this page)
    # =====================================================
    def _refresh_portal_status(self):
        try:
            status = api.get_portal_status()
        except Exception:
            status = {}
        running = bool(status.get("running"))
        port = status.get("configured_port") or status.get("port")
        text = "Portal: Running" if running else "Portal: Stopped"
        if port:
            text += f" (port {port})"
        if not status.get("credentials_configured"):
            text += "  -  no portal login set yet (set one to authenticate curl)"
        self.portal_status_label.setText(text)
        self.portal_start_btn.setEnabled(not running)
        self.portal_stop_btn.setEnabled(running)
        if port and not self.portal_port_input.text().strip():
            self.portal_port_input.setText(str(port))

    def start_portal(self):
        try:
            result = api.start_portal()
            if isinstance(result, dict) and not result.get("running") and result.get("error"):
                QMessageBox.critical(self, "Portal failed to start", result["error"])
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
        self._refresh_portal_status()
        self._refresh_curl_command()

    def stop_portal(self):
        try:
            api.stop_portal()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
        self._refresh_portal_status()
        self._refresh_curl_command()

    def open_portal_admin(self):
        # The detailed portal admin (credentials, login history, sessions, file
        # pools) opens here from Host Enrollment - the portal is no longer a
        # separate top-level tile.
        from client.webserver_portal_page import WebserverPortalPage
        if getattr(self, "_portal_admin_window", None) is None:
            self._portal_admin_window = WebserverPortalPage()
        self._portal_admin_window.show()
        self._portal_admin_window.raise_()
        self._portal_admin_window.activateWindow()
        if hasattr(self._portal_admin_window, "refresh"):
            try:
                self._portal_admin_window.refresh()
            except Exception:
                pass

    def save_portal_port(self):
        try:
            port = int(self.portal_port_input.text().strip())
        except ValueError:
            QMessageBox.warning(self, "Invalid port", "Port must be a number.")
            return
        if not (1 <= port <= 65535):
            QMessageBox.warning(self, "Invalid port", "Port must be between 1 and 65535.")
            return
        try:
            api.set_portal_port(port)
            QMessageBox.information(self, "Saved",
                                    f"Portal port set to {port}. Restart the portal if it's running.")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
        self._refresh_portal_status()
        self._refresh_curl_command()

    def _refresh_curl_command(self):
        """Build the one-line curl install command from the portal's status and
        the controller address. Mirrors the Webserver Portal page's data
        sources; placeholders are shown until the portal address/credentials
        are configured there."""
        try:
            status = api.get_portal_status()
        except Exception:
            status = {}
        try:
            config = api.get_controller_config()
        except Exception:
            config = {}

        host = config.get("address") or "<this machine's address>"
        port = status.get("configured_port") or status.get("port") or 8090
        user = status.get("username") if status.get("credentials_configured") else "<username>"

        self.curl_text.setPlainText(
            f"curl -k -sS -f -u '{user}:<password>' "
            f"-o sysible-agent-bundle.zip "
            f'"https://{host}:{port}/cli/bundle" '
            f"&& unzip -o sysible-agent-bundle.zip -d sysible-agent-bundle "
            f"&& cd sysible-agent-bundle "
            f"&& chmod +x run_agent.sh "
            f"&& sudo ./run_agent.sh"
        )

    def copy_curl_command(self):
        QApplication.clipboard().setText(self.curl_text.toPlainText())

    def _selected_agents(self):
        """Every selected host row (skips environment header rows). Falls
        back to self.selected_agent if the list reports nothing selected."""
        agents = []
        for item in self.agent_list.selectedItems():
            agent = item.data(Qt.UserRole)
            if agent is not None:
                agents.append(agent)
        if not agents and self.selected_agent:
            agents = [self.selected_agent]
        return agents

    def set_environment(self):
        agents = self._selected_agents()
        if not agents:
            QMessageBox.information(self, "No host selected", "Select one or more hosts first.")
            return

        env = self.env_combo.currentText()
        if env == _NEW_ENV_OPTION:
            return

        failures = []
        for agent in agents:
            try:
                api.set_agent_environment(agent["host_id"], env)
            except Exception as e:
                failures.append(f"{agent.get('hostname') or agent['host_id']}: {e}")

        self.refresh()

        if failures:
            QMessageBox.critical(
                self, "Some hosts failed",
                f"Set environment failed for {len(failures)} of {len(agents)} host(s):\n\n"
                + "\n".join(failures))

    # =====================================================
    # DISENROLL
    #
    # Two independent ways to remove the systemd service from a
    # managed host: this button (queues a teardown task here, over the
    # normal agent dispatch path, then drops the enrollment once it's
    # acknowledged or a short timeout passes) or running
    # disenroll_agent.sh from the agent bundle directly on the host.
    # Neither depends on the other - if the host is offline this
    # button still removes the enrollment, just without the remote
    # teardown, and the script remains available as the manual
    # fallback.
    # =====================================================
    def disenroll_host(self):
        if not self.selected_agent:
            return

        if self.pending_disenroll is not None:
            return

        host_id = self.selected_agent["host_id"]

        reply = QMessageBox.question(
            self,
            "Confirm",
            f"Remove {host_id}?\n\n"
            "If this host is online, its systemd service will also be "
            "stopped and removed before the enrollment is dropped. If "
            "it's offline, the enrollment is removed here regardless, "
            "but you'll need to run disenroll_agent.sh on that host "
            "directly to clean up its systemd service."
        )

        if reply != QMessageBox.Yes:
            return

        # Tearing down the agent's own systemd service needs root. Under the
        # run-as-you model the task runs as your local user and elevates via
        # sudo, so a password-sudo host needs your stored sudo password - the
        # same as any other privileged action (see _api_dispatch.run_on_entry).
        # Without it the teardown would just bounce off `sudo -n`.
        become_password = None
        if self.selected_agent.get("requires_sudo_password"):
            from client import become_credentials
            label = self.selected_agent.get("hostname") or host_id
            become_password = become_credentials.get_password(label)
            if not become_password:
                QMessageBox.warning(
                    self, "Sudo password needed",
                    f"'{label}' is set to require a sudo password, so tearing down its "
                    "agent service needs your stored sudo password — but none is saved. "
                    "Click “Sudo Password” in the dashboard header to set it, then disenroll "
                    "again. (Or remove the enrollment anyway and run disenroll_agent.sh on "
                    "the host directly.)")
                return

        self.remove_btn.setEnabled(False)
        self.disenroll_status.setText(f"Asking {host_id} to remove its systemd service...")

        try:
            task_ids = api.queue_command_on_hosts(
                [host_id], api.cmd_uninstall_agent_service(),
                become_password=become_password)
        except Exception:
            task_ids = {}

        task_id = task_ids.get(host_id)

        if task_id is None:
            self._finish_disenroll(
                host_id,
                "Could not queue the systemd-service teardown on that host. "
                "Enrollment removed regardless - run disenroll_agent.sh on "
                "that host directly to clean up its systemd service."
            )
            return

        self.pending_disenroll = {"host_id": host_id, "task_id": task_id, "attempts": 0}
        self.disenroll_poll_timer.start(_DISENROLL_POLL_MS)

    def _poll_disenroll(self):
        if self.pending_disenroll is None:
            self.disenroll_poll_timer.stop()
            return

        host_id = self.pending_disenroll["host_id"]
        task_id = self.pending_disenroll["task_id"]

        try:
            raw = api.get_result_by_task(host_id, task_id)
        except Exception:
            raw = None

        output = api.parse_task_output(raw) if raw else None

        if output is not None:
            self.disenroll_poll_timer.stop()
            note = (output.get("stdout") or output.get("stderr") or "").strip()
            self._finish_disenroll(host_id, note or "Host acknowledged the teardown request.")
            return

        self.pending_disenroll["attempts"] += 1

        if self.pending_disenroll["attempts"] >= _DISENROLL_MAX_POLLS:
            self.disenroll_poll_timer.stop()
            self._finish_disenroll(
                host_id,
                "Host did not respond in time (likely offline). Enrollment "
                "removed regardless - run disenroll_agent.sh on that host "
                "directly to clean up its systemd service."
            )
            return

        self.disenroll_status.setText(f"Waiting for {host_id} to acknowledge teardown...")

    def _finish_disenroll(self, host_id, note):
        self.pending_disenroll = None
        self.remove_btn.setEnabled(True)
        self.disenroll_status.setText("")

        try:
            api.disenroll_agent(host_id)
            bus.host_removed.emit(host_id)
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        if note:
            QMessageBox.information(self, "Host disenrolled", note)
