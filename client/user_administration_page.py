# Retired from the dashboard. This page used to be the top-level
# "User Administration" card on client/home.py, but that duplicated
# the more complete User & Group Administration tile under System
# Administration (client/user_group_administration_page.py, opened
# via client/system_administration_page.py) - same job, two places to
# find it. The dashboard card now points at
# client/admin_configuration_page.py (Sysible Controller
# Settings) instead.
#
# Nothing imports this module anymore. Safe to delete this file.

import json
import time

from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout,
    QListWidget, QListWidgetItem, QLabel, QPushButton,
    QLineEdit, QTextEdit, QComboBox, QMessageBox
)
from PySide6.QtCore import Qt, QTimer

from client import api
from client.events import bus

SYNC_POLL_MS = 2000
SYNC_TIMEOUT_S = 60
HOST_REFRESH_MS = 10000
AUTO_RESYNC_DELAY_MS = 4000


class UserAdministrationPage(QWidget):
    """
    User Administration now targets *enrolled hosts* rather than the
    controller's own machine: check one or more hosts above, Sync to
    pull their real user/group state, then any action below (lock,
    sudo, create, delete, groups, password) is queued as a command on
    every checked host.
    """

    def __init__(self):
        super().__init__()

        self.setWindowTitle("User Administration")

        self.active_host_id = None
        self.host_data = {}      # host_id -> {users, groups, sessions, synced_at, error}
        self.selected_user = None
        self.pending_sync = {}   # host_id -> task_id
        self.sync_total = 0
        self.sync_deadline = None

        main = QVBoxLayout()
        self.setLayout(main)

        # =========================================================
        # HEADER
        # =========================================================
        title = QLabel("User Administration")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        main.addWidget(title)

        # =========================================================
        # TARGET HOSTS
        # =========================================================
        hosts_box = QVBoxLayout()

        hosts_header = QHBoxLayout()
        hosts_title = QLabel("Target Hosts (enrolled)")
        hosts_title.setStyleSheet("font-weight: bold;")
        hosts_header.addWidget(hosts_title)
        hosts_header.addStretch()

        btn_refresh_hosts = QPushButton("Refresh Hosts")
        btn_refresh_hosts.clicked.connect(self.load_hosts)

        btn_select_all = QPushButton("Select All")
        btn_select_all.clicked.connect(self.select_all_hosts)

        btn_deselect_all = QPushButton("Deselect All")
        btn_deselect_all.clicked.connect(self.deselect_all_hosts)

        self.btn_sync = QPushButton("Sync Checked Hosts")
        self.btn_sync.clicked.connect(self.sync_checked_hosts)

        hosts_header.addWidget(btn_refresh_hosts)
        hosts_header.addWidget(btn_select_all)
        hosts_header.addWidget(btn_deselect_all)
        hosts_header.addWidget(self.btn_sync)

        hosts_box.addLayout(hosts_header)

        self.host_list = QListWidget()
        self.host_list.setFixedHeight(110)
        self.host_list.itemClicked.connect(self.on_host_selected)
        hosts_box.addWidget(self.host_list)

        self.sync_status = QLabel("Check one or more hosts, then Sync to pull live user data.")
        self.sync_status.setStyleSheet("color: #888;")
        hosts_box.addWidget(self.sync_status)

        main.addLayout(hosts_box)

        # =========================================================
        # SPLIT
        # =========================================================
        split = QHBoxLayout()
        split.setContentsMargins(5, 5, 5, 5)
        split.setSpacing(6)

        # =========================================================
        # LEFT PANEL
        # =========================================================
        left = QVBoxLayout()

        self.active_host_label = QLabel("Viewing: (no host selected)")
        self.active_host_label.setStyleSheet("font-weight: bold;")

        self.user_search = QLineEdit()
        self.user_search.setPlaceholderText("Search users...")
        self.user_search.textChanged.connect(self.filter_users)

        users_label = QLabel("Users (on selected host)")
        users_label.setStyleSheet("font-weight: bold;")

        self.user_list = QListWidget()
        self.user_list.itemClicked.connect(self.on_select_user)

        left.addWidget(self.active_host_label)
        left.addWidget(self.user_search)
        left.addWidget(users_label)
        left.addWidget(self.user_list)

        split.addLayout(left, 2)

        # =========================================================
        # RIGHT PANEL
        # =========================================================
        right = QVBoxLayout()
        right.setSpacing(6)

        self.account_info = QLabel()
        right.addWidget(self.account_info)

        self.status_label = QLabel()
        right.addWidget(self.status_label)

        sudo_title = QLabel("Sudo Status")
        sudo_title.setStyleSheet("font-weight: bold;")
        self.sudo_status = QLabel()
        right.addWidget(sudo_title)
        right.addWidget(self.sudo_status)

        groups_title = QLabel("User's Groups")
        groups_title.setStyleSheet("font-weight: bold;")
        self.user_groups_label = QLabel()
        right.addWidget(groups_title)
        right.addWidget(self.user_groups_label)

        sessions_title = QLabel("Active Sessions (this host)")
        sessions_title.setStyleSheet("font-weight: bold;")
        self.sessions = QTextEdit()
        self.sessions.setReadOnly(True)
        self.sessions.setMaximumHeight(100)
        right.addWidget(sessions_title)
        right.addWidget(self.sessions)

        # =========================================================
        # USER ACTIONS (dispatched to all checked hosts)
        # =========================================================
        action_row = QHBoxLayout()

        self.btn_lock = QPushButton("Lock")
        self.btn_unlock = QPushButton("Unlock")
        self.btn_sudo = QPushButton("Toggle Sudo")
        self.btn_delete = QPushButton("Delete User")

        for b in [self.btn_lock, self.btn_unlock, self.btn_sudo, self.btn_delete]:
            b.setFixedWidth(110)
            action_row.addWidget(b)

        self.btn_lock.clicked.connect(self.lock_user)
        self.btn_unlock.clicked.connect(self.unlock_user)
        self.btn_sudo.clicked.connect(self.toggle_sudo)
        self.btn_delete.clicked.connect(self.delete_user)

        right.addLayout(action_row)

        # ---------------- SET PASSWORD ----------------
        pw_row = QHBoxLayout()
        self.new_password_input = QLineEdit()
        self.new_password_input.setPlaceholderText("New password")
        self.new_password_input.setEchoMode(QLineEdit.Password)
        btn_set_password = QPushButton("Set Password")
        btn_set_password.clicked.connect(self.set_password)
        pw_row.addWidget(self.new_password_input)
        pw_row.addWidget(btn_set_password)
        right.addLayout(pw_row)

        # =========================================================
        # GROUP MANAGEMENT
        # =========================================================
        group_label = QLabel("Group Management")
        group_label.setStyleSheet("font-weight: bold;")

        self.group_dropdown = QComboBox()

        btn_add_group = QPushButton("Add User to Group")
        btn_remove_group = QPushButton("Remove User from Group")

        btn_add_group.clicked.connect(self.add_group)
        btn_remove_group.clicked.connect(self.remove_group)

        group_row = QHBoxLayout()
        group_row.addWidget(self.group_dropdown)
        group_row.addWidget(btn_add_group)
        group_row.addWidget(btn_remove_group)

        right.addWidget(group_label)
        right.addLayout(group_row)

        # =========================================================
        # CREATE USER
        # =========================================================
        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("Username")

        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("Password (optional)")
        self.password_input.setEchoMode(QLineEdit.Password)

        self.shell_input = QLineEdit()
        self.shell_input.setPlaceholderText("/bin/bash")

        btn_create_user = QPushButton("Create User")
        btn_create_user.clicked.connect(self.create_user)

        right.addWidget(QLabel("Create User"))
        right.addWidget(self.username_input)
        right.addWidget(self.password_input)
        right.addWidget(self.shell_input)
        right.addWidget(btn_create_user)

        # =========================================================
        # CREATE GROUP
        # =========================================================
        self.new_group_input = QLineEdit()
        self.new_group_input.setPlaceholderText("New group name")

        btn_create_group = QPushButton("Create Group")
        btn_create_group.clicked.connect(self.create_group)

        btn_delete_group = QPushButton("Delete Group")
        btn_delete_group.clicked.connect(self.delete_group)

        create_group_row = QHBoxLayout()
        create_group_row.addWidget(self.new_group_input)
        create_group_row.addWidget(btn_create_group)
        create_group_row.addWidget(btn_delete_group)

        right.addWidget(QLabel("Create / Delete Group"))
        right.addLayout(create_group_row)

        self.dispatch_status = QLabel()
        self.dispatch_status.setStyleSheet("color: #888;")
        right.addWidget(self.dispatch_status)

        split.addLayout(right, 3)
        main.addLayout(split)

        # =========================================================
        # DATA
        # =========================================================
        self.load_hosts()

        # =========================================================
        # TIMERS
        # =========================================================
        self.host_refresh_timer = QTimer()
        self.host_refresh_timer.timeout.connect(self.load_hosts)
        self.host_refresh_timer.start(HOST_REFRESH_MS)

        self.sync_timer = QTimer()
        self.sync_timer.timeout.connect(self._poll_sync)

        bus.host_removed.connect(self.load_hosts)

    # =========================================================
    # TARGET HOSTS
    # =========================================================
    def checked_host_ids(self):
        ids = []
        for i in range(self.host_list.count()):
            item = self.host_list.item(i)
            host_id = item.data(Qt.UserRole)
            if host_id is None:
                continue  # environment header row, not a host
            if item.checkState() == Qt.Checked:
                ids.append(host_id)
        return ids

    def load_hosts(self):
        checked = set(self.checked_host_ids())

        try:
            agents = api.get_agents()
        except Exception as e:
            self.sync_status.setText(f"Could not load hosts: {e}")
            return

        try:
            environments = api.list_environments()
        except Exception:
            environments = []

        self.host_list.blockSignals(True)
        self.host_list.clear()

        # Group hosts by environment so this checklist matches the
        # grouping shown in Host Enrollment / Remote Administration.
        groups = {}
        for a in agents:
            env = a.get("environment") or ""
            groups.setdefault(env, []).append(a)

        known_envs = [e for e in environments if e in groups]
        extra_envs = sorted(e for e in groups if e and e not in environments)
        unassigned = groups.get("", [])

        for env in known_envs + extra_envs:
            self._add_host_header(env)
            for a in groups[env]:
                self._add_host_item(a, checked)

        if unassigned:
            self._add_host_header("Unassigned")
            for a in unassigned:
                self._add_host_item(a, checked)

        self.host_list.blockSignals(False)

        # Purge cached data for hosts that no longer exist (disenrolled
        # from Host Enrollment or Remote Administration) so a stale
        # user list never lingers here.
        current_ids = {a.get("host_id") for a in agents}
        stale_ids = set(self.host_data.keys()) - current_ids

        for host_id in stale_ids:
            del self.host_data[host_id]
            self.pending_sync.pop(host_id, None)

        if self.active_host_id in stale_ids:
            self.active_host_id = None
            self.active_host_label.setText("Viewing: (no host selected)")
            self.user_list.clear()
            self.selected_user = None
            self._clear_user_detail()
            self.group_dropdown.clear()

    def _add_host_header(self, text):
        item = QListWidgetItem(text.upper())
        item.setFlags(Qt.NoItemFlags)

        font = item.font()
        font.setBold(True)
        item.setFont(font)

        self.host_list.addItem(item)

    def _add_host_item(self, agent, checked):
        host_id = agent.get("host_id")
        label = f"    {agent.get('hostname') or host_id}  [{agent.get('status', '?')}]"
        item = QListWidgetItem(label)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked if host_id in checked else Qt.Unchecked)
        item.setData(Qt.UserRole, host_id)
        self.host_list.addItem(item)

    def select_all_hosts(self):
        for i in range(self.host_list.count()):
            item = self.host_list.item(i)
            if item.data(Qt.UserRole) is not None:
                item.setCheckState(Qt.Checked)

    def deselect_all_hosts(self):
        for i in range(self.host_list.count()):
            item = self.host_list.item(i)
            if item.data(Qt.UserRole) is not None:
                item.setCheckState(Qt.Unchecked)

    def on_host_selected(self, item):
        host_id = item.data(Qt.UserRole)

        if host_id is None:
            return  # environment header row, not a host

        self.active_host_id = host_id
        self.active_host_label.setText(f"Viewing: {item.text().strip()}")
        self.refresh_user_panel_from_cache()

    # =========================================================
    # SYNC (full read from enrolled hosts)
    # =========================================================
    def sync_checked_hosts(self):
        host_ids = self.checked_host_ids()

        if not host_ids:
            QMessageBox.information(self, "No hosts checked", "Check one or more hosts to sync.")
            return

        try:
            task_ids = api.sync_hosts(host_ids)
        except Exception as e:
            QMessageBox.critical(self, "Sync failed", str(e))
            return

        self.pending_sync = {h: t for h, t in task_ids.items() if t is not None}
        self.sync_total = len(self.pending_sync)
        self.sync_deadline = time.time() + SYNC_TIMEOUT_S
        self.sync_status.setText(f"Syncing 0/{self.sync_total} hosts...")
        self.sync_timer.start(SYNC_POLL_MS)

    def _poll_sync(self):
        if not self.pending_sync:
            self.sync_timer.stop()
            return

        done = []

        for host_id, task_id in list(self.pending_sync.items()):
            try:
                raw = api.get_result_by_task(host_id, task_id)
            except Exception:
                raw = None

            if raw is None:
                continue  # agent hasn't reported back yet

            done.append(host_id)

            output = api.parse_task_output(raw)
            parsed = None

            if output and output.get("returncode") == 0:
                try:
                    parsed = json.loads(output["stdout"])
                except (TypeError, ValueError, KeyError):
                    parsed = None

            if parsed is not None:
                self.host_data[host_id] = {
                    "users": parsed.get("users", []),
                    "groups": parsed.get("groups", []),
                    "sessions": parsed.get("sessions", []),
                    "synced_at": time.time(),
                    "error": None,
                }
            else:
                previous = self.host_data.get(host_id, {"users": [], "groups": [], "sessions": []})
                self.host_data[host_id] = {
                    **previous,
                    "error": (output or {}).get("stderr") or "sync failed",
                }

        for host_id in done:
            del self.pending_sync[host_id]

        synced_so_far = self.sync_total - len(self.pending_sync)
        self.sync_status.setText(f"Syncing {synced_so_far}/{self.sync_total} hosts...")

        timed_out = bool(self.sync_deadline) and time.time() > self.sync_deadline

        if not self.pending_sync or timed_out:
            self.sync_timer.stop()

            if timed_out and self.pending_sync:
                self.sync_status.setText(
                    f"Sync timed out waiting on {len(self.pending_sync)} host(s) "
                    f"(synced {synced_so_far}/{self.sync_total})."
                )
                self.pending_sync = {}
            else:
                self.sync_status.setText(f"Synced {synced_so_far}/{self.sync_total} hosts.")

            if self.active_host_id in self.host_data:
                self.refresh_user_panel_from_cache()

    def _auto_resync(self, host_ids):
        try:
            task_ids = api.sync_hosts(host_ids)
        except Exception:
            return

        new_pending = {h: t for h, t in task_ids.items() if t is not None}
        self.pending_sync.update(new_pending)
        self.sync_total += len(new_pending)
        self.sync_deadline = time.time() + SYNC_TIMEOUT_S

        if not self.sync_timer.isActive():
            self.sync_timer.start(SYNC_POLL_MS)

    # =========================================================
    # USER PANEL (driven by cached sync data for the active host)
    # =========================================================
    def refresh_user_panel_from_cache(self):
        self.user_list.clear()
        self.selected_user = None
        self._clear_user_detail()
        self.load_groups_for_active_host()

        data = self.host_data.get(self.active_host_id)

        if not data:
            self.account_info.setText("No synced data for this host yet - click Sync.")
            return

        for u in data.get("users", []):
            self.user_list.addItem(u["username"])

        if data.get("error"):
            self.account_info.setText(f"Last sync error: {data['error']}")
        else:
            synced_at = time.strftime("%H:%M:%S", time.localtime(data["synced_at"]))
            self.account_info.setText(f"Last synced {synced_at}")

    def load_groups_for_active_host(self):
        self.group_dropdown.clear()
        data = self.host_data.get(self.active_host_id)
        if data:
            for g in data.get("groups", []):
                self.group_dropdown.addItem(g["name"])

    def filter_users(self, text):
        data = self.host_data.get(self.active_host_id)
        self.user_list.clear()
        if not data:
            return
        for u in data.get("users", []):
            if text.lower() in u["username"].lower():
                self.user_list.addItem(u["username"])

    def on_select_user(self, item):
        self.selected_user = item.text()
        self.load_user_detail(self.selected_user)

    def _clear_user_detail(self):
        self.account_info.setText("")
        self.status_label.setText("")
        self.sudo_status.setText("")
        self.user_groups_label.setText("")
        self.sessions.setPlainText("")

    def load_user_detail(self, username):
        data = self.host_data.get(self.active_host_id)
        if not data:
            return

        user = next((u for u in data.get("users", []) if u["username"] == username), None)
        if not user:
            return

        self.status_label.setText(
            "Account Status: LOCKED" if user.get("locked") else "Account Status: ACTIVE"
        )
        self.sudo_status.setText("Yes" if user.get("sudo") else "No")
        self.user_groups_label.setText(
            ", ".join(user.get("groups", [])) if user.get("groups") else "None"
        )

        sessions = [s for s in data.get("sessions", []) if s.get("username") == username]
        self.sessions.setPlainText(
            "\n".join(s.get("tty", "-") for s in sessions) or "No active sessions"
        )

    # =========================================================
    # DISPATCH HELPER (queues a command on every checked host)
    # =========================================================
    def dispatch(self, command, label, auto_resync=True):
        host_ids = self.checked_host_ids()

        if not host_ids:
            QMessageBox.information(self, "No hosts checked", "Check one or more target hosts first.")
            return False

        try:
            task_ids = api.queue_command_on_hosts(host_ids, command)
        except Exception as e:
            QMessageBox.critical(self, "Dispatch failed", str(e))
            return False

        failed = [h for h, t in task_ids.items() if t is None]
        ok_count = len(task_ids) - len(failed)

        msg = f"{label}: dispatched to {ok_count}/{len(task_ids)} host(s)."
        if failed:
            msg += f" Failed to queue on: {', '.join(failed)}."
        self.dispatch_status.setText(msg)

        if auto_resync:
            QTimer.singleShot(AUTO_RESYNC_DELAY_MS, lambda: self._auto_resync(host_ids))

        return True

    # =========================================================
    # ACTIONS (all dispatched to checked hosts, never local)
    # =========================================================
    def create_user(self):
        username = self.username_input.text().strip()
        if not username:
            return

        try:
            cmd = api.cmd_create_user(
                username,
                self.password_input.text(),
                self.shell_input.text() or "/bin/bash",
            )
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        if self.dispatch(cmd, f"Create user '{username}'"):
            self.username_input.clear()
            self.password_input.clear()
            self.shell_input.clear()

    def delete_user(self):
        if not self.selected_user:
            return

        confirm = QMessageBox.question(
            self, "Confirm delete",
            f"Delete user '{self.selected_user}' on all checked hosts?",
            QMessageBox.Yes | QMessageBox.No
        )
        if confirm != QMessageBox.Yes:
            return

        self.dispatch(api.cmd_delete_user(self.selected_user), f"Delete user '{self.selected_user}'")

    def lock_user(self):
        if not self.selected_user:
            return
        self.dispatch(api.cmd_lock_user(self.selected_user), f"Lock '{self.selected_user}'")

    def unlock_user(self):
        if not self.selected_user:
            return
        self.dispatch(api.cmd_unlock_user(self.selected_user), f"Unlock '{self.selected_user}'")

    def toggle_sudo(self):
        if not self.selected_user:
            return

        data = self.host_data.get(self.active_host_id)
        current = False
        if data:
            user = next((u for u in data.get("users", []) if u["username"] == self.selected_user), None)
            current = bool(user and user.get("sudo"))

        self.dispatch(
            api.cmd_set_sudo(self.selected_user, not current),
            f"Toggle sudo for '{self.selected_user}'"
        )

    def set_password(self):
        if not self.selected_user:
            return

        password = self.new_password_input.text()
        if not password:
            return

        try:
            cmd = api.cmd_set_password(self.selected_user, password)
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        if self.dispatch(cmd, f"Set password for '{self.selected_user}'"):
            self.new_password_input.clear()

    def add_group(self):
        if not self.selected_user:
            return
        group = self.group_dropdown.currentText()
        if not group:
            return
        self.dispatch(
            api.cmd_add_user_to_group(group, self.selected_user),
            f"Add '{self.selected_user}' to '{group}'"
        )

    def remove_group(self):
        if not self.selected_user:
            return
        group = self.group_dropdown.currentText()
        if not group:
            return
        self.dispatch(
            api.cmd_remove_user_from_group(group, self.selected_user),
            f"Remove '{self.selected_user}' from '{group}'"
        )

    def create_group(self):
        name = self.new_group_input.text().strip()
        if not name:
            return

        if self.dispatch(api.cmd_create_group(name), f"Create group '{name}'"):
            self.new_group_input.clear()

    def delete_group(self):
        name = self.new_group_input.text().strip() or self.group_dropdown.currentText()
        if not name:
            QMessageBox.information(
                self, "No group", "Type a group name above (or pick one from the dropdown) first."
            )
            return

        confirm = QMessageBox.question(
            self, "Confirm delete",
            f"Delete group '{name}' on all checked hosts?",
            QMessageBox.Yes | QMessageBox.No
        )
        if confirm != QMessageBox.Yes:
            return

        if self.dispatch(api.cmd_delete_group(name), f"Delete group '{name}'"):
            self.new_group_input.clear()
