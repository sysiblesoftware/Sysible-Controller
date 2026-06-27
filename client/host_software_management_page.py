import html
import os

from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout,
    QListWidget, QListWidgetItem, QLabel, QPushButton,
    QLineEdit, QTextEdit, QMessageBox, QTabWidget, QFileDialog,
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
        self.package_choices = []      # [(display, name)] from the most recent List/Search run

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
            "Target Hosts (agent-managed)", self.host_list,
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
            "e.g. nginx curl  (space-separated for more than one; also the search term / filter)"
        )
        # Doubles as the search term AND the live filter for the package
        # list below, so typing a name does double duty instead of needing
        # a second, separate field.
        self.package_name_input.textChanged.connect(self.filter_installed_packages)
        name_row.addWidget(self.package_name_input, 1)
        btn_search = QPushButton("Search Available")
        btn_search.setToolTip(
            "Search the host's repositories for available packages matching the "
            "term above (e.g. 'http' finds httpd / apache2). Results appear in the "
            "list below - click one to drop its name into the field, then Install."
        )
        btn_search.clicked.connect(self.run_search_packages)
        name_row.addWidget(btn_search)
        btn_list_packages = QPushButton("List Installed")
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
        btn_update.setToolTip("Upgrade the named package(s), or every installed package when the field is blank.")
        btn_update.clicked.connect(self.run_update)
        # Emphasized: keeping a fleet patched is the most common, most
        # important action here, so it reads as the primary button rather
        # than sitting flush with Install/Remove.
        btn_update.setStyleSheet(
            "QPushButton { font-weight: bold; background-color: #3C4B64; color: #ffffff; "
            "border: 1px solid #506080; border-radius: 4px; padding: 6px 14px; }"
            "QPushButton:hover { background-color: #4C6285; }"
        )
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

        # ---- Install a local package file across the checked hosts ----
        local_hdr = QLabel("Install a Local Package File (.deb / .rpm)")
        local_hdr.setStyleSheet("font-weight: bold;")
        layout.addWidget(local_hdr)

        local_row = QHBoxLayout()
        self.local_pkg_path = QLineEdit()
        self.local_pkg_path.setPlaceholderText("Local .deb or .rpm file on this computer…")
        local_row.addWidget(self.local_pkg_path, 1)
        btn_browse_pkg = QPushButton("Browse…")
        btn_browse_pkg.clicked.connect(self._browse_local_package)
        local_row.addWidget(btn_browse_pkg)
        btn_install_local = QPushButton("Upload && Install on Checked Hosts")
        btn_install_local.clicked.connect(self.run_install_local_package)
        local_row.addWidget(btn_install_local)
        layout.addLayout(local_row)

        local_hint = QLabel(
            "Uploads the file to /tmp on each checked host over SSH, then installs it with the "
            "host's package manager (dependencies resolve from its repos). Needs an SSH connection "
            "to the host; the agent-only file channel is too small for full packages."
        )
        theme.style_hint_label(local_hint)
        local_hint.setWordWrap(True)
        layout.addWidget(local_hint)

        return panel

    def clear_all_results(self):
        """Close every per-host result tab at once."""
        self.pkg_tabs.clear()
        self.pkg_results = {}
        self.pkg_pending = {}

    def _build_results_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        self.pkg_status = QLabel("Pick an action above to run it on all checked hosts.")
        self.pkg_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        _hdr = QHBoxLayout()
        _hdr.addWidget(self.pkg_status)
        _hdr.addStretch()
        _btn_clear_all = QPushButton("Clear All Results")
        _btn_clear_all.setToolTip("Close every per-host result tab below.")
        _btn_clear_all.clicked.connect(self.clear_all_results)
        _hdr.addWidget(_btn_clear_all)
        layout.addLayout(_hdr)

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

    def run_search_packages(self):
        term = self.package_name_input.text().strip()
        try:
            cmd = api.cmd_search_packages(term)
        except ValueError as e:
            QMessageBox.information(self, "Nothing to search for", str(e))
            return
        self._run_pkg_command(cmd, f"Search: '{term}'")

    def _browse_local_package(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a package file", "",
            "Packages (*.deb *.rpm);;All files (*)"
        )
        if path:
            self.local_pkg_path.setText(path)

    @staticmethod
    def _ssh_id_for(entry):
        """The SSH host id to upload through for a checked entry, or None if
        the host has no SSH connection (agent-only - can't take a full file)."""
        if entry["kind"] == "ssh":
            return entry.get("id")
        if entry["kind"] == "merged":
            return (entry.get("ssh_entry") or {}).get("id")
        return None

    def run_install_local_package(self):
        local = self.local_pkg_path.text().strip()
        if not local or not os.path.isfile(local):
            QMessageBox.warning(self, "No file", "Choose a local .deb or .rpm file first.")
            return
        entries = self.checked_entries()
        if not entries:
            QMessageBox.information(self, "No hosts checked", "Check one or more target hosts first.")
            return

        remote = "/tmp/" + os.path.basename(local)
        uploaded, failures = [], []
        for e in entries:
            ssh_id = self._ssh_id_for(e)
            if not ssh_id:
                failures.append(f"{e['label']}: no SSH connection (agent-only)")
                continue
            try:
                api.upload_file_ssh(ssh_id, local, remote)
                uploaded.append(e)
            except Exception as ex:
                failures.append(f"{e['label']}: {ex}")

        if failures:
            QMessageBox.warning(
                self, "Some uploads failed",
                "These hosts won't get the package:\n\n" + "\n".join(failures)
            )
        if not uploaded:
            return

        try:
            cmd = api.cmd_install_local_package(remote)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        # Install only on the hosts that actually received the file.
        self._run_pkg_command(cmd, f"Install local package {os.path.basename(local)}", entries=uploaded)

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
    def _run_pkg_command(self, command, label, entries=None):
        if entries is None:
            entries = self.checked_entries()

        if not entries:
            QMessageBox.information(self, "No hosts checked", "Check one or more target hosts first.")
            return

        self.last_command_label = label
        self.pkg_results = {}
        self.pkg_pending = {}
        self.pkg_tabs.clear()

        # Clear the stale picker unless this run will repopulate it
        # (List Installed or a Search), so an old installed list doesn't
        # linger under, say, an Install result.
        if label != "Installed Packages" and not label.startswith("Search:"):
            self.package_choices = []
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
        # Success is the exit code where we have one (the same rule the
        # result banner uses); stderr alone is NOT failure - many commands
        # write progress/warnings to stderr on success.
        code = data.get("code")
        failed = (code != 0) if code is not None else (bool(data["stderr"]) and not data["stdout"])
        return "error" if failed else "ok"

    def _render_pkg_result(self, text_edit, data):
        if data["pending"]:
            text_edit.setPlainText("Waiting for this agent host to report back...")
            return

        # Success is judged by the exit code, not by whether anything went
        # to stderr - apt writes harmless notices there ("apt does not have
        # a stable CLI...", autoremove hints) even on a clean upgrade. A
        # prominent banner makes "it finished" obvious: apt's own output
        # just trails off (e.g. "Processing triggers for ...") with nothing
        # that reads as "done".
        code = data["code"]
        label = getattr(self, "last_command_label", "Operation")
        if code is not None:
            failed = code != 0
        else:
            failed = bool(data["stderr"]) and not data["stdout"]

        if failed:
            bg = STATUS_ERROR_COLOR
            headline = f"✗ {label} failed"
        else:
            bg = STATUS_SUCCESS_COLOR
            headline = f"✓ {label} complete"
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
                if i == self.pkg_tabs.currentIndex():
                    self._maybe_populate_picker(data)
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
            self.pkg_status.setText(f"'{self.last_command_label}' complete on all hosts.")

    def on_pkg_tab_changed(self, index):
        if index < 0:
            return
        key = self.pkg_tabs.tabBar().tabData(index)
        data = self.pkg_results.get(key)
        if not data or data["pending"]:
            return
        self._maybe_populate_picker(data)

    # =========================================================
    # INSTALLED PACKAGES SEARCH (filterable view of the most recent
    # "List Installed Packages" result for whichever host is selected
    # above - same pattern as Service Management's installed-services
    # search/filter_services)
    # =========================================================
    def _maybe_populate_picker(self, data):
        """Fill the package picker below the field from a result, depending
        on which action produced it - the installed list, or repository
        search results. Any other action leaves the picker alone."""
        if data is None or data.get("pending"):
            return
        label = self.last_command_label or ""
        if label == "Installed Packages":
            self._populate_installed_packages(data["stdout"])
        elif label.startswith("Search:"):
            self._populate_search_results(data["stdout"])

    def _populate_installed_packages(self, stdout):
        names = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
        if names == ["Neither dpkg nor rpm found on this host."]:
            names = []
        # (display, name) - identical here; the search path differs.
        self.package_choices = [(n, n) for n in sorted(set(names))]
        self._render_package_choices(self.package_name_input.text())

    def _populate_search_results(self, stdout):
        # Each line is "<name> <summary>" (see api.cmd_search_packages).
        seen = set()
        choices = []
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line or line.startswith("No supported package manager"):
                continue
            parts = line.split(None, 1)
            name = parts[0]
            if name in seen:
                continue
            seen.add(name)
            choices.append((line, name))
        self.package_choices = sorted(choices, key=lambda c: c[1].lower())
        self._render_package_choices(self.package_name_input.text())

    def _render_package_choices(self, text):
        self.installed_packages_list.clear()
        text = (text or "").lower()
        for display, name in self.package_choices:
            if text in display.lower():
                item = QListWidgetItem(display)
                item.setData(Qt.UserRole, name)
                self.installed_packages_list.addItem(item)

    def filter_installed_packages(self, text):
        self._render_package_choices(text)

    def _pick_installed_package(self, item):
        # Drop just the package name into the field (a search row also
        # shows its summary, which must not end up in the field).
        name = item.data(Qt.UserRole) or item.text()
        self.package_name_input.setText(name)
