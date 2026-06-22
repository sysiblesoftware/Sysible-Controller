import html

from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout,
    QListWidget, QListWidgetItem, QLabel, QPushButton,
    QLineEdit, QTextEdit, QMessageBox, QCheckBox, QFrame, QTabWidget,
)
from PySide6.QtCore import Qt, QTimer

from client import api
from client import theme
from client.events import bus
from client.theme import STATUS_NEUTRAL_COLOR, STATUS_SUCCESS_COLOR, STATUS_ERROR_COLOR
from client.branding import make_page_header
from client.tab_sizing import shrink_tabwidget_to_current_page
from client.collapsible_groups import (
    make_group_header_item, apply_collapse_state, get_collapsed_groups,
    connect_group_toggle, add_collapse_expand_buttons,
)
from client.host_panel import build_host_panel

HOST_REFRESH_MS = 10000
SERVICE_POLL_MS = 2000


def _entry_key(entry):
    """Hashable identity for a merged-host entry (api.list_merged_hosts()) -
    agent host_ids and SSH host names live in separate namespaces, so the
    kind has to be part of the key."""
    return (entry["kind"], entry["id"])


class ServiceManagementPage(QWidget):
    """
    Service Management against a *merged* host list (agent-enrolled
    hosts AND SSH-enrolled hosts) - start/stop/restart/reload, enable/
    disable at boot, status checks, troubleshooting, log viewing, and
    creating/configuring systemd units, all dispatched to whichever
    hosts are checked.

    Third tile under System Administration, alongside User & Group
    Administration and System Health & Logs - split out the same way
    those were, so its host checklist and controls get the whole
    window instead of fighting other tools for space.
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Service Management")
        self.resize(1350, 820)

        self.service_results = {}    # entry_key -> {label, stdout, stderr, code, pending}
        self.service_pending = {}    # entry_key -> (entry, task_id)
        self.last_command_label = None
        self.installed_services = []   # parsed from the most recent "List Installed Services" run

        main = QVBoxLayout()
        self.setLayout(main)

        main.addLayout(make_page_header("Service Management"))

        # =========================================================
        # BODY: Target Hosts as a full-height left column (#352),
        # everything else in the right-hand content column.
        # =========================================================
        body = QHBoxLayout()

        self.host_list = QListWidget()
        connect_group_toggle(self.host_list)

        btn_refresh_hosts = QPushButton("Refresh Hosts")
        btn_refresh_hosts.clicked.connect(self.load_hosts)

        btn_select_all = QPushButton("Select All")
        btn_select_all.clicked.connect(self.select_all_hosts)

        btn_deselect_all = QPushButton("Deselect All")
        btn_deselect_all.clicked.connect(self.deselect_all_hosts)

        btn_collapse_all, btn_expand_all = add_collapse_expand_buttons(self.host_list)

        body.addWidget(build_host_panel(
            "Target Hosts (agent-managed)", self.host_list,
            [[btn_refresh_hosts, btn_select_all, btn_deselect_all],
             [btn_collapse_all, btn_expand_all]],
        ))

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)

        # ---------------------------------------------------------
        # SERVICE NAME + ACTIONS
        # ---------------------------------------------------------
        content_layout.addWidget(self._build_actions_panel())

        # ---------------------------------------------------------
        # CREATE / CONFIGURE (collapsed by default - these are
        # occasional setup actions, not the day-to-day start/stop/
        # status checks above, so they shouldn't permanently eat into
        # the vertical space the results panel below needs to actually
        # be readable)
        # ---------------------------------------------------------
        self.advanced_toggle = QPushButton("▸ Create / Configure Service (click to expand)")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setChecked(False)
        self.advanced_toggle.clicked.connect(self._toggle_advanced)
        content_layout.addWidget(self.advanced_toggle)

        self.advanced_panel = QWidget()
        advanced_layout = QVBoxLayout(self.advanced_panel)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        advanced_layout.addWidget(self._build_create_panel())
        advanced_layout.addWidget(self._build_dependencies_panel())
        self.advanced_panel.setVisible(False)
        content_layout.addWidget(self.advanced_panel)

        # ---------------------------------------------------------
        # RESULTS
        # Given stretch factor 1 - the only stretchy widget in this
        # layout - so it claims all vertical space the sections above
        # don't need, instead of being squeezed down to its minimum
        # size like before. This is the actual fix for "the list of
        # services is too small to use": there was nothing wrong with
        # the widget itself, it just never got given any room to grow
        # into.
        # ---------------------------------------------------------
        content_layout.addWidget(self._build_results_panel(), 1)

        body.addWidget(content, 1)
        main.addLayout(body, 1)

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

        self.service_poll_timer = QTimer()
        self.service_poll_timer.timeout.connect(self._poll_service)

        bus.host_removed.connect(self.load_hosts)

    @staticmethod
    def _divider():
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        return line

    def _toggle_advanced(self, checked):
        self.advanced_panel.setVisible(checked)
        self.advanced_toggle.setText(
            "▾ Create / Configure Service (click to collapse)" if checked
            else "▸ Create / Configure Service (click to expand)"
        )

    # =========================================================
    # PANEL BUILDERS
    # =========================================================
    def _build_actions_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Service name:"))
        self.service_name_input = QLineEdit()
        self.service_name_input.setPlaceholderText(
            "e.g. nginx  (also the search term / filter for the list)"
        )
        self.service_name_input.textChanged.connect(self.filter_installed_services)
        name_row.addWidget(self.service_name_input, 1)
        btn_list_services = QPushButton("List Installed Services")
        btn_list_services.clicked.connect(self.run_list_services)
        name_row.addWidget(btn_list_services)
        layout.addLayout(name_row)

        # Services list to the LEFT of the action buttons (rather than
        # stacked above them), so the buttons can be grouped under headings
        # without eating the vertical space the results panel needs. The
        # window has plenty of width to spend on this.
        split = QHBoxLayout()

        left_col = QVBoxLayout()
        left_col.setContentsMargins(0, 0, 0, 0)
        list_label = QLabel("Installed services (click to fill the field):")
        theme.style_hint_label(list_label)
        left_col.addWidget(list_label)
        self.installed_services_list = QListWidget()
        self.installed_services_list.setMinimumHeight(150)
        self.installed_services_list.itemClicked.connect(self._pick_installed_service)
        left_col.addWidget(self.installed_services_list, 1)
        split.addLayout(left_col, 2)

        def _subheading(text):
            lbl = QLabel(text)
            lbl.setStyleSheet(f"font-weight: bold; color: {STATUS_NEUTRAL_COLOR};")
            return lbl

        def _btn(text, slot, bold=False):
            b = QPushButton(text)
            if bold:
                b.setStyleSheet("font-weight: bold;")
            b.clicked.connect(slot)
            return b

        btn_col = QVBoxLayout()
        btn_col.setContentsMargins(0, 0, 0, 0)

        btn_col.addWidget(_subheading("Service control"))
        ctrl_row = QHBoxLayout()
        for b in (_btn("Start", self.run_start), _btn("Stop", self.run_stop),
                  _btn("Restart", self.run_restart), _btn("Reload", self.run_reload)):
            ctrl_row.addWidget(b)
        btn_col.addLayout(ctrl_row)

        btn_col.addWidget(_subheading("Boot & status"))
        boot_row = QHBoxLayout()
        for b in (_btn("Enable At Boot", self.run_enable), _btn("Disable At Boot", self.run_disable),
                  _btn("Check Status", self.run_status), _btn("View Logs", self.run_logs)):
            boot_row.addWidget(b)
        btn_col.addLayout(boot_row)

        btn_col.addWidget(_subheading("Diagnostics"))
        diag_row = QHBoxLayout()
        for b in (_btn("Troubleshoot This Service", self.run_troubleshoot, bold=True),
                  _btn("Troubleshoot All Failed", self.run_troubleshoot_failed),
                  _btn("View Dependencies", self.run_dependencies)):
            diag_row.addWidget(b)
        btn_col.addLayout(diag_row)
        btn_col.addStretch()

        split.addLayout(btn_col, 3)
        layout.addLayout(split)

        return panel

    def _build_create_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        layout.addWidget(self._divider())

        label = QLabel("Create Custom Systemd Service")
        label.setStyleSheet("font-weight: bold;")
        layout.addWidget(label)

        self.new_service_name = QLineEdit()
        self.new_service_name.setPlaceholderText("Service name (e.g. my-app)")
        self.new_service_name.setMaximumWidth(280)

        self.new_service_description = QLineEdit()
        self.new_service_description.setPlaceholderText("Description (optional)")
        self.new_service_description.setMaximumWidth(380)

        self.new_service_exec_start = QLineEdit()
        self.new_service_exec_start.setPlaceholderText("ExecStart command (e.g. /usr/bin/myapp --flag)")
        self.new_service_exec_start.setMaximumWidth(480)

        self.new_service_workdir = QLineEdit()
        self.new_service_workdir.setPlaceholderText("Working directory (optional)")
        self.new_service_workdir.setMaximumWidth(380)

        self.new_service_user = QLineEdit()
        self.new_service_user.setPlaceholderText("Run as user (default root)")
        self.new_service_user.setMaximumWidth(220)

        self.new_service_restart = QLineEdit()
        self.new_service_restart.setPlaceholderText("Restart policy (default on-failure)")
        self.new_service_restart.setMaximumWidth(220)

        self.new_service_after = QLineEdit()
        self.new_service_after.setPlaceholderText("After (default network.target)")
        self.new_service_after.setText("network.target")
        self.new_service_after.setMaximumWidth(280)

        self.new_service_enable_now = QCheckBox("Enable + start immediately after creating")
        self.new_service_enable_now.setChecked(True)

        for w in (
            self.new_service_name, self.new_service_description,
            self.new_service_exec_start, self.new_service_workdir,
            self.new_service_user, self.new_service_restart,
            self.new_service_after,
        ):
            layout.addWidget(w)
        layout.addWidget(self.new_service_enable_now)

        btn_create = QPushButton("Create Service")
        btn_create.clicked.connect(self.run_create_service)
        layout.addWidget(btn_create)

        return panel

    def _build_dependencies_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        layout.addWidget(self._divider())

        label = QLabel("Configure Dependencies (for the service named above)")
        label.setStyleSheet("font-weight: bold;")
        layout.addWidget(label)

        hint = QLabel(
            "Adds an override drop-in (systemd's standard approach, not an "
            "edit to the original unit file) setting any of these that are filled in."
        )
        theme.style_hint_label(hint)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.dep_after = QLineEdit()
        self.dep_after.setPlaceholderText("After= (space-separated unit names)")
        self.dep_after.setMaximumWidth(420)

        self.dep_requires = QLineEdit()
        self.dep_requires.setPlaceholderText("Requires= (space-separated unit names)")
        self.dep_requires.setMaximumWidth(420)

        self.dep_wants = QLineEdit()
        self.dep_wants.setPlaceholderText("Wants= (space-separated unit names)")
        self.dep_wants.setMaximumWidth(420)

        for w in (self.dep_after, self.dep_requires, self.dep_wants):
            layout.addWidget(w)

        btn_save_deps = QPushButton("Save Dependencies")
        btn_save_deps.clicked.connect(self.run_set_dependencies)
        layout.addWidget(btn_save_deps)

        return panel

    def _build_results_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        self.service_status = QLabel("Pick an action above to run it on all checked hosts.")
        self.service_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        layout.addWidget(self.service_status)

        # One tab per host instead of a host-list-plus-single-output-panel -
        # same fix as System Health & Logs, for the same reason: a shared
        # panel only ever shows whichever host was last clicked, which is
        # the "can't see two hosts' results at once" problem.
        self.service_tabs = QTabWidget()
        self.service_tabs.setTabsClosable(True)
        self.service_tabs.tabCloseRequested.connect(self._close_service_tab)
        self.service_tabs.currentChanged.connect(self.on_service_tab_changed)
        shrink_tabwidget_to_current_page(self.service_tabs)
        layout.addWidget(self.service_tabs)
        return panel

    # =========================================================
    # TARGET HOSTS
    # =========================================================
    def checked_entries(self):
        entries = []
        for i in range(self.host_list.count()):
            item = self.host_list.item(i)
            entry = item.data(Qt.UserRole)
            if entry is None:
                continue  # environment header row, not a host
            if item.checkState() == Qt.Checked:
                entries.append(entry)
        return entries

    def load_hosts(self):
        checked = {_entry_key(e) for e in self.checked_entries()}

        try:
            entries = api.list_merged_hosts()
        except Exception as e:
            self.service_status.setStyleSheet(f"color: {STATUS_ERROR_COLOR};")
            self.service_status.setText(f"Could not load hosts: {e}")
            return

        try:
            environments = api.list_environments()
        except Exception:
            environments = []

        self._collapsed_envs = get_collapsed_groups(self.host_list)

        self.host_list.blockSignals(True)
        self.host_list.clear()

        groups = {}
        for e in entries:
            env = e.get("environment") or ""
            groups.setdefault(env, []).append(e)

        known_envs = [e for e in environments if e in groups]
        extra_envs = sorted(e for e in groups if e and e not in environments)
        unassigned = groups.get("", [])

        for env in known_envs + extra_envs:
            self._add_host_header(env)
            for e in groups[env]:
                self._add_host_item(e, checked)

        if unassigned:
            self._add_host_header("Unassigned")
            for e in unassigned:
                self._add_host_item(e, checked)

        apply_collapse_state(self.host_list)
        self.host_list.blockSignals(False)
        self._fit_host_list_height()

    def _add_host_header(self, text):
        item = make_group_header_item(text, collapsed=text in self._collapsed_envs)
        self.host_list.addItem(item)

    def _add_host_item(self, entry, checked):
        label = f"    {entry['label']}  [{entry['type_text']}]"
        item = QListWidgetItem(label)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked if _entry_key(entry) in checked else Qt.Unchecked)
        item.setData(Qt.UserRole, entry)
        self.host_list.addItem(item)

    def _fit_host_list_height(self):
        """No-op: the host list now lives in a full-height left column
        (see #352, client/host_panel.py) instead of a short horizontal
        strip, so it always expands to fill the available vertical
        space instead of being capped to a handful of rows. Kept as a
        method (rather than removing call sites) so existing
        load_hosts() calls don't need to change."""
        pass

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

    # =========================================================
    # SERVICE NAME HELPER
    # =========================================================
    def _service_name(self):
        name = self.service_name_input.text().strip()
        if not name:
            QMessageBox.information(self, "No service name", "Type a service name above first.")
            return None
        return name

    # =========================================================
    # ACTIONS
    # =========================================================
    def run_list_services(self):
        self._run_service_command(api.cmd_list_services(), "Installed Services")

    def run_start(self):
        name = self._service_name()
        if name:
            self._run_service_command(api.cmd_service_start(name), f"Start '{name}'")

    def run_stop(self):
        name = self._service_name()
        if name:
            self._run_service_command(api.cmd_service_stop(name), f"Stop '{name}'")

    def run_restart(self):
        name = self._service_name()
        if name:
            self._run_service_command(api.cmd_service_restart(name), f"Restart '{name}'")

    def run_reload(self):
        name = self._service_name()
        if name:
            self._run_service_command(api.cmd_service_reload(name), f"Reload '{name}'")

    def run_enable(self):
        name = self._service_name()
        if name:
            self._run_service_command(api.cmd_service_enable(name), f"Enable '{name}' at boot")

    def run_disable(self):
        name = self._service_name()
        if name:
            self._run_service_command(api.cmd_service_disable(name), f"Disable '{name}' at boot")

    def run_status(self):
        name = self._service_name()
        if name:
            self._run_service_command(api.cmd_service_status(name), f"Status of '{name}'")

    def run_logs(self):
        name = self._service_name()
        if name:
            self._run_service_command(api.cmd_service_logs(name), f"Logs for '{name}'")

    def run_troubleshoot(self):
        name = self._service_name()
        if name:
            self._run_service_command(api.cmd_troubleshoot_service(name), f"Troubleshoot '{name}'")

    def run_troubleshoot_failed(self):
        self._run_service_command(api.cmd_failed_services(), "Failed Services")

    def run_dependencies(self):
        name = self._service_name()
        if name:
            self._run_service_command(api.cmd_service_dependencies(name), f"Dependencies of '{name}'")

    def run_create_service(self):
        name = self.new_service_name.text().strip()

        if not name:
            QMessageBox.warning(self, "Missing field", "Service name is required.")
            return

        try:
            cmd = api.cmd_create_systemd_service(
                name,
                description=self.new_service_description.text(),
                exec_start=self.new_service_exec_start.text(),
                working_directory=self.new_service_workdir.text(),
                run_as_user=self.new_service_user.text() or "root",
                restart_policy=self.new_service_restart.text() or "on-failure",
                after=self.new_service_after.text() or "network.target",
                enable_now=self.new_service_enable_now.isChecked(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid service definition", str(e))
            return

        self._run_service_command(cmd, f"Create service '{name}'")

    def run_set_dependencies(self):
        name = self._service_name()
        if not name:
            return

        try:
            cmd = api.cmd_set_service_dependencies(
                name,
                after=self.dep_after.text(),
                requires=self.dep_requires.text(),
                wants=self.dep_wants.text(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Nothing to save", str(e))
            return

        self._run_service_command(cmd, f"Set dependencies for '{name}'")

    # =========================================================
    # DISPATCH + RESULTS (same pattern as System Health & Logs)
    # =========================================================
    def _run_service_command(self, command, label):
        entries = self.checked_entries()

        if not entries:
            QMessageBox.information(self, "No hosts checked", "Check one or more target hosts first.")
            return

        self.last_command_label = label
        self.service_results = {}
        self.service_pending = {}
        self.service_tabs.clear()

        # NOTE: the installed-services picker is deliberately NOT cleared
        # here. It's a convenience list for filling the service name field,
        # not tied to the current output - wiping it on every Start/Stop/...
        # meant the service you just picked vanished and had to be looked up
        # again. It's only replaced when "List Installed Services" reruns.

        for entry in entries:
            key = _entry_key(entry)
            result = api.run_on_entry(entry, command)

            if result["sync"]:
                self.service_results[key] = {
                    "label": entry["label"],
                    "stdout": result["stdout"],
                    "stderr": result["stderr"] or result["error"] or "",
                    "code": result["code"],
                    "pending": False,
                }
            elif result["error"]:
                self.service_results[key] = {
                    "label": entry["label"], "stdout": "", "stderr": result["error"],
                    "code": None, "pending": False,
                }
            else:
                self.service_results[key] = {
                    "label": entry["label"], "stdout": "", "stderr": "",
                    "code": None, "pending": True,
                }
                self.service_pending[key] = (entry, result["task_id"])

            self._add_service_tab(key)

        self.service_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        self.service_status.setText(f"Running '{label}' on {len(entries)} host(s)...")

        if self.service_tabs.count() > 0:
            self.service_tabs.setCurrentIndex(0)
            self.on_service_tab_changed(0)

        if self.service_pending:
            self.service_poll_timer.start(SERVICE_POLL_MS)
        else:
            self.service_status.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
            self.service_status.setText(f"'{label}' complete.")

    def _status_text(self, data):
        if data["pending"]:
            return "pending..."
        return "ok" if not data["stderr"] else "error"

    def _render_service_result(self, text_edit, data):
        if data["pending"]:
            text_edit.setPlainText("Waiting for this agent host to report back...")
            return

        # A coloured banner makes the outcome obvious even for the silent
        # actions (start/stop/enable/...), which on their own print nothing
        # on success. Success is judged by exit code where we have one.
        code = data["code"]
        label = self.last_command_label or "Action"
        if code is not None:
            failed = code != 0
        else:
            failed = bool(data["stderr"]) and not data["stdout"]

        bg = STATUS_ERROR_COLOR if failed else STATUS_SUCCESS_COLOR
        headline = f"{'✗' if failed else '✓'} {label}{' failed' if failed else ' complete'}"
        if code is not None:
            headline += f" (exit {code})"
        banner = (
            f'<div style="background-color:{bg}; color:#ffffff; font-weight:bold; '
            f'padding:5px 10px; border-radius:4px; margin:0 0 6px 0;">'
            f'{html.escape(headline)}</div>'
        )

        text = data["stdout"]
        if data["stderr"]:
            text += f"\n\n--- stderr ---\n{data['stderr']}"
        body = (
            f'<pre style="font-family:monospace; white-space:pre-wrap; margin:0;">'
            f'{html.escape(text)}</pre>'
        )
        text_edit.setHtml(banner + body)

    def _close_service_tab(self, index):
        bar = self.service_tabs.tabBar()
        key = bar.tabData(index)
        self.service_tabs.removeTab(index)
        self.service_results.pop(key, None)
        self.service_pending.pop(key, None)

    def _add_service_tab(self, key):
        data = self.service_results.get(key)
        if not data:
            return
        status = self._status_text(data)

        text_edit = QTextEdit()
        text_edit.setReadOnly(True)
        text_edit.setStyleSheet("font-family: monospace;")
        self._render_service_result(text_edit, data)

        idx = self.service_tabs.addTab(text_edit, f"{data['label']}  [{status}]")
        self.service_tabs.tabBar().setTabData(idx, key)

    def _refresh_service_tab(self, key):
        bar = self.service_tabs.tabBar()
        for i in range(self.service_tabs.count()):
            if bar.tabData(i) != key:
                continue
            data = self.service_results.get(key)
            if data:
                status = self._status_text(data)
                self.service_tabs.setTabText(i, f"{data['label']}  [{status}]")
                self._render_service_result(self.service_tabs.widget(i), data)
                if self.last_command_label == "Installed Services" and i == self.service_tabs.currentIndex():
                    self._populate_installed_services(data["stdout"])
            return

    def _poll_service(self):
        if not self.service_pending:
            self.service_poll_timer.stop()
            return

        done = []

        for key, (entry, task_id) in list(self.service_pending.items()):
            result = api.poll_entry_result(entry, task_id)
            if result is None:
                continue

            self.service_results[key] = {
                "label": entry["label"],
                "stdout": result["stdout"],
                "stderr": result["stderr"] or result["error"] or "",
                "code": result["code"],
                "pending": False,
            }
            self._refresh_service_tab(key)
            done.append(key)

        for key in done:
            del self.service_pending[key]

        if not self.service_pending:
            self.service_poll_timer.stop()
            self.service_status.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
            self.service_status.setText("All hosts reported back.")

    def on_service_tab_changed(self, index):
        """Switching tabs no longer needs to render anything - every
        tab's QTextEdit is already populated when its result lands -
        but the Installed Services picker only makes sense for whichever
        host's tab is actually in front, so that still has to follow the
        active tab."""
        if index < 0:
            return
        key = self.service_tabs.tabBar().tabData(index)
        data = self.service_results.get(key)
        if not data or data["pending"]:
            return
        if self.last_command_label == "Installed Services":
            self._populate_installed_services(data["stdout"])

    # =========================================================
    # INSTALLED SERVICES SEARCH (filterable view of the most recent
    # "List Installed Services" result for whichever host is selected
    # above - same pattern as User & Group Administration's user
    # search/filter_users)
    # =========================================================
    def _populate_installed_services(self, stdout):
        names = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
        if names == ["systemctl not available on this host"]:
            names = []
        self.installed_services = sorted(set(names))
        self._render_installed_services(self.service_name_input.text())

    def _render_installed_services(self, text):
        self.installed_services_list.clear()
        text = (text or "").lower()
        for name in self.installed_services:
            if text in name.lower():
                self.installed_services_list.addItem(name)

    def filter_installed_services(self, text):
        self._render_installed_services(text)

    def _pick_installed_service(self, item):
        self.service_name_input.setText(item.text())
