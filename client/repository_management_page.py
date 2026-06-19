from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout,
    QListWidget, QListWidgetItem, QLabel, QPushButton,
    QLineEdit, QTextEdit, QMessageBox, QTabWidget,
    QCheckBox, QFrame,
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

HOST_REFRESH_MS = 10000
REPO_POLL_MS = 2000


def _entry_key(entry):
    """Hashable identity for a merged-host entry (api.list_merged_hosts()) -
    agent host_ids and SSH host names live in separate namespaces, so the
    kind has to be part of the key."""
    return (entry["kind"], entry["id"])


class RepositoryManagementPage(QWidget):
    """
    Repository Management against a *merged* host list (agent-enrolled
    hosts AND SSH-enrolled hosts) - configure where packages come
    from: list, add, enable, disable, and remove software repositories.

    Kept as its own tile, separate from Host Software Management
    (client/host_software_management_page.py), since configuring
    repository sources is a distinct, less-frequent operation from
    installing/removing/updating the packages themselves - the same
    split the original feature request asked for.

    "Alias / Repository ID" means different things per package
    manager, surfaced in the hint label below the fields: zypper and
    Debian/Ubuntu need it up front to add a repo (it becomes the
    filename/alias actions below target), while dnf/yum assign their
    own ID when adding - visible by running List Repositories - so the
    field there is only used for Enable/Disable/Remove afterward, not
    for Add.
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Repository Management")
        self.resize(1000, 760)

        self.repo_results = {}    # entry_key -> {label, stdout, stderr, code, pending}
        self.repo_pending = {}    # entry_key -> (entry, task_id)

        main = QVBoxLayout()
        self.setLayout(main)

        main.addLayout(make_page_header("Repository Management"))

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
        # REPOSITORY FIELDS + ACTIONS
        # =========================================================
        main.addWidget(self._build_actions_panel())

        # =========================================================
        # RESULTS
        # =========================================================
        main.addWidget(self._build_results_panel(), 1)

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

        self.repo_poll_timer = QTimer()
        self.repo_poll_timer.timeout.connect(self._poll_repo)

        bus.host_removed.connect(self.load_hosts)

    # =========================================================
    # PANEL BUILDERS
    # =========================================================
    def _build_actions_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        url_row = QHBoxLayout()
        url_row.addWidget(QLabel("Repository URL / source line:"))
        self.repo_url_input = QLineEdit()
        self.repo_url_input.setPlaceholderText(
            "RHEL/SUSE: a repo URL (e.g. https://.../docker-ce.repo)  |  "
            "Debian/Ubuntu: a full source line (e.g. deb https://example.com/ubuntu focal main)"
        )
        url_row.addWidget(self.repo_url_input, 1)
        layout.addLayout(url_row)

        alias_row = QHBoxLayout()
        alias_row.addWidget(QLabel("Alias / Repository ID:"))
        self.repo_alias_input = QLineEdit()
        self.repo_alias_input.setPlaceholderText("e.g. docker-ce  (letters, numbers, dots, dashes, underscores)")
        self.repo_alias_input.setMaximumWidth(360)
        alias_row.addWidget(self.repo_alias_input)
        alias_row.addStretch()
        layout.addLayout(alias_row)

        hint = QLabel(
            "Alias / Repository ID is required for zypper and Debian/Ubuntu when adding a repository, and "
            "required on every package manager for Enable / Disable / Remove afterward. For dnf/yum, Add "
            "ignores this field - run List Repositories afterward to see the ID dnf/yum assigned, then use "
            "that ID here for Enable/Disable/Remove."
        )
        theme.style_hint_label(hint)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        row1 = QHBoxLayout()
        btn_list = QPushButton("List Repositories")
        btn_list.clicked.connect(self.run_list)
        btn_add = QPushButton("Add Repository")
        btn_add.clicked.connect(self.run_add)
        btn_add_all = QPushButton("Add Repository to All Hosts")
        btn_add_all.setToolTip(
            "Add this repository to every enrolled host (agent + SSH), regardless "
            "of which hosts are checked above."
        )
        btn_add_all.clicked.connect(self.run_add_to_all)
        for b in (btn_list, btn_add, btn_add_all):
            row1.addWidget(b)
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        btn_enable = QPushButton("Enable Repository")
        btn_enable.clicked.connect(self.run_enable)
        btn_disable = QPushButton("Disable Repository")
        btn_disable.clicked.connect(self.run_disable)
        btn_remove = QPushButton("Remove Repository")
        btn_remove.clicked.connect(self.run_remove)
        for b in (btn_enable, btn_disable, btn_remove):
            row2.addWidget(b)
        layout.addLayout(row2)

        layout.addWidget(self._divider())
        layout.addLayout(self._build_create_repo_section())

        return panel

    def _divider(self):
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        return line

    def _build_create_repo_section(self):
        """Builds a real repo definition from these fields directly,
        for the common case where there's no existing hosted .repo URL
        or hand-written source line to point Add Repository at - just
        a baseurl (and optionally a GPG key) to turn into one. Reuses
        the Alias / Repository ID field above rather than adding a
        second alias field, since it's the same "Alias / Repository
        ID" concept either way."""
        section = QVBoxLayout()

        title = QLabel("Create Repository (build one from scratch)")
        title.setStyleSheet("font-weight: bold;")
        section.addWidget(title)

        hint = QLabel(
            "Writes a real repo definition from the fields below, using the Alias / "
            "Repository ID field above as the repo's name/filename - no existing "
            "hosted .repo URL or hand-written source line required. GPG Key URL (if "
            "given) is fetched and imported/trusted automatically. Distribution and "
            "Components only apply on Debian/Ubuntu (apt) hosts."
        )
        theme.style_hint_label(hint)
        hint.setWordWrap(True)
        section.addWidget(hint)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name:"))
        self.create_name_input = QLineEdit()
        self.create_name_input.setPlaceholderText("Display name (optional - defaults to Alias / Repository ID)")
        name_row.addWidget(self.create_name_input, 1)
        section.addLayout(name_row)

        baseurl_row = QHBoxLayout()
        baseurl_row.addWidget(QLabel("Base URL:"))
        self.create_baseurl_input = QLineEdit()
        self.create_baseurl_input.setPlaceholderText("e.g. https://download.example.com/repo/el9/x86_64")
        baseurl_row.addWidget(self.create_baseurl_input, 1)
        section.addLayout(baseurl_row)

        gpg_row = QHBoxLayout()
        self.create_gpgcheck = QCheckBox("GPG Check")
        self.create_gpgcheck.setChecked(True)
        gpg_row.addWidget(self.create_gpgcheck)
        gpg_row.addWidget(QLabel("GPG Key URL:"))
        self.create_gpgkey_input = QLineEdit()
        self.create_gpgkey_input.setPlaceholderText("e.g. https://download.example.com/RPM-GPG-KEY  (optional)")
        gpg_row.addWidget(self.create_gpgkey_input, 1)
        section.addLayout(gpg_row)

        apt_row = QHBoxLayout()
        apt_row.addWidget(QLabel("Distribution:"))
        self.create_distribution_input = QLineEdit()
        self.create_distribution_input.setPlaceholderText("apt only - e.g. focal, jammy  (default: stable)")
        self.create_distribution_input.setMaximumWidth(220)
        apt_row.addWidget(self.create_distribution_input)
        apt_row.addWidget(QLabel("Components:"))
        self.create_components_input = QLineEdit()
        self.create_components_input.setPlaceholderText("apt only - e.g. main contrib  (default: main)")
        apt_row.addWidget(self.create_components_input, 1)
        section.addLayout(apt_row)

        btn_create = QPushButton("Create Repository")
        btn_create.setStyleSheet("font-weight: bold;")
        btn_create.clicked.connect(self.run_create)
        section.addWidget(btn_create)

        return section

    def _build_results_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        self.repo_status = QLabel("Pick an action above to run it on all checked hosts.")
        self.repo_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        layout.addWidget(self.repo_status)

        self.repo_tabs = QTabWidget()
        self.repo_tabs.setTabsClosable(True)
        self.repo_tabs.tabCloseRequested.connect(self._close_repo_tab)
        layout.addWidget(self.repo_tabs)
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
            self.repo_status.setStyleSheet(f"color: {STATUS_ERROR_COLOR};")
            self.repo_status.setText(f"Could not load hosts: {e}")
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
    # FIELD HELPERS
    # =========================================================
    def _alias(self):
        alias = self.repo_alias_input.text().strip()
        if not alias:
            QMessageBox.information(self, "No alias", "Type an Alias / Repository ID above first.")
            return None
        return alias

    # =========================================================
    # ACTIONS
    # =========================================================
    def run_list(self):
        self._run_repo_command(api.cmd_list_repositories(), "Repositories")

    def run_add(self):
        cmd, url = self._build_add_command()
        if cmd is None:
            return
        self._run_repo_command(cmd, f"Add Repository '{url}'")

    def run_add_to_all(self):
        """Add the repository typed above to literally every host in the
        fleet (agent + SSH), not just whatever happens to be checked in
        the Target Hosts list right now. The normal Add Repository button
        already supports "add to several hosts" via the checklist + sync
        action below; this is the one-click "every server" shortcut for
        when a repo's been proven out on one host and should now go
        everywhere, without having to remember to Select All first."""
        cmd, url = self._build_add_command()
        if cmd is None:
            return

        try:
            entries = api.list_merged_hosts()
        except Exception as e:
            QMessageBox.warning(self, "Could not load hosts", f"Could not load the fleet host list: {e}")
            return

        if not entries:
            QMessageBox.information(self, "No hosts", "No agent or SSH hosts are enrolled yet.")
            return

        confirm = QMessageBox.question(
            self, "Add Repository to All Hosts",
            f"Add repository '{url}' to all {len(entries)} enrolled host(s), "
            "regardless of which hosts are checked above?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        # Keep the checklist in sync with what was actually targeted, so
        # the result tabs and the visible checkboxes agree afterward.
        self.select_all_hosts()
        self._dispatch_repo_command(cmd, f"Add Repository '{url}' to All Hosts", entries)

    def _build_add_command(self):
        """Shared URL/alias validation for run_add() and run_add_to_all().
        Returns (command, url) on success, or (None, None) if a
        QMessageBox was already shown for invalid input."""
        url = self.repo_url_input.text().strip()
        if not url:
            QMessageBox.information(self, "No URL", "Type a repository URL / source line above first.")
            return None, None
        try:
            cmd = api.cmd_add_repository(url, self.repo_alias_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return None, None
        return cmd, url

    def run_enable(self):
        alias = self._alias()
        if not alias:
            return
        try:
            cmd = api.cmd_enable_repository(alias)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_repo_command(cmd, f"Enable Repository '{alias}'")

    def run_disable(self):
        alias = self._alias()
        if not alias:
            return
        try:
            cmd = api.cmd_disable_repository(alias)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_repo_command(cmd, f"Disable Repository '{alias}'")

    def run_remove(self):
        alias = self._alias()
        if not alias:
            return
        try:
            cmd = api.cmd_remove_repository(alias)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_repo_command(cmd, f"Remove Repository '{alias}'")

    def run_create(self):
        alias = self._alias()
        if not alias:
            return
        try:
            cmd = api.cmd_create_repository(
                alias,
                self.create_baseurl_input.text(),
                name=self.create_name_input.text(),
                gpgcheck=self.create_gpgcheck.isChecked(),
                gpgkey=self.create_gpgkey_input.text(),
                distribution=self.create_distribution_input.text(),
                components=self.create_components_input.text(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_repo_command(cmd, f"Create Repository '{alias}'")

    # =========================================================
    # DISPATCH + RESULTS (same pattern as Service Management /
    # Host Software Management)
    # =========================================================
    def _run_repo_command(self, command, label):
        entries = self.checked_entries()

        if not entries:
            QMessageBox.information(self, "No hosts checked", "Check one or more target hosts first.")
            return

        self._dispatch_repo_command(command, label, entries)

    def _dispatch_repo_command(self, command, label, entries):
        self.repo_results = {}
        self.repo_pending = {}
        self.repo_tabs.clear()

        for entry in entries:
            key = _entry_key(entry)
            result = api.run_on_entry(entry, command)

            if result["sync"]:
                self.repo_results[key] = {
                    "label": entry["label"],
                    "stdout": result["stdout"],
                    "stderr": result["stderr"] or result["error"] or "",
                    "code": result["code"],
                    "pending": False,
                }
            elif result["error"]:
                self.repo_results[key] = {
                    "label": entry["label"], "stdout": "", "stderr": result["error"],
                    "code": None, "pending": False,
                }
            else:
                self.repo_results[key] = {
                    "label": entry["label"], "stdout": "", "stderr": "",
                    "code": None, "pending": True,
                }
                self.repo_pending[key] = (entry, result["task_id"])

            self._add_repo_tab(key)

        self.repo_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        self.repo_status.setText(f"Running '{label}' on {len(entries)} host(s)...")

        if self.repo_tabs.count() > 0:
            self.repo_tabs.setCurrentIndex(0)

        if self.repo_pending:
            self.repo_poll_timer.start(REPO_POLL_MS)
        else:
            self.repo_status.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
            self.repo_status.setText(f"'{label}' complete.")

    def _status_text(self, data):
        if data["pending"]:
            return "pending..."
        return "ok" if not data["stderr"] else "error"

    def _render_repo_result(self, text_edit, data):
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

    def _close_repo_tab(self, index):
        bar = self.repo_tabs.tabBar()
        key = bar.tabData(index)
        self.repo_tabs.removeTab(index)
        self.repo_results.pop(key, None)
        self.repo_pending.pop(key, None)

    def _add_repo_tab(self, key):
        data = self.repo_results.get(key)
        if not data:
            return
        status = self._status_text(data)

        text_edit = QTextEdit()
        text_edit.setReadOnly(True)
        text_edit.setStyleSheet("font-family: monospace;")
        self._render_repo_result(text_edit, data)

        idx = self.repo_tabs.addTab(text_edit, f"{data['label']}  [{status}]")
        self.repo_tabs.tabBar().setTabData(idx, key)

    def _refresh_repo_tab(self, key):
        bar = self.repo_tabs.tabBar()
        for i in range(self.repo_tabs.count()):
            if bar.tabData(i) != key:
                continue
            data = self.repo_results.get(key)
            if data:
                status = self._status_text(data)
                self.repo_tabs.setTabText(i, f"{data['label']}  [{status}]")
                self._render_repo_result(self.repo_tabs.widget(i), data)
            return

    def _poll_repo(self):
        if not self.repo_pending:
            self.repo_poll_timer.stop()
            return

        done = []

        for key, (entry, task_id) in list(self.repo_pending.items()):
            result = api.poll_entry_result(entry, task_id)
            if result is None:
                continue

            self.repo_results[key] = {
                "label": entry["label"],
                "stdout": result["stdout"],
                "stderr": result["stderr"] or result["error"] or "",
                "code": result["code"],
                "pending": False,
            }
            self._refresh_repo_tab(key)
            done.append(key)

        for key in done:
            del self.repo_pending[key]

        if not self.repo_pending:
            self.repo_poll_timer.stop()
            self.repo_status.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
            self.repo_status.setText("All hosts reported back.")
