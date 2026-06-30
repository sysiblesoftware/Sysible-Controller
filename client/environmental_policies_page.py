import html
import re

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QMessageBox, QFrame, QCheckBox, QScrollArea, QListWidget, QListWidgetItem,
    QTextEdit, QTabWidget, QGroupBox,
)

from client import api
from client import theme
from client.theme import STATUS_NEUTRAL_COLOR, STATUS_SUCCESS_COLOR, STATUS_ERROR_COLOR
from client.branding import make_page_header
from client.tab_sizing import shrink_tabwidget_to_current_page
from client.host_panel import build_host_panel

HOST_REFRESH_MS = 10000
PUSH_POLL_MS = 2000


def _entry_key(entry):
    """Hashable identity for a merged-host entry (api.list_merged_hosts()) -
    agent host_ids and SSH host names live in separate namespaces, so the
    kind has to be part of the key."""
    return (entry["kind"], entry["id"])


class EnvironmentalPoliciesPage(QWidget):
    """
    Baseline password/lockout/sudo/umask policy for accounts on managed
    target hosts. Distinct from Sysible Controller Settings' Administrator Password
    Policy, which only governs this controller's own GUI-login accounts.

    Two halves:
      - Policy Defaults: the saved baseline (backend's environmental_policy
        singleton). User & Group Administration's Generate Password / Set
        Password / Create User read this same object to decide what a
        valid password looks like - saving here takes effect there
        immediately, no host push required.
      - Push To Hosts: actually applies whichever of the saved settings
        are checked to a chosen set of enrolled hosts' real configuration
        (pwquality.conf / faillock.conf / sudoers.d / login.defs) - same
        merged agent+SSH host checklist and sync/async dispatch pattern as
        Service Management.
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Environmental Policies")
        self.resize(1150, 860)

        self.push_results = {}   # entry_key -> {label, stdout, stderr, code, pending}
        self.push_pending = {}   # entry_key -> (entry, task_id)

        outer = QVBoxLayout()
        self.setLayout(outer)

        outer.addWidget(make_page_header("Environmental Policies", font_size=22, logo_height=32))

        body = QHBoxLayout()

        # =========================================================
        # TARGET HOSTS (agent + SSH, merged) - left column, full height
        # =========================================================
        # Used to live as a fixed-110px-tall QListWidget mid-scroll inside
        # the "Push To Hosts" section below - moved up here (#352) so the
        # checklist gets the page's full height instead of a few visible
        # rows, same as every other System Administration tool.
        self.host_list = QListWidget()

        btn_refresh_hosts = QPushButton("Refresh Hosts")
        btn_refresh_hosts.clicked.connect(self.load_hosts)

        btn_select_all = QPushButton("Select All")
        btn_select_all.clicked.connect(self.select_all_hosts)

        btn_deselect_all = QPushButton("Deselect All")
        btn_deselect_all.clicked.connect(self.deselect_all_hosts)

        host_panel = build_host_panel(
            "Target Hosts (agent-managed)",
            self.host_list,
            [[btn_refresh_hosts, btn_select_all, btn_deselect_all]],
        )
        body.addWidget(host_panel)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        body.addWidget(scroll, 1)

        outer.addLayout(body, 1)

        content = QWidget()
        layout = QVBoxLayout(content)
        scroll.setWidget(content)

        # =====================================================
        # SECTION 1: POLICY DEFAULTS
        # =====================================================
        self._build_defaults_section(layout)

        layout.addWidget(self._divider())

        # =====================================================
        # SECTION 2: PUSH TO HOSTS
        # =====================================================
        self._build_push_section(layout)

        layout.addStretch()

        self.refresh()

        self.host_refresh_timer = QTimer()
        self.host_refresh_timer.timeout.connect(self.load_hosts)
        self.host_refresh_timer.start(HOST_REFRESH_MS)

        self.push_poll_timer = QTimer()
        self.push_poll_timer.timeout.connect(self._poll_push)

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

    # =========================================================
    # SECTION 1: POLICY DEFAULTS
    # =========================================================
    def _build_defaults_section(self, layout):
        layout.addWidget(self._section_label("Policy Defaults"))

        hint = QLabel(
            "The saved baseline for accounts on managed hosts. User & Group "
            "Administration's Generate Password / Set Password / Create User "
            "enforce whatever is saved here - no host push required for that. "
            "Use \"Push To Hosts\" below to actually apply these to specific "
            "hosts' pwquality / faillock / sudoers / login.defs configuration."
        )
        theme.style_hint_label(hint)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # -- Password --
        pw_group = QGroupBox("Password")
        pw_layout = QVBoxLayout(pw_group)
        pw_row1 = QHBoxLayout()
        pw_row1.addWidget(QLabel("Minimum length:"))
        self.pw_minlen_input = QLineEdit()
        self.pw_minlen_input.setMaximumWidth(60)
        pw_row1.addWidget(self.pw_minlen_input)
        pw_row1.addSpacing(18)
        pw_row1.addWidget(QLabel("Retry attempts:"))
        self.pw_retry_input = QLineEdit()
        self.pw_retry_input.setMaximumWidth(60)
        pw_row1.addWidget(self.pw_retry_input)
        pw_row1.addStretch()
        pw_layout.addLayout(pw_row1)

        pw_row2 = QHBoxLayout()
        self.pw_require_upper = QCheckBox("Require uppercase")
        self.pw_require_lower = QCheckBox("Require lowercase")
        self.pw_require_digit = QCheckBox("Require digit")
        self.pw_require_symbol = QCheckBox("Require symbol")
        for cb in (self.pw_require_upper, self.pw_require_lower, self.pw_require_digit, self.pw_require_symbol):
            pw_row2.addWidget(cb)
        pw_row2.addStretch()
        pw_layout.addLayout(pw_row2)
        layout.addWidget(pw_group)

        # -- Lockout --
        lockout_group = QGroupBox("Lockout")
        lockout_row = QHBoxLayout(lockout_group)
        lockout_row.addWidget(QLabel("Failed attempts before lock:"))
        self.lockout_deny_input = QLineEdit()
        self.lockout_deny_input.setMaximumWidth(60)
        lockout_row.addWidget(self.lockout_deny_input)
        lockout_row.addSpacing(18)
        lockout_row.addWidget(QLabel("Unlock time (seconds, 0 = manual):"))
        self.lockout_unlock_input = QLineEdit()
        self.lockout_unlock_input.setMaximumWidth(80)
        lockout_row.addWidget(self.lockout_unlock_input)
        lockout_row.addStretch()
        layout.addWidget(lockout_group)

        # -- Sudo --
        sudo_group = QGroupBox("Sudo")
        sudo_row = QHBoxLayout(sudo_group)
        sudo_row.addWidget(QLabel("Session timeout (minutes):"))
        self.sudo_timeout_input = QLineEdit()
        self.sudo_timeout_input.setMaximumWidth(60)
        sudo_row.addWidget(self.sudo_timeout_input)
        sudo_row.addSpacing(18)
        self.sudo_require_password = QCheckBox("Require password for sudo")
        sudo_row.addWidget(self.sudo_require_password)
        sudo_row.addStretch()
        layout.addWidget(sudo_group)

        # -- Umask --
        umask_group = QGroupBox("Umask")
        umask_row = QHBoxLayout(umask_group)
        umask_row.addWidget(QLabel("Default umask (octal, e.g. 027):"))
        self.umask_input = QLineEdit()
        self.umask_input.setMaximumWidth(60)
        umask_row.addWidget(self.umask_input)
        umask_row.addStretch()
        layout.addWidget(umask_group)

        buttons = QHBoxLayout()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh_defaults)
        self.save_defaults_btn = QPushButton("Save Policy Defaults")
        self.save_defaults_btn.clicked.connect(self.save_defaults)
        buttons.addWidget(refresh_btn)
        buttons.addWidget(self.save_defaults_btn)
        layout.addLayout(buttons)

        self.defaults_status_label = QLabel("")
        layout.addWidget(self.defaults_status_label)

    def refresh_defaults(self):
        try:
            policy = api.get_environmental_policy()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return
        self._fill_defaults(policy)
        self.defaults_status_label.setText("")

    def _fill_defaults(self, policy):
        password = policy.get("password") or {}
        lockout = policy.get("lockout") or {}
        sudo = policy.get("sudo") or {}

        self.pw_minlen_input.setText(str(password.get("minlen", 12)))
        self.pw_retry_input.setText(str(password.get("retry", 3)))
        self.pw_require_upper.setChecked(password.get("ucredit", -1) < 0)
        self.pw_require_lower.setChecked(password.get("lcredit", -1) < 0)
        self.pw_require_digit.setChecked(password.get("dcredit", -1) < 0)
        self.pw_require_symbol.setChecked(password.get("ocredit", -1) < 0)

        self.lockout_deny_input.setText(str(lockout.get("deny", 5)))
        self.lockout_unlock_input.setText(str(lockout.get("unlock_time", 900)))

        self.sudo_timeout_input.setText(str(sudo.get("timestamp_timeout", 15)))
        self.sudo_require_password.setChecked(sudo.get("require_password", True))

        self.umask_input.setText(policy.get("umask", "027"))

    def _collect_policy_from_fields(self):
        """Returns (policy_dict, error_message) - error_message is None
        when every field parsed cleanly, policy_dict is None otherwise."""
        try:
            minlen = int(self.pw_minlen_input.text().strip())
            retry = int(self.pw_retry_input.text().strip())
            deny = int(self.lockout_deny_input.text().strip())
            unlock_time = int(self.lockout_unlock_input.text().strip())
            timestamp_timeout = int(self.sudo_timeout_input.text().strip())
        except ValueError:
            return None, "Length, retry, deny, unlock time, and sudo timeout must all be whole numbers."

        umask = self.umask_input.text().strip()
        if not re.fullmatch(r"[0-7]{3,4}", umask):
            return None, "Umask must be an octal value like 027 or 0027."

        policy = {
            "password": {
                "minlen": minlen,
                "retry": retry,
                "ucredit": -1 if self.pw_require_upper.isChecked() else 0,
                "lcredit": -1 if self.pw_require_lower.isChecked() else 0,
                "dcredit": -1 if self.pw_require_digit.isChecked() else 0,
                "ocredit": -1 if self.pw_require_symbol.isChecked() else 0,
            },
            "lockout": {
                "deny": deny,
                "unlock_time": unlock_time,
            },
            "sudo": {
                "timestamp_timeout": timestamp_timeout,
                "require_password": self.sudo_require_password.isChecked(),
            },
            "umask": umask,
        }
        return policy, None

    def save_defaults(self):
        policy, error = self._collect_policy_from_fields()
        if error:
            QMessageBox.warning(self, "Invalid value", error)
            return

        try:
            saved = api.set_environmental_policy(policy)
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        self._fill_defaults(saved)
        self.defaults_status_label.setText(
            "Saved - Generate Password / Set Password / Create User in User & Group "
            "Administration now enforce this baseline."
        )

    # =========================================================
    # SECTION 2: PUSH TO HOSTS
    # =========================================================
    def _build_push_section(self, layout):
        layout.addWidget(self._section_label("Push To Hosts"))

        hint = QLabel(
            "Applies whichever of the saved Policy Defaults above are checked below "
            "to the checked hosts' actual configuration (pwquality.conf, "
            "faillock.conf, sudoers.d, login.defs). Save Policy Defaults first if "
            "you just changed something - this pushes whatever was last saved, not "
            "whatever is currently typed above."
        )
        theme.style_hint_label(hint)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # Target Hosts checklist itself now lives in the left-column panel
        # built in __init__ (see #352, client/host_panel.py) - this section
        # just keeps the "which policies + push button" controls that act
        # on whatever's checked over there.

        which_group = QGroupBox("Policies to push")
        which_row = QHBoxLayout(which_group)
        self.push_password = QCheckBox("Password Quality")
        self.push_password.setChecked(True)
        self.push_lockout = QCheckBox("Lockout")
        self.push_lockout.setChecked(True)
        self.push_sudo = QCheckBox("Sudo")
        self.push_umask = QCheckBox("Umask")
        for cb in (self.push_password, self.push_lockout, self.push_sudo, self.push_umask):
            which_row.addWidget(cb)
        which_row.addStretch()
        layout.addWidget(which_group)

        self.push_btn = QPushButton("Push Selected Policies to Checked Hosts")
        self.push_btn.setStyleSheet("font-weight:bold;")
        self.push_btn.clicked.connect(self.push_to_hosts)
        layout.addWidget(self.push_btn)

        self.push_status = QLabel("")
        self.push_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        layout.addWidget(self.push_status)

        # One tab per host instead of a host-list-plus-single-output-panel -
        # same fix as System Health & Logs / Service Management, for the
        # same reason: a shared panel only ever shows whichever host was
        # last clicked, which is the "can't see two hosts' results at
        # once" problem.
        self.push_tabs = QTabWidget()
        self.push_tabs.setTabsClosable(True)
        self.push_tabs.tabCloseRequested.connect(self._close_push_tab)
        shrink_tabwidget_to_current_page(self.push_tabs)
        layout.addWidget(self.push_tabs)

    def checked_entries(self):
        entries = []
        for i in range(self.host_list.count()):
            item = self.host_list.item(i)
            entry = item.data(Qt.UserRole)
            if entry is None:
                continue
            if item.checkState() == Qt.Checked:
                entries.append(entry)
        return entries

    def load_hosts(self):
        checked = {_entry_key(e) for e in self.checked_entries()}

        try:
            entries = api.list_merged_hosts()
        except Exception as e:
            self.push_status.setStyleSheet(f"color: {STATUS_ERROR_COLOR};")
            self.push_status.setText(f"Could not load hosts: {e}")
            return

        self.host_list.blockSignals(True)
        self.host_list.clear()
        for e in entries:
            label = f"{e['label']}  [{e['type_text']}]"
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if _entry_key(e) in checked else Qt.Unchecked)
            item.setData(Qt.UserRole, e)
            self.host_list.addItem(item)
        self.host_list.blockSignals(False)

    def select_all_hosts(self):
        for i in range(self.host_list.count()):
            self.host_list.item(i).setCheckState(Qt.Checked)

    def deselect_all_hosts(self):
        for i in range(self.host_list.count()):
            self.host_list.item(i).setCheckState(Qt.Unchecked)

    def push_to_hosts(self):
        entries = self.checked_entries()
        if not entries:
            QMessageBox.information(self, "No hosts checked", "Check one or more target hosts first.")
            return

        if not (self.push_password.isChecked() or self.push_lockout.isChecked()
                or self.push_sudo.isChecked() or self.push_umask.isChecked()):
            QMessageBox.information(self, "Nothing selected", "Check at least one policy to push above.")
            return

        try:
            policy = api.get_environmental_policy()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        commands = []
        if self.push_password.isChecked():
            p = policy.get("password") or {}
            commands.append(api.cmd_set_password_quality_policy(
                minlen=p.get("minlen"), retry=p.get("retry"), dcredit=p.get("dcredit"),
                ucredit=p.get("ucredit"), lcredit=p.get("lcredit"), ocredit=p.get("ocredit"),
            ))
        if self.push_lockout.isChecked():
            l = policy.get("lockout") or {}
            commands.append(api.cmd_set_account_lockout_policy(
                deny=l.get("deny"), unlock_time=l.get("unlock_time"),
            ))
        if self.push_sudo.isChecked():
            s = policy.get("sudo") or {}
            commands.append(api.cmd_set_sudo_policy(
                timestamp_timeout=s.get("timestamp_timeout"), require_password=s.get("require_password"),
            ))
        if self.push_umask.isChecked():
            commands.append(api.cmd_set_umask_policy(policy.get("umask", "027")))

        command = " && ".join(commands)

        self.push_results = {}
        self.push_pending = {}
        self.push_tabs.clear()

        for entry in entries:
            key = _entry_key(entry)
            result = api.run_on_entry(entry, command)

            if result["sync"]:
                self.push_results[key] = {
                    "label": entry["label"], "stdout": result["stdout"],
                    "stderr": result["stderr"] or result["error"] or "",
                    "code": result["code"], "pending": False,
                }
            elif result["error"]:
                self.push_results[key] = {
                    "label": entry["label"], "stdout": "", "stderr": result["error"],
                    "code": None, "pending": False,
                }
            else:
                self.push_results[key] = {
                    "label": entry["label"], "stdout": "", "stderr": "",
                    "code": None, "pending": True,
                }
                self.push_pending[key] = (entry, result["task_id"])

            self._add_push_tab(key)

        self.push_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        self.push_status.setText(f"Pushing to {len(entries)} host(s)...")

        if self.push_tabs.count() > 0:
            self.push_tabs.setCurrentIndex(0)

        if self.push_pending:
            self.push_poll_timer.start(PUSH_POLL_MS)
        else:
            self.push_status.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
            self.push_status.setText("Push complete.")

    def _status_text(self, data):
        if data["pending"]:
            return "pending..."
        # Success is the exit code where we have one (the same rule the
        # result banner uses); stderr alone is NOT failure - many commands
        # write progress/warnings to stderr on success.
        code = data.get("code")
        failed = (code != 0) if code is not None else (bool(data["stderr"]) and not data["stdout"])
        return "error" if failed else "ok"

    def _render_push_result(self, text_edit, data):
        if data["pending"]:
            text_edit.setPlainText("Waiting for this agent host to report back...")
            return

        # The policy push commands mostly succeed silently, so a banner
        # makes the outcome clear at a glance (green = applied, red = failed).
        code = data["code"]
        if code is not None:
            failed = code != 0
        else:
            failed = bool(data["stderr"]) and not data["stdout"]

        bg = STATUS_ERROR_COLOR if failed else STATUS_SUCCESS_COLOR
        headline = "✗ Push failed" if failed else "✓ Policies applied"
        if code is not None:
            headline += f" (exit {code})"
        banner = (
            f'<div style="background-color:{bg}; color:#ffffff; font-weight:bold; '
            f'padding:5px 10px; border-radius:4px; margin:0 0 6px 0;">{html.escape(headline)}</div>'
        )

        text = data["stdout"] or "(no output - command succeeded silently)"
        if data["stderr"]:
            text += f"\n\n--- stderr ---\n{data['stderr']}"
        body = (
            f'<pre style="font-family:monospace; white-space:pre-wrap; margin:0;">'
            f'{html.escape(text)}</pre>'
        )
        text_edit.setHtml(banner + body)

    def _close_push_tab(self, index):
        bar = self.push_tabs.tabBar()
        key = bar.tabData(index)
        self.push_tabs.removeTab(index)
        self.push_results.pop(key, None)
        self.push_pending.pop(key, None)

    def _add_push_tab(self, key):
        data = self.push_results.get(key)
        if not data:
            return
        status = self._status_text(data)

        text_edit = QTextEdit()
        text_edit.setReadOnly(True)
        text_edit.setStyleSheet("font-family:monospace;")
        self._render_push_result(text_edit, data)

        idx = self.push_tabs.addTab(text_edit, f"{data['label']}  [{status}]")
        self.push_tabs.tabBar().setTabData(idx, key)

    def _refresh_push_tab(self, key):
        bar = self.push_tabs.tabBar()
        for i in range(self.push_tabs.count()):
            if bar.tabData(i) != key:
                continue
            data = self.push_results.get(key)
            if data:
                self.push_tabs.setTabText(i, f"{data['label']}  [{self._status_text(data)}]")
                self._render_push_result(self.push_tabs.widget(i), data)
            return

    def _poll_push(self):
        if not self.push_pending:
            self.push_poll_timer.stop()
            return

        done = []
        for key, (entry, task_id) in list(self.push_pending.items()):
            result = api.poll_entry_result(entry, task_id)
            if result is None:
                continue

            self.push_results[key] = {
                "label": entry["label"], "stdout": result["stdout"],
                "stderr": result["stderr"] or result["error"] or "",
                "code": result["code"], "pending": False,
            }
            self._refresh_push_tab(key)
            done.append(key)

        for key in done:
            del self.push_pending[key]

        if not self.push_pending:
            self.push_poll_timer.stop()
            self.push_status.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
            self.push_status.setText("All hosts reported back.")

    def refresh(self):
        self.refresh_defaults()
        self.load_hosts()
