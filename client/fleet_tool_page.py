"""Shared base for the System Administration "fleet action" tools.

Captures the structure every one of them repeats: a full-height left
column of checkable target hosts (agent-managed, grouped by environment),
a content area whose action controls a subclass supplies, and a results
panel that opens one tab per host with a green/red success/fail banner.

Subclasses implement build_action_tabs() (returning the tabbed action
controls) and call self.run_command(command, label) from their button
handlers. Use self.group(title) to build titled section boxes.
"""
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QListWidget,
    QListWidgetItem, QTextEdit, QTabWidget, QGroupBox, QMessageBox,
)
from PySide6.QtCore import Qt, QTimer

from client import api
from client import result_banner
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
POLL_MS = 2000


def _entry_key(entry):
    return (entry["kind"], entry["id"])


class FleetToolPage(QWidget):
    def __init__(self, title, width=1350, height=820):
        super().__init__()
        self.setWindowTitle(title)
        self.resize(width, height)

        self.results = {}
        self.pending = {}
        self.last_command_label = None
        self._collapsed_envs = set()

        main = QVBoxLayout(self)
        main.addLayout(make_page_header(title))

        body = QHBoxLayout()

        self.host_list = QListWidget()
        connect_group_toggle(self.host_list)
        btn_refresh = QPushButton("Refresh Hosts")
        btn_refresh.clicked.connect(self.load_hosts)
        btn_sel = QPushButton("Select All")
        btn_sel.clicked.connect(self.select_all_hosts)
        btn_desel = QPushButton("Deselect All")
        btn_desel.clicked.connect(self.deselect_all_hosts)
        btn_col, btn_exp = add_collapse_expand_buttons(self.host_list)
        body.addWidget(build_host_panel(
            "Target Hosts (agent-managed)", self.host_list,
            [[btn_refresh, btn_sel, btn_desel], [btn_col, btn_exp]],
        ))

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.addWidget(self.build_action_tabs())
        content_layout.addWidget(self._build_results_panel(), 1)
        body.addWidget(content, 1)
        main.addLayout(body, 1)

        self.load_hosts()

        self.host_refresh_timer = QTimer()
        self.host_refresh_timer.timeout.connect(self.load_hosts)
        self.host_refresh_timer.start(HOST_REFRESH_MS)
        self.poll_timer = QTimer()
        self.poll_timer.timeout.connect(self._poll)
        bus.host_removed.connect(self.load_hosts)

    # ---- subclasses override ----
    def build_action_tabs(self):
        raise NotImplementedError

    @staticmethod
    def group(title):
        box = QGroupBox(title)
        return box, QVBoxLayout(box)

    # ---- results panel ----
    def _build_results_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        header = QHBoxLayout()
        self.status_label = QLabel("Pick an action above to run it on all checked hosts.")
        self.status_label.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        header.addWidget(self.status_label)
        header.addStretch()
        btn_clear = QPushButton("Clear All Results")
        btn_clear.setToolTip("Close every per-host result tab below.")
        btn_clear.clicked.connect(self.clear_all_results)
        header.addWidget(btn_clear)
        layout.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        shrink_tabwidget_to_current_page(self.tabs)
        layout.addWidget(self.tabs)
        return panel

    def clear_all_results(self):
        """Close every per-host result tab at once. Shared by every tool
        built on FleetToolPage, so the button appears on all of them."""
        self.tabs.clear()
        self.results = {}
        self.pending = {}

    # ---- hosts ----
    def checked_entries(self):
        out = []
        for i in range(self.host_list.count()):
            item = self.host_list.item(i)
            entry = item.data(Qt.UserRole)
            if entry is not None and item.checkState() == Qt.Checked:
                out.append(entry)
        return out

    def load_hosts(self):
        checked = {_entry_key(e) for e in self.checked_entries()}
        try:
            entries = api.list_merged_hosts()
        except Exception as e:
            self.status_label.setStyleSheet(f"color: {STATUS_ERROR_COLOR};")
            self.status_label.setText(f"Could not load hosts: {e}")
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
            groups.setdefault(e.get("environment") or "", []).append(e)
        known = [e for e in environments if e in groups]
        extra = sorted(e for e in groups if e and e not in environments)
        for env in known + extra:
            self._add_host_header(env)
            for e in groups[env]:
                self._add_host_item(e, checked)
        if groups.get(""):
            self._add_host_header("Unassigned")
            for e in groups[""]:
                self._add_host_item(e, checked)
        apply_collapse_state(self.host_list)
        self.host_list.blockSignals(False)

    def _add_host_header(self, text):
        self.host_list.addItem(make_group_header_item(text, collapsed=text in self._collapsed_envs))

    def _add_host_item(self, entry, checked):
        item = QListWidgetItem(f"    {entry['label']}  [{entry['type_text']}]")
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked if _entry_key(entry) in checked else Qt.Unchecked)
        item.setData(Qt.UserRole, entry)
        self.host_list.addItem(item)

    def select_all_hosts(self):
        for i in range(self.host_list.count()):
            it = self.host_list.item(i)
            if it.data(Qt.UserRole) is not None:
                it.setCheckState(Qt.Checked)

    def deselect_all_hosts(self):
        for i in range(self.host_list.count()):
            it = self.host_list.item(i)
            if it.data(Qt.UserRole) is not None:
                it.setCheckState(Qt.Unchecked)

    # ---- dispatch + results ----
    def run_command(self, command, label):
        entries = self.checked_entries()
        if not entries:
            QMessageBox.information(self, "No hosts checked", "Check one or more target hosts first.")
            return
        self.last_command_label = label
        self.results = {}
        self.pending = {}
        self.tabs.clear()
        for entry in entries:
            key = _entry_key(entry)
            result = api.run_on_entry(entry, command)
            if result["sync"]:
                self.results[key] = {
                    "label": entry["label"], "stdout": result["stdout"],
                    "stderr": result["stderr"] or result["error"] or "",
                    "code": result["code"], "pending": False,
                }
            elif result["error"]:
                self.results[key] = {
                    "label": entry["label"], "stdout": "", "stderr": result["error"],
                    "code": None, "pending": False,
                }
            else:
                self.results[key] = {
                    "label": entry["label"], "stdout": "", "stderr": "",
                    "code": None, "pending": True,
                }
                self.pending[key] = (entry, result["task_id"])
            self._add_tab(key)
        self.status_label.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        self.status_label.setText(f"Running '{label}' on {len(entries)} host(s)...")
        if self.tabs.count() > 0:
            self.tabs.setCurrentIndex(0)
        if self.pending:
            self.poll_timer.start(POLL_MS)
        else:
            self.status_label.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
            self.status_label.setText(f"'{label}' complete.")

    def run_with(self, label, factory):
        """Build a command via `factory` (which may raise ValueError for bad
        input) and dispatch it; show the validation error otherwise."""
        try:
            command = factory()
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self.run_command(command, label)

    def _status_text(self, data):
        if data["pending"]:
            return "pending..."
        return "ok" if not data["stderr"] else "error"

    def _render(self, text_edit, data):
        if data["pending"]:
            text_edit.setPlainText("Waiting for this agent host to report back...")
            return
        label = self.last_command_label or "Action"
        text_edit.setHtml(result_banner.result_html(
            data, ok_label=f"{label} complete", fail_label=f"{label} failed"))

    def _add_tab(self, key):
        data = self.results.get(key)
        if not data:
            return
        te = QTextEdit()
        te.setReadOnly(True)
        te.setStyleSheet("font-family: monospace;")
        self._render(te, data)
        idx = self.tabs.addTab(te, f"{data['label']}  [{self._status_text(data)}]")
        self.tabs.tabBar().setTabData(idx, key)

    def _refresh_tab(self, key):
        bar = self.tabs.tabBar()
        for i in range(self.tabs.count()):
            if bar.tabData(i) != key:
                continue
            data = self.results.get(key)
            if data:
                self.tabs.setTabText(i, f"{data['label']}  [{self._status_text(data)}]")
                self._render(self.tabs.widget(i), data)
            return

    def _close_tab(self, index):
        key = self.tabs.tabBar().tabData(index)
        self.tabs.removeTab(index)
        self.results.pop(key, None)
        self.pending.pop(key, None)

    def _poll(self):
        if not self.pending:
            self.poll_timer.stop()
            return
        done = []
        for key, (entry, task_id) in list(self.pending.items()):
            result = api.poll_entry_result(entry, task_id)
            if result is None:
                continue
            self.results[key] = {
                "label": entry["label"], "stdout": result["stdout"],
                "stderr": result["stderr"] or result["error"] or "",
                "code": result["code"], "pending": False,
            }
            self._refresh_tab(key)
            done.append(key)
        for key in done:
            del self.pending[key]
        if not self.pending:
            self.poll_timer.stop()
            self.status_label.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
            self.status_label.setText("All hosts reported back.")
