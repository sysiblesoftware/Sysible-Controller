from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout,
    QListWidget, QListWidgetItem, QLabel, QPushButton,
    QLineEdit, QTextEdit, QMessageBox, QTabWidget,
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
PKG_POLL_MS = 2000


def _entry_key(entry):
    """Hashable identity for a merged-host entry (api.list_merged_hosts()) -
    agent host_ids and SSH host names live in separate namespaces, so the
    kind has to be part of the key."""
    return (entry["kind"], entry["id"])


class HostSoftwareManagementPage(QWidget):
    """
    Host Software Management against a *merged* host list (agent-
    enrolled hosts AND SSH-enrolled hosts) - install, remove, update/
    upgrade, query, and verify packages, plus a Detect Package Manager
    / OS action, all dispatched to whichever hosts are checked.

    Originally this whole feature didn't exist - the app could install
    its own dependencies via install_sysible.sh, but had no way to
    manage *managed hosts'* packages at all. Built cross-distro from
    the start (every command in client/api.py's Host Software
    Management section auto-detects dnf/yum/zypper/apt-get on each
    target host), so a fleet mixing RHEL/CentOS/Fedora, SUSE, and
    Debian/Ubuntu hosts can all be checked at once and each gets the
    right command. Repository configuration is deliberately a separate
    tool - see client/repository_management_page.py - since it's a
    distinct, less-frequent operation from installing/removing/
    updating packages themselves.
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Host Software Management")
        self.resize(1350, 820)

        self.pkg_results = {}    # entry_key -> {label, stdout, stderr, code, pending}
        self.pkg_pending = {}    # entry_key -> (entry, task_id)
        self.last_command_label = None
        self.installed_packages = []   # parsed from the most recent "List Installed Packages" run

        main = QVBoxLayout()
        self.setLayout(main)

        main.addLayout(make_page_header("Host Software Management"))

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
            "Target Hosts (agent + SSH)", self.host_list,
            [[btn_refresh_hosts, btn_select_all, btn_deselect_all],
             [btn_collapse_all, btn_expand_all]],
        ))

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)

        # ---------------------------------------------------------
        # DETECT + PACKAGE NAME + ACTIONS
        # ---------------------------------------------------------
        content_layout.addWidget(self._build_actions_panel())

        # ---------------------------------------------------------
        # RESULTS (stretchy - see service_management_page.py for why)
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

        self.pkg_poll_timer = QTimer()
        self.pkg_poll_timer.timeout.connect(self._poll_pkg)

        bus.host_removed.connect(self.load_hosts)

    # =========================================================
    # PANEL BUILDERS
    # =========================================================
    def _build_actions_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        detect_row = QHBoxLayout()
        detect_hint = QLabel(
            "Not sure what a host is running? Detect its OS and package manager first:"
        )
        theme.style_hint_label(detect_hint)
        detect_row.addWidget(detect_hint)
        detect_row.addStretch()
        btn_detect = QPushButton("Detect Package Manager / OS")
        btn_detect.setStyleSheet("font-weight: bold;")
        btn_detect.clicked.connect(self.run_detect_environment)
        detect_row.addWidget(btn_detect)
        layout.addLayout(detect_row)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Package name(s):"))
        self.package_name_input = QLineEdit()
        self.package_name_input.setPlaceholderText(
            "e.g. nginx curl  (space-separated for more than one - also filters the list below)"
        )
        # Doubles as the live filter for the installed-packages list
        # below once "List Installed Packages" has been run, so typing
        # a name does double duty instead of needing a second,
        # separate search field that just feeds back into this one.
        self.package_name_input.textChanged.connect(self.filter_installed_packages)
        name_row.addWidget(self.package_name_input, 1)
        btn_list_packages = QPushButton("List Installed Packages")
        btn_list_packages.clicked.connect(self.run_list_installed)
        name_row.addWidget(btn_list_packages)
        layout.addLayout(name_row)

        self.installed_packages_list = QListWidget()
        self.installed_packages_list.setMaximumHeight(130)
        self.installed_packages_list.itemClicked.connect(self._pick_installed_package)
        layout.addWidget(self.installed_packages_list)

        row1 = QHBoxLayout()
        btn_install = QPushButton("Install")
        btn_install.clicked.connect(self.run_install)
        btn_remove = QPushButton("Remove")
        btn_remove.clicked.connect(self.run_remove)
        btn_update = QPushButton("Update / Upgrade")
        btn_update.clicked.connect(self.run_update)
        for b in (btn_install, btn_remove, btn_update):
            row1.addWidget(b)
        layout.addLayout(row1)

        update_hint = QLabel("Update / Upgrade with the field above blank upgrades every installed package on the checked hosts.")
        theme.style_hint_label(update_hint)
        update_hint.setWordWrap(True)
        layout.addWidget(update_hint)

        row2 = QHBoxLayout()
        btn_query = QPushButton("Query Package Info")
        btn_query.clicked.connect(self.run_query)
        btn_verify = QPushButton("Verify Package Integrity")
        btn_verify.clicked.connect(self.run_verify)
        btn_clean = QPushButton("Clean Package Cache")
        btn_clean.clicked.connect(self.run_clean_cache)
        for b in (btn_query, btn_verify, btn_clean):
            row2.addWidget(b)
        layout.addLayout(row2)

        return panel

    def _build_results_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        self.pkg_status = QLabel("Pick an action above to run it on all checked hosts.")
        self.pkg_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        layout.addWidget(self.pkg_status)

        self.pkg_tabs = QTabWidget()
        self.pkg_tabs.setTabsClosable(True)
        self.pkg_tabs.tabCloseRequested.connect(self._close_pkg_tab)
        self.pkg_tabs.currentChanged.connect(self.on_pkg_tab_changed)
        shrink_tabwidget_to_current_page(self.pkg_tabs)
        layout.addWidget(self.pkg_tabs)
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
            self.pkg_status.setStyleSheet(f"color: {STATUS_ERROR_COLOR};")
            self.pkg_status.setText(f"Could not load hosts: {e}")
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
    # PACKAGE NAME HELPER
    # =========================================================
    def _required_package_names(self):
        names = self.package_name_input.text().strip()
        if not names:
            QMessageBox.information(self, "No package name", "Type at least one package name above first.")
            return None
        return names

    # =========================================================
    # ACTIONS
    # =========================================================
    def run_detect_environment(self):
        self._run_pkg_command(api.cmd_detect_host_environment(), "Detected Host Environment")

    def run_list_installed(self):
        self._run_pkg_command(api.cmd_list_installed_packages(), "Installed Packages")

    def run_install(self):
        names = self._required_package_names()
        if not names:
            return
        try:
            cmd = api.cmd_install_packages(names)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_pkg_command(cmd, f"Install '{names}'")

    def run_remove(self):
        names = self._required_package_names()
        if not names:
            return
        try:
            cmd = api.cmd_remove_packages(names)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_pkg_command(cmd, f"Remove '{names}'")

    def run_update(self):
        names = self.package_name_input.text().strip()
        cmd = api.cmd_update_packages(names)
        label = f"Update / Upgrade '{names}'" if names else "Update / Upgrade All Packages"
        self._run_pkg_command(cmd, label)

    def run_query(self):
        names = self._required_package_names()
        if not names:
            return
        try:
            cmd = api.cmd_query_package(names)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_pkg_command(cmd, f"Query Info '{names}'")

    def run_verify(self):
        names = self._required_package_names()
        if not names:
            return
        try:
            cmd = api.cmd_verify_package(names)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_pkg_command(cmd, f"Verify Integrity '{names}'")

    def run_clean_cache(self):
        self._run_pkg_command(api.cmd_clean_package_cache(), "Clean Package Cache")

    # =========================================================
    # DISPATCH + RESULTS (same pattern as Service Management)
    # =========================================================
    def _run_pkg_command(self, command, label):
        entries = self.checked_entries()

        if not entries:
            QMessageBox.information(self, "No hosts checked", "Check one or more target hosts first.")
            return

        self.last_command_label = label
        self.pkg_results = {}
        self.pkg_pending = {}
        self.pkg_tabs.clear()

        if label != "Installed Packages":
            self.installed_packages = []
            self.installed_packages_list.clear()

        for entry in entries:
            key = _entry_key(entry)
            result = api.run_on_entry(entry, command)

            if result["sync"]:
                self.pkg_results[key] = {
                    "label": entry["label"],
                    "stdout": result["stdout"],
                    "stderr": result["stderr"] or result["error"] or "",
                    "code": result["code"],
                    "pending": False,
                }
            elif result["error"]:
                self.pkg_results[key] = {
                    "label": entry["label"], "stdout": "", "stderr": result["error"],
                    "code": None, "pending": False,
                }
            else:
                self.pkg_results[key] = {
                    "label": entry["label"], "stdout": "", "stderr": "",
                    "code": None, "pending": True,
                }
                self.pkg_pending[key] = (entry, result["task_id"])

            self._add_pkg_tab(key)

        self.pkg_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        self.pkg_status.setText(f"Running '{label}' on {len(entries)} host(s)...")

        if self.pkg_tabs.count() > 0:
            self.pkg_tabs.setCurrentIndex(0)
            self.on_pkg_tab_changed(0)

        if self.pkg_pending:
            self.pkg_poll_timer.start(PKG_POLL_MS)
        else:
            self.pkg_status.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
            self.pkg_status.setText(f"'{label}' complete.")

    def _status_text(self, data):
        if data["pending"]:
            return "pending..."
        return "ok" if not data["stderr"] else "error"

    def _render_pkg_result(self, text_edit, data):
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

    def _close_pkg_tab(self, index):
        bar = self.pkg_tabs.tabBar()
        key = bar.tabData(index)
        self.pkg_tabs.removeTab(index)
        self.pkg_results.pop(key, None)
        self.pkg_pending.pop(key, None)

    def _add_pkg_tab(self, key):
        data = self.pkg_results.get(key)
        if not data:
            return
        status = self._status_text(data)

        text_edit = QTextEdit()
        text_edit.setReadOnly(True)
        text_edit.setStyleSheet("font-family: monospace;")
        self._render_pkg_result(text_edit, data)

        idx = self.pkg_tabs.addTab(text_edit, f"{data['label']}  [{status}]")
        self.pkg_tabs.tabBar().setTabData(idx, key)

    def _refresh_pkg_tab(self, key):
        bar = self.pkg_tabs.tabBar()
        for i in range(self.pkg_tabs.count()):
            if bar.tabData(i) != key:
                continue
            data = self.pkg_results.get(key)
            if data:
                status = self._status_text(data)
                self.pkg_tabs.setTabText(i, f"{data['label']}  [{status}]")
                self._render_pkg_result(self.pkg_tabs.widget(i), data)
                if self.last_command_label == "Installed Packages" and i == self.pkg_tabs.currentIndex():
                    self._populate_installed_packages(data["stdout"])
            return

    def _poll_pkg(self):
        if not self.pkg_pending:
            self.pkg_poll_timer.stop()
            return

        done = []

        for key, (entry, task_id) in list(self.pkg_pending.items()):
            result = api.poll_entry_result(entry, task_id)
            if result is None:
                continue

            self.pkg_results[key] = {
                "label": entry["label"],
                "stdout": result["stdout"],
                "stderr": result["stderr"] or result["error"] or "",
                "code": result["code"],
                "pending": False,
            }
            self._refresh_pkg_tab(key)
            done.append(key)

        for key in done:
            del self.pkg_pending[key]

        if not self.pkg_pending:
            self.pkg_poll_timer.stop()
            self.pkg_status.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
            self.pkg_status.setText("All hosts reported back.")

    def on_pkg_tab_changed(self, index):
        if index < 0:
            return
        key = self.pkg_tabs.tabBar().tabData(index)
        data = self.pkg_results.get(key)
        if not data or data["pending"]:
            return
        if self.last_command_label == "Installed Packages":
            self._populate_installed_packages(data["stdout"])

    # =========================================================
    # INSTALLED PACKAGES SEARCH (filterable view of the most recent
    # "List Installed Packages" result for whichever host is selected
    # above - same pattern as Service Management's installed-services
    # search/filter_services)
    # =========================================================
    def _populate_installed_packages(self, stdout):
        names = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
        if names == ["Neither dpkg nor rpm found on this host."]:
            names = []
        self.installed_packages = sorted(set(names))
        self._render_installed_packages(self.package_name_input.text())

    def _render_installed_packages(self, text):
        self.installed_packages_list.clear()
        text = (text or "").lower()
        for name in self.installed_packages:
            if text in name.lower():
                self.installed_packages_list.addItem(name)

    def filter_installed_packages(self, text):
        self._render_installed_packages(text)

    def _pick_installed_package(self, item):
        self.package_name_input.setText(item.text())
