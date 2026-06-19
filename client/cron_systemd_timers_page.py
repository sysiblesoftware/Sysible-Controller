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
from client.collapsible_groups import (
    make_group_header_item, apply_collapse_state, get_collapsed_groups,
    connect_group_toggle, add_collapse_expand_buttons,
)
from client.schedule_builder import HumanScheduleBuilder

HOST_REFRESH_MS = 10000
CRON_POLL_MS = 2000


def _entry_key(entry):
    """Hashable identity for a merged-host entry (api.list_merged_hosts()) -
    agent host_ids and SSH host names live in separate namespaces, so the
    kind has to be part of the key."""
    return (entry["kind"], entry["id"])


class CronSystemdTimersPage(QWidget):
    """
    Cron & Systemd Timers against a *merged* host list (agent-enrolled
    hosts AND SSH-enrolled hosts) - view/add/remove cron entries for
    the connecting user (plus the system-wide crontab/cron.d files),
    and view/create/start/stop/enable/disable/delete systemd timers,
    all dispatched to whichever hosts are checked.

    Fifth tile under System Administration, built the same way as
    Service Management (its closest sibling - both manage systemd-
    adjacent scheduled work) so its host checklist, controls, and
    multi-host tabbed results follow the same pattern as the rest of
    System Administration.
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Cron & Systemd Timers")
        self.resize(1100, 820)

        self.cron_results = {}    # entry_key -> {label, stdout, stderr, code, pending}
        self.cron_pending = {}    # entry_key -> (entry, task_id)

        main = QVBoxLayout()
        self.setLayout(main)

        main.addLayout(make_page_header("Cron & Systemd Timers"))

        # =========================================================
        # TARGET HOSTS (agent + SSH, merged)
        # =========================================================
        hosts_box = QVBoxLayout()

        self.host_list = QListWidget()
        self.host_list.setFixedHeight(70)
        connect_group_toggle(self.host_list)

        hosts_header = QHBoxLayout()
        hosts_title = QLabel("Target Hosts (agent + SSH)")
        hosts_title.setStyleSheet("font-weight: bold;")
        hosts_header.addWidget(hosts_title)
        hosts_header.addStretch()

        btn_refresh_hosts = QPushButton("Refresh Hosts")
        btn_refresh_hosts.clicked.connect(self.load_hosts)

        btn_select_all = QPushButton("Select All")
        btn_select_all.clicked.connect(self.select_all_hosts)

        btn_deselect_all = QPushButton("Deselect All")
        btn_deselect_all.clicked.connect(self.deselect_all_hosts)

        btn_collapse_all, btn_expand_all = add_collapse_expand_buttons(self.host_list)

        hosts_header.addWidget(btn_refresh_hosts)
        hosts_header.addWidget(btn_select_all)
        hosts_header.addWidget(btn_deselect_all)
        hosts_header.addWidget(btn_collapse_all)
        hosts_header.addWidget(btn_expand_all)

        hosts_box.addLayout(hosts_header)
        hosts_box.addWidget(self.host_list)

        main.addLayout(hosts_box)

        # =========================================================
        # CRON
        # =========================================================
        main.addWidget(self._build_cron_panel())

        # =========================================================
        # SYSTEMD TIMERS
        # =========================================================
        main.addWidget(self._build_timers_panel())

        # =========================================================
        # CREATE TIMER (collapsed by default - occasional setup, same
        # reasoning as Service Management's Create / Configure panel)
        # =========================================================
        self.create_toggle = QPushButton("▸ Create Systemd Timer (click to expand)")
        self.create_toggle.setCheckable(True)
        self.create_toggle.setChecked(False)
        self.create_toggle.clicked.connect(self._toggle_create)
        main.addWidget(self.create_toggle)

        self.create_panel = self._build_create_timer_panel()
        self.create_panel.setVisible(False)
        main.addWidget(self.create_panel)

        # =========================================================
        # RESULTS (stretch factor 1 - same reasoning as Service
        # Management: claim whatever vertical space the sections above
        # don't need, instead of being squeezed to its minimum size)
        # =========================================================
        main.addWidget(self._build_results_panel(), 1)

        # =========================================================
        # DATA
        # =========================================================
        self.load_hosts()

        # =========================================================
        # TIMERS (QTimer, not to be confused with systemd timers above)
        # =========================================================
        self.host_refresh_timer = QTimer()
        self.host_refresh_timer.timeout.connect(self.load_hosts)
        self.host_refresh_timer.start(HOST_REFRESH_MS)

        self.cron_poll_timer = QTimer()
        self.cron_poll_timer.timeout.connect(self._poll_cron)

        bus.host_removed.connect(self.load_hosts)

    @staticmethod
    def _divider():
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        return line

    def _toggle_create(self, checked):
        self.create_panel.setVisible(checked)
        self.create_toggle.setText(
            "▾ Create Systemd Timer (click to collapse)" if checked
            else "▸ Create Systemd Timer (click to expand)"
        )

    def _toggle_timer_calendar(self, checked):
        self.new_timer_schedule_builder.setVisible(checked)

    # =========================================================
    # PANEL BUILDERS
    # =========================================================
    def _build_cron_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        header = QLabel("Cron Jobs")
        header.setStyleSheet("font-weight: bold;")
        layout.addWidget(header)

        hint = QLabel(
            "Add/remove apply to the connecting user's own crontab. "
            "\"List Cron Jobs\" also shows /etc/crontab and /etc/cron.d for full visibility."
        )
        theme.style_hint_label(hint)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        btn_list_row = QHBoxLayout()
        btn_list_cron = QPushButton("List Cron Jobs")
        btn_list_cron.setStyleSheet("font-weight: bold;")
        btn_list_cron.clicked.connect(self.run_list_cron_jobs)
        btn_list_row.addWidget(btn_list_cron)
        btn_list_row.addStretch()
        layout.addLayout(btn_list_row)

        schedule_label = QLabel("Schedule:")
        schedule_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(schedule_label)

        # Plain-English frequency/time picker instead of hand-written
        # cron syntax - builds the actual "*/15 * * * *"-style string
        # behind the scenes (still reachable directly via its Advanced
        # checkbox for anyone who wants to type one).
        self.cron_schedule_builder = HumanScheduleBuilder(mode="cron")
        layout.addWidget(self.cron_schedule_builder)

        add_row = QHBoxLayout()
        self.cron_command = QLineEdit()
        self.cron_command.setPlaceholderText("Command to run")
        self.cron_command.setMaximumWidth(400)
        self.cron_comment = QLineEdit()
        self.cron_comment.setPlaceholderText("Comment (optional, helps removal later)")
        self.cron_comment.setMaximumWidth(280)
        btn_add_cron = QPushButton("Add Cron Job")
        btn_add_cron.clicked.connect(self.run_add_cron_job)
        add_row.addWidget(self.cron_command, 1)
        add_row.addWidget(self.cron_comment, 1)
        add_row.addWidget(btn_add_cron)
        layout.addLayout(add_row)

        remove_row = QHBoxLayout()
        self.cron_remove_text = QLineEdit()
        self.cron_remove_text.setPlaceholderText(
            "Exact line (or unique snippet/comment) to remove - see List Cron Jobs above"
        )
        self.cron_remove_text.setMaximumWidth(420)
        btn_remove_cron = QPushButton("Remove Cron Job")
        btn_remove_cron.clicked.connect(self.run_remove_cron_job)
        remove_row.addWidget(self.cron_remove_text, 1)
        remove_row.addWidget(btn_remove_cron)
        layout.addLayout(remove_row)

        return panel

    def _build_timers_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        layout.addWidget(self._divider())

        header = QLabel("Systemd Timers")
        header.setStyleSheet("font-weight: bold;")
        layout.addWidget(header)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Timer name:"))
        self.timer_name_input = QLineEdit()
        self.timer_name_input.setPlaceholderText("e.g. nightly-backup")
        self.timer_name_input.setMaximumWidth(280)
        name_row.addWidget(self.timer_name_input, 1)
        btn_list_timers = QPushButton("List All Timers")
        btn_list_timers.clicked.connect(self.run_list_timers)
        name_row.addWidget(btn_list_timers)
        layout.addLayout(name_row)

        row1 = QHBoxLayout()
        btn_start = QPushButton("Start")
        btn_start.clicked.connect(self.run_timer_start)
        btn_stop = QPushButton("Stop")
        btn_stop.clicked.connect(self.run_timer_stop)
        btn_enable = QPushButton("Enable At Boot")
        btn_enable.clicked.connect(self.run_timer_enable)
        btn_disable = QPushButton("Disable At Boot")
        btn_disable.clicked.connect(self.run_timer_disable)
        btn_status = QPushButton("Check Status")
        btn_status.clicked.connect(self.run_timer_status)
        for b in (btn_start, btn_stop, btn_enable, btn_disable, btn_status):
            row1.addWidget(b)
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        self.timer_delete_service_too = QCheckBox("Also delete paired .service unit")
        self.timer_delete_service_too.setChecked(True)
        btn_delete = QPushButton("Delete Timer")
        btn_delete.clicked.connect(self.run_delete_timer)
        row2.addWidget(self.timer_delete_service_too)
        row2.addWidget(btn_delete)
        row2.addStretch()
        layout.addLayout(row2)

        return panel

    def _build_create_timer_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        layout.addWidget(self._divider())

        label = QLabel("Create Systemd Timer")
        label.setStyleSheet("font-weight: bold;")
        layout.addWidget(label)

        hint = QLabel(
            "Writes a paired oneshot service (runs the command below) and a timer "
            "(the schedule) - set at least one of OnCalendar / OnBootSec / OnUnitActiveSec."
        )
        theme.style_hint_label(hint)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.new_timer_name = QLineEdit()
        self.new_timer_name.setPlaceholderText("Timer name (e.g. nightly-backup)")
        self.new_timer_name.setMaximumWidth(280)

        self.new_timer_description = QLineEdit()
        self.new_timer_description.setPlaceholderText("Description (optional)")
        self.new_timer_description.setMaximumWidth(380)

        self.new_timer_exec_start = QLineEdit()
        self.new_timer_exec_start.setPlaceholderText("ExecStart command (e.g. /usr/local/bin/backup.sh)")
        self.new_timer_exec_start.setMaximumWidth(420)

        self.new_timer_on_boot_sec = QLineEdit()
        self.new_timer_on_boot_sec.setPlaceholderText("OnBootSec (e.g. 15min, optional)")
        self.new_timer_on_boot_sec.setMaximumWidth(140)

        self.new_timer_on_unit_active_sec = QLineEdit()
        self.new_timer_on_unit_active_sec.setPlaceholderText("OnUnitActiveSec (e.g. 1h, optional)")
        self.new_timer_on_unit_active_sec.setMaximumWidth(140)

        self.new_timer_user = QLineEdit()
        self.new_timer_user.setPlaceholderText("Run as user (default root)")
        self.new_timer_user.setMaximumWidth(160)

        self.new_timer_enable_now = QCheckBox("Enable + start immediately after creating")
        self.new_timer_enable_now.setChecked(True)

        for w in (
            self.new_timer_name, self.new_timer_description,
            self.new_timer_exec_start,
        ):
            layout.addWidget(w)

        # Plain-English frequency/time picker instead of hand-written
        # OnCalendar syntax - same builder widget as the cron section
        # above, in "calendar" mode. Calendar scheduling is optional
        # here (OnBootSec/OnUnitActiveSec below can drive the timer
        # instead), so it's behind its own checkbox.
        self.new_timer_use_calendar = QCheckBox("Run on a calendar schedule")
        self.new_timer_use_calendar.setChecked(True)
        self.new_timer_use_calendar.toggled.connect(self._toggle_timer_calendar)
        layout.addWidget(self.new_timer_use_calendar)

        self.new_timer_schedule_builder = HumanScheduleBuilder(mode="calendar")
        layout.addWidget(self.new_timer_schedule_builder)

        for w in (self.new_timer_on_boot_sec, self.new_timer_on_unit_active_sec, self.new_timer_user):
            layout.addWidget(w)
        layout.addWidget(self.new_timer_enable_now)

        btn_create = QPushButton("Create Timer")
        btn_create.clicked.connect(self.run_create_timer)
        layout.addWidget(btn_create)

        return panel

    def _build_results_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        self.cron_status = QLabel("Pick an action above to run it on all checked hosts.")
        self.cron_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        layout.addWidget(self.cron_status)

        # One tab per host instead of a host-list-plus-single-output-panel -
        # same fix as System Health & Logs / Service Management, for the
        # same reason: a shared panel only ever shows whichever host was
        # last clicked.
        self.cron_tabs = QTabWidget()
        self.cron_tabs.setTabsClosable(True)
        self.cron_tabs.tabCloseRequested.connect(self._close_cron_tab)
        layout.addWidget(self.cron_tabs)
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
            self.cron_status.setStyleSheet(f"color: {STATUS_ERROR_COLOR};")
            self.cron_status.setText(f"Could not load hosts: {e}")
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
        """A fixed 110px box looked fine with a handful of hosts but
        left a large, obviously-unused blank gap below the rows when
        there were only one or two - size to the actual *visible* row
        count instead (collapsed groups should shrink the box, not
        just hide their rows inside an unchanged-size box), capped so
        a long host list still scrolls rather than taking over the
        page."""
        visible = sum(
            1 for i in range(self.host_list.count())
            if not self.host_list.item(i).isHidden()
        )
        row_h = self.host_list.sizeHintForRow(0) if visible else 22
        if row_h <= 0:
            row_h = 22
        height = row_h * min(visible, 6) + 2 * self.host_list.frameWidth() + 6
        self.host_list.setFixedHeight(max(48, min(height, 160)))

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
    # TIMER NAME HELPER
    # =========================================================
    def _timer_name(self):
        name = self.timer_name_input.text().strip()
        if not name:
            QMessageBox.information(self, "No timer name", "Type a timer name above first.")
            return None
        return name

    # =========================================================
    # CRON ACTIONS
    # =========================================================
    def run_list_cron_jobs(self):
        self._run_cron_command(api.cmd_list_cron_jobs(), "Cron Jobs")

    def run_add_cron_job(self):
        try:
            cmd = api.cmd_add_cron_job(
                self.cron_schedule_builder.value(),
                self.cron_command.text(),
                self.cron_comment.text(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid cron job", str(e))
            return
        self._run_cron_command(cmd, "Add Cron Job")

    def run_remove_cron_job(self):
        try:
            cmd = api.cmd_remove_cron_job(self.cron_remove_text.text())
        except ValueError as e:
            QMessageBox.warning(self, "Nothing to remove", str(e))
            return
        self._run_cron_command(cmd, "Remove Cron Job")

    # =========================================================
    # TIMER ACTIONS
    # =========================================================
    def run_list_timers(self):
        self._run_cron_command(api.cmd_list_timers(), "All Timers")

    def run_timer_start(self):
        name = self._timer_name()
        if name:
            self._run_cron_command(api.cmd_timer_start(name), f"Start timer '{name}'")

    def run_timer_stop(self):
        name = self._timer_name()
        if name:
            self._run_cron_command(api.cmd_timer_stop(name), f"Stop timer '{name}'")

    def run_timer_enable(self):
        name = self._timer_name()
        if name:
            self._run_cron_command(api.cmd_timer_enable(name), f"Enable timer '{name}' at boot")

    def run_timer_disable(self):
        name = self._timer_name()
        if name:
            self._run_cron_command(api.cmd_timer_disable(name), f"Disable timer '{name}' at boot")

    def run_timer_status(self):
        name = self._timer_name()
        if name:
            self._run_cron_command(api.cmd_timer_status(name), f"Status of timer '{name}'")

    def run_delete_timer(self):
        name = self._timer_name()
        if not name:
            return
        try:
            cmd = api.cmd_delete_timer(name, self.timer_delete_service_too.isChecked())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid timer name", str(e))
            return
        self._run_cron_command(cmd, f"Delete timer '{name}'")

    def run_create_timer(self):
        name = self.new_timer_name.text().strip()

        if not name:
            QMessageBox.warning(self, "Missing field", "Timer name is required.")
            return

        on_calendar = (
            self.new_timer_schedule_builder.value()
            if self.new_timer_use_calendar.isChecked() else ""
        )

        try:
            cmd = api.cmd_create_systemd_timer(
                name,
                exec_start=self.new_timer_exec_start.text(),
                on_calendar=on_calendar,
                on_boot_sec=self.new_timer_on_boot_sec.text(),
                on_unit_active_sec=self.new_timer_on_unit_active_sec.text(),
                description=self.new_timer_description.text(),
                run_as_user=self.new_timer_user.text() or "root",
                enable_now=self.new_timer_enable_now.isChecked(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid timer definition", str(e))
            return

        self._run_cron_command(cmd, f"Create timer '{name}'")

    # =========================================================
    # DISPATCH + RESULTS (same pattern as System Health & Logs /
    # Service Management)
    # =========================================================
    def _run_cron_command(self, command, label):
        entries = self.checked_entries()

        if not entries:
            QMessageBox.information(self, "No hosts checked", "Check one or more target hosts first.")
            return

        self.cron_results = {}
        self.cron_pending = {}
        self.cron_tabs.clear()

        for entry in entries:
            key = _entry_key(entry)
            result = api.run_on_entry(entry, command)

            if result["sync"]:
                self.cron_results[key] = {
                    "label": entry["label"],
                    "stdout": result["stdout"],
                    "stderr": result["stderr"] or result["error"] or "",
                    "code": result["code"],
                    "pending": False,
                }
            elif result["error"]:
                self.cron_results[key] = {
                    "label": entry["label"], "stdout": "", "stderr": result["error"],
                    "code": None, "pending": False,
                }
            else:
                self.cron_results[key] = {
                    "label": entry["label"], "stdout": "", "stderr": "",
                    "code": None, "pending": True,
                }
                self.cron_pending[key] = (entry, result["task_id"])

            self._add_cron_tab(key)

        self.cron_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        self.cron_status.setText(f"Running '{label}' on {len(entries)} host(s)...")

        if self.cron_tabs.count() > 0:
            self.cron_tabs.setCurrentIndex(0)

        if self.cron_pending:
            self.cron_poll_timer.start(CRON_POLL_MS)
        else:
            self.cron_status.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
            self.cron_status.setText(f"'{label}' complete.")

    def _status_text(self, data):
        if data["pending"]:
            return "pending..."
        return "ok" if not data["stderr"] else "error"

    def _render_result(self, text_edit, data):
        if data["pending"]:
            text_edit.setPlainText("Waiting for this agent host to report back...")
            return

        if data["stderr"] and not data["stdout"]:
            text_edit.setPlainText(f"ERROR:\n{data['stderr']}")
        else:
            text = data["stdout"]
            if data["stderr"]:
                text += f"\n\n--- stderr ---\n{data['stderr']}"
            text_edit.setPlainText(text)

    def _close_cron_tab(self, index):
        bar = self.cron_tabs.tabBar()
        key = bar.tabData(index)
        self.cron_tabs.removeTab(index)
        self.cron_results.pop(key, None)
        self.cron_pending.pop(key, None)

    def _add_cron_tab(self, key):
        data = self.cron_results.get(key)
        if not data:
            return
        status = self._status_text(data)

        text_edit = QTextEdit()
        text_edit.setReadOnly(True)
        text_edit.setStyleSheet("font-family: monospace;")
        self._render_result(text_edit, data)

        idx = self.cron_tabs.addTab(text_edit, f"{data['label']}  [{status}]")
        self.cron_tabs.tabBar().setTabData(idx, key)

    def _refresh_cron_tab(self, key):
        bar = self.cron_tabs.tabBar()
        for i in range(self.cron_tabs.count()):
            if bar.tabData(i) != key:
                continue
            data = self.cron_results.get(key)
            if data:
                status = self._status_text(data)
                self.cron_tabs.setTabText(i, f"{data['label']}  [{status}]")
                self._render_result(self.cron_tabs.widget(i), data)
            return

    def _poll_cron(self):
        if not self.cron_pending:
            self.cron_poll_timer.stop()
            return

        done = []

        for key, (entry, task_id) in list(self.cron_pending.items()):
            result = api.poll_entry_result(entry, task_id)
            if result is None:
                continue

            self.cron_results[key] = {
                "label": entry["label"],
                "stdout": result["stdout"],
                "stderr": result["stderr"] or result["error"] or "",
                "code": result["code"],
                "pending": False,
            }
            self._refresh_cron_tab(key)
            done.append(key)

        for key in done:
            del self.cron_pending[key]

        if not self.cron_pending:
            self.cron_poll_timer.stop()
            self.cron_status.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
            self.cron_status.setText("All hosts reported back.")
