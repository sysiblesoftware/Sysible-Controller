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
    QTextEdit,
    QMessageBox,
    QFileDialog,
    QAbstractItemView,
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
            self.agents = api.get_agents()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        self._populate_combo()
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

        self.details.setPlainText(text)

        self._sync_combo_to_selected()

    # =====================================================
    # SET ENVIRONMENT
    # =====================================================
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

        self.remove_btn.setEnabled(False)
        self.disenroll_status.setText(f"Asking {host_id} to remove its systemd service...")

        try:
            task_ids = api.queue_command_on_hosts([host_id], api.cmd_uninstall_agent_service())
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
