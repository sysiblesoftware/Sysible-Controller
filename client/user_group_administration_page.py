import time

from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout,
    QListWidget, QListWidgetItem, QLabel, QPushButton,
    QLineEdit, QTextEdit, QComboBox, QMessageBox,
    QDialog, QScrollArea, QTabWidget, QDateEdit, QApplication,
    QSizePolicy,
)
from PySide6.QtCore import Qt, QTimer, QDate
from PySide6.QtGui import QColor

from client import api
from client.events import bus
from client import theme
from client.theme import STATUS_NEUTRAL_COLOR, STATUS_SUCCESS_COLOR, STATUS_ERROR_COLOR, STATUS_WARNING_COLOR
from client.branding import make_page_header
from client.tab_sizing import shrink_tabwidget_to_current_page
from client.host_panel import build_host_panel
from client.collapsible_groups import (
    make_group_header_item, apply_collapse_state, get_collapsed_groups,
    connect_group_toggle, add_collapse_expand_buttons,
)

SYNC_POLL_MS = 2000
SYNC_TIMEOUT_S = 60
HOST_REFRESH_MS = 10000
AUTO_RESYNC_DELAY_MS = 4000
DISPATCH_POLL_MS = 2000


def _entry_key(entry):
    """Hashable identity for a merged-host entry (api.list_merged_hosts()) -
    agent host_ids and SSH host names live in separate namespaces, so the
    kind has to be part of the key."""
    return (entry["kind"], entry["id"])


class _CommandReportDialog(QDialog):
    """Read-only popup for multi-host report commands (privileged-user
    audit, group listing) - dispatches the same way dispatch() does for
    writes (SSH resolves immediately, agent hosts are polled), but just
    displays whatever each host returns instead of triggering a resync."""

    def __init__(self, parent, title, entries, command):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(700, 500)

        self.entries = entries
        self.command = command
        self.pending = {}   # entry_key -> (entry, task_id)
        self.output = {}    # entry_key -> (label, text)

        layout = QVBoxLayout(self)

        self.status_label = QLabel("Running...")
        layout.addWidget(self.status_label)

        self.text = QTextEdit()
        self.text.setReadOnly(True)
        layout.addWidget(self.text)

        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        layout.addWidget(btn_close)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)

        self._run()

    def _run(self):
        for entry in self.entries:
            key = _entry_key(entry)
            result = api.run_on_entry(entry, self.command)

            if result["sync"]:
                text = result["stdout"] or result["stderr"] or result["error"] or "(no output)"
                self.output[key] = (entry["label"], text)
            elif result["error"]:
                self.output[key] = (entry["label"], f"ERROR: {result['error']}")
            else:
                self.pending[key] = (entry, result["task_id"])

        self._render()

        if self.pending:
            self.timer.start(DISPATCH_POLL_MS)
        else:
            self.status_label.setText("Done.")

    def _poll(self):
        done = []
        for key, (entry, task_id) in list(self.pending.items()):
            result = api.poll_entry_result(entry, task_id)
            if result is None:
                continue
            done.append(key)
            if result["error"]:
                self.output[key] = (entry["label"], f"ERROR: {result['error']}")
            else:
                text = result["stdout"] or result["stderr"] or "(no output)"
                self.output[key] = (entry["label"], text)

        for key in done:
            del self.pending[key]

        self._render()

        if not self.pending:
            self.timer.stop()
            self.status_label.setText("Done.")
        else:
            self.status_label.setText(f"Running... ({len(self.pending)} host(s) still pending)")

    def _render(self):
        parts = []
        for label, text in sorted(self.output.values(), key=lambda pair: pair[0]):
            parts.append(f"=== {label} ===\n{text.strip()}\n")
        self.text.setPlainText("\n".join(parts))


class _GeneratedPasswordDialog(QDialog):
    """Shown right after Generate fills a password field. The field
    itself still gets the value (Set Password / Create User act on it
    same as before), but it's small, easy to misclick out of, and
    switches back to dot-masked the moment focus moves elsewhere -
    plenty of ways to lose a freshly generated password before it's
    actually used. This pins it on screen, in the clear, with its own
    one-click copy, until it's explicitly dismissed."""

    def __init__(self, parent, password):
        super().__init__(parent)
        self.setWindowTitle("Generated Password")
        self.setModal(True)

        layout = QVBoxLayout(self)

        hint = QLabel("Copy this password now - it won't be shown again.")
        layout.addWidget(hint)

        row = QHBoxLayout()
        self.password_field = QLineEdit(password)
        self.password_field.setReadOnly(True)
        self.password_field.setMinimumWidth(260)
        row.addWidget(self.password_field)

        btn_copy = QPushButton("Copy to Clipboard")
        btn_copy.clicked.connect(self._copy)
        row.addWidget(btn_copy)
        layout.addLayout(row)

        self.copied_label = QLabel("")
        self.copied_label.setStyleSheet(f"color:{STATUS_SUCCESS_COLOR};")
        layout.addWidget(self.copied_label)

        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        layout.addWidget(btn_close)

        self.password_field.selectAll()

    def _copy(self):
        QApplication.clipboard().setText(self.password_field.text())
        self.copied_label.setText("Copied.")


class UserGroupAdministrationPage(QWidget):
    """
    User & Group Administration against a *merged* host list (agent-
    enrolled hosts AND SSH-enrolled hosts).

    Split out of the original combined System Administration page so it
    opens as its own focused window from the System Administration menu,
    rather than living behind a tab next to System Health & Logs.
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("User & Group Administration")
        self.resize(1290, 760)

        self.host_data = {}         # entry_key -> {users, groups, sessions, synced_at, error}
        self.active_entry = None
        self.selected_user = None
        self.pending_sync = {}      # entry_key -> (entry, task_id)
        self.sync_total = 0
        self.sync_deadline = None

        self.pending_dispatch = {}  # entry_key -> (entry, task_id, label)

        main = QVBoxLayout()
        self.setLayout(main)

        main.addLayout(make_page_header("User & Group Administration"))

        body = QHBoxLayout()

        # =========================================================
        # TARGET HOSTS (agent + SSH, merged) - left column, full height
        # =========================================================
        self.host_list = QListWidget()
        self.host_list.itemClicked.connect(self.on_host_selected)
        connect_group_toggle(self.host_list)

        btn_refresh_hosts = QPushButton("Refresh Hosts")
        btn_refresh_hosts.clicked.connect(self.load_hosts)

        btn_select_all = QPushButton("Select All")
        btn_select_all.clicked.connect(self.select_all_hosts)

        btn_deselect_all = QPushButton("Deselect All")
        btn_deselect_all.clicked.connect(self.deselect_all_hosts)

        self.btn_sync = QPushButton("Sync Checked Hosts")
        self.btn_sync.clicked.connect(self.sync_checked_hosts)

        btn_collapse_all, btn_expand_all = add_collapse_expand_buttons(self.host_list)

        self.sync_status = QLabel("Check one or more hosts, then Sync to pull live user data.")
        self.sync_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        self.sync_status.setWordWrap(True)

        host_panel = build_host_panel(
            "Target Hosts (agent-managed)",
            self.host_list,
            [
                [btn_refresh_hosts, btn_select_all, btn_deselect_all],
                [self.btn_sync],
                [btn_collapse_all, btn_expand_all],
            ],
            extra_widgets=[self.sync_status],
        )
        body.addWidget(host_panel)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)

        # =========================================================
        # USER & GROUP PANEL
        # =========================================================
        content_layout.addWidget(self._build_users_panel(), 1)

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

        self.sync_timer = QTimer()
        self.sync_timer.timeout.connect(self._poll_sync)

        self.dispatch_poll_timer = QTimer()
        self.dispatch_poll_timer.timeout.connect(self._poll_dispatch)

        bus.host_removed.connect(self.load_hosts)

    # =========================================================
    # PANEL BUILDER
    # =========================================================
    def _build_users_panel(self):
        panel = QWidget()
        split = QHBoxLayout(panel)
        split.setContentsMargins(5, 5, 5, 5)
        split.setSpacing(6)

        # ---------------- LEFT PANEL ----------------
        left = QVBoxLayout()

        self.active_host_label = QLabel("Viewing: (no host selected)")
        self.active_host_label.setStyleSheet("font-weight: bold;")

        # No manual "Refresh" button here anymore - every action below
        # (lock, unlock, set password, etc.) auto-resyncs the host it
        # ran on a few seconds after it completes (see dispatch()'s
        # auto_resync below), and refresh_user_panel_from_cache() now
        # keeps whoever was selected still selected through that
        # resync, so there's nothing left for a manual refresh to do
        # that isn't already happening on its own.
        host_label_row = QHBoxLayout()
        host_label_row.addWidget(self.active_host_label)
        host_label_row.addStretch()

        self.user_search = QLineEdit()
        self.user_search.setPlaceholderText("Search users...")
        self.user_search.setMaximumWidth(280)
        self.user_search.textChanged.connect(self.filter_users)

        users_label = QLabel("Users (on selected host)")
        users_label.setStyleSheet("font-weight: bold;")

        self.user_list = QListWidget()
        self.user_list.itemClicked.connect(self.on_select_user)

        left.addLayout(host_label_row)
        left.addWidget(self.user_search)
        left.addWidget(users_label)
        left.addWidget(self.user_list)

        split.addLayout(left, 2)

        # ---------------- RIGHT PANEL ----------------
        # Tabbed instead of one long scrolled column - the old layout
        # put everything from session/sudo info down through host-wide
        # PAM policy in a single vertical scroll, so reaching whatever
        # you actually came here for (often the policy section at the
        # very bottom) meant scrolling past everything else first.
        # Grouped by what each tab is for; widget attribute names are
        # unchanged so every action method below still works as-is.
        tabs = QTabWidget()
        tabs.addTab(self._build_create_user_tab(), "Create User")
        tabs.addTab(self._build_account_tab(), "Account")
        tabs.addTab(self._build_password_tab(), "Password")
        tabs.addTab(self._build_groups_tab(), "Groups")
        tabs.addTab(self._build_reports_tab(), "Reports")
        # Per-page sizing so the stack tracks the visible tab, but NO height
        # cap here: unlike the System Administration tool pages, this right
        # column has no results panel below the tabs (just a one-line status
        # label), so there's nothing for a cap to free up space for. Capping
        # to the current page's sizeHint under-measured the taller tabs
        # (Account), giving them a scrollbar *and* a block of dead space below.
        # Letting the tab widget expand to fill the column fixes both.
        shrink_tabwidget_to_current_page(tabs)
        tabs.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        # Kept as an attribute (not just a local) so the dashboard's
        # feature search bar (client/home.py / client/feature_search.py)
        # can jump straight to a specific tab - e.g. typing "create a
        # user" opens this page already on the Create User tab instead
        # of leaving the admin to find it themselves.
        self.tabs = tabs

        self.dispatch_status = QLabel()
        self.dispatch_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        self.dispatch_status.setWordWrap(True)

        right = QVBoxLayout()
        right.addWidget(tabs)
        right.addWidget(self.dispatch_status)
        # No trailing stretch: the tab widget is set to Expanding above and
        # claims the column's leftover height itself, so there's no empty
        # block below it. (A stretch here would fight the expanding tabs for
        # that space and reintroduce the dead gap.)

        split.addLayout(right, 3)
        return panel

    @staticmethod
    def _scrollable(widget):
        # Each tab still gets its own scroll area as a safety net in
        # case a window is resized very small - but in normal sizes
        # a tab's content now fits without scrolling at all.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(widget)
        return scroll

    def _build_create_user_tab(self):
        """Its own tab rather than buried at the bottom of Details &&
        Groups (where it used to live, after Account Details and Group
        Management) - creating a user doesn't depend on one being
        selected first, unlike every other tab here, so it's the first
        tab and doesn't need a selected_user to be usable."""
        content = QWidget()
        right = QVBoxLayout(content)
        right.setSpacing(6)

        create_label = QLabel("Create User")
        create_label.setStyleSheet("font-weight: bold;")
        create_hint = QLabel("Creates the account on every currently checked host.")
        theme.style_hint_label(create_hint)
        create_hint.setWordWrap(True)
        right.addWidget(create_label)
        right.addWidget(create_hint)

        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("Username")
        self.username_input.setMaximumWidth(280)

        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("Password (optional)")
        self.password_input.setEchoMode(QLineEdit.Password)
        self.password_input.setMaximumWidth(220)
        btn_generate_create_password = QPushButton("Generate")
        btn_generate_create_password.setToolTip(
            "Fill in a random password that meets the strength requirement."
        )
        btn_generate_create_password.clicked.connect(
            lambda: self._fill_generated_password(self.password_input)
        )
        create_password_row = QHBoxLayout()
        create_password_row.addWidget(self.password_input)
        create_password_row.addWidget(btn_generate_create_password)

        self.shell_input = QLineEdit()
        self.shell_input.setPlaceholderText("/bin/bash")
        self.shell_input.setMaximumWidth(220)

        btn_create_user = QPushButton("Create User")
        btn_create_user.clicked.connect(self.create_user)

        right.addWidget(QLabel("Username"))
        right.addWidget(self.username_input)
        right.addWidget(QLabel("Password"))
        right.addLayout(create_password_row)
        right.addWidget(QLabel("Shell"))
        right.addWidget(self.shell_input)
        right.addWidget(btn_create_user)

        right.addStretch()
        return self._scrollable(content)

    def _build_account_tab(self):
        content = QWidget()
        right = QVBoxLayout(content)
        right.setSpacing(6)

        self.account_info = QLabel()
        right.addWidget(self.account_info)

        status_row = QHBoxLayout()
        self.status_label = QLabel()
        status_row.addWidget(self.status_label)
        status_row.addStretch()
        self.btn_status_by_host = QPushButton("View Status by Host...")
        self.btn_status_by_host.setFixedWidth(160)
        self.btn_status_by_host.setEnabled(False)
        self.btn_status_by_host.clicked.connect(self.show_status_by_host)
        status_row.addWidget(self.btn_status_by_host)
        right.addLayout(status_row)

        details_label = QLabel("Account Details (/etc/passwd)")
        details_label.setStyleSheet("font-weight: bold;")

        current_shell_row = QHBoxLayout()
        current_shell_title = QLabel("Current Shell:")
        current_shell_title.setStyleSheet("font-weight: bold;")
        self.current_shell_label = QLabel("-")
        current_shell_row.addWidget(current_shell_title)
        current_shell_row.addWidget(self.current_shell_label)
        current_shell_row.addStretch()

        self.account_shell_input = QLineEdit()
        self.account_shell_input.setPlaceholderText("Shell, e.g. /bin/bash")
        self.account_shell_input.setMaximumWidth(220)
        btn_set_shell = QPushButton("Set Shell")
        btn_set_shell.clicked.connect(self.set_user_shell)
        shell_row = QHBoxLayout()
        shell_row.addWidget(self.account_shell_input)
        shell_row.addWidget(btn_set_shell)

        self.account_comment_input = QLineEdit()
        self.account_comment_input.setPlaceholderText("Full name / comment (GECOS)")
        self.account_comment_input.setMaximumWidth(320)
        btn_set_comment = QPushButton("Set Full Name")
        btn_set_comment.clicked.connect(self.set_user_comment)
        comment_row = QHBoxLayout()
        comment_row.addWidget(self.account_comment_input)
        comment_row.addWidget(btn_set_comment)

        right.addWidget(details_label)
        right.addLayout(current_shell_row)
        right.addLayout(shell_row)
        right.addLayout(comment_row)

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

        action_row = QHBoxLayout()
        self.btn_lock = QPushButton("Lock")
        self.btn_unlock = QPushButton("Unlock")
        self.btn_sudo = QPushButton("Toggle Sudo")
        self.btn_kill_sessions = QPushButton("Kill Sessions")
        self.btn_delete = QPushButton("Delete User")
        for b in [self.btn_lock, self.btn_unlock, self.btn_sudo, self.btn_kill_sessions, self.btn_delete]:
            b.setFixedWidth(110)
            action_row.addWidget(b)
        self.btn_lock.clicked.connect(self.lock_user)
        self.btn_unlock.clicked.connect(self.unlock_user)
        self.btn_sudo.clicked.connect(self.toggle_sudo)
        self.btn_kill_sessions.clicked.connect(self.kill_sessions)
        self.btn_delete.clicked.connect(self.delete_user)
        right.addLayout(action_row)

        # "Delete User" above only acts on whichever hosts are
        # currently checked - this one ignores the checklist entirely
        # and removes the account from every managed host (agent +
        # SSH), for the case where someone needs to be fully cut off
        # rather than cleaned up on a couple of machines. Styled and
        # worded to stand apart from the routine actions above it, and
        # gated by its own warning dialog in terminate_user().
        btn_terminate = QPushButton("Terminate User (Remove From ALL Hosts)")
        btn_terminate.setStyleSheet(f"color:{STATUS_ERROR_COLOR}; font-weight:bold;")
        btn_terminate.clicked.connect(self.terminate_user)
        right.addWidget(btn_terminate)

        right.addStretch()
        return self._scrollable(content)

    def _build_password_tab(self):
        content = QWidget()
        right = QVBoxLayout(content)
        right.setSpacing(6)

        pw_label = QLabel("Set Password")
        pw_label.setStyleSheet("font-weight: bold;")
        right.addWidget(pw_label)

        pw_hint = QLabel(
            "Enforces the password policy set in System Administration "
            "> Environmental Policies (default: at least 12 characters, "
            "with uppercase, lowercase, a digit, and a symbol)."
        )
        theme.style_hint_label(pw_hint)
        pw_hint.setWordWrap(True)
        right.addWidget(pw_hint)

        pw_row = QHBoxLayout()
        self.new_password_input = QLineEdit()
        self.new_password_input.setPlaceholderText("New password")
        self.new_password_input.setEchoMode(QLineEdit.Password)
        self.new_password_input.setMaximumWidth(220)
        btn_generate_password = QPushButton("Generate")
        btn_generate_password.setToolTip(
            "Fill in a random password that already meets the strength "
            "requirement above."
        )
        btn_generate_password.clicked.connect(
            lambda: self._fill_generated_password(self.new_password_input)
        )
        btn_set_password = QPushButton("Set Password")
        btn_set_password.clicked.connect(self.set_password)
        btn_force_reset = QPushButton("Force Reset Next Login")
        btn_force_reset.clicked.connect(self.force_password_reset)
        pw_row.addWidget(self.new_password_input)
        pw_row.addWidget(btn_generate_password)
        pw_row.addWidget(btn_set_password)
        pw_row.addWidget(btn_force_reset)
        right.addLayout(pw_row)

        aging_label = QLabel("Password Aging (days, blank = leave unchanged)")
        aging_label.setStyleSheet("font-weight: bold;")
        self.aging_max_input = QLineEdit()
        self.aging_max_input.setPlaceholderText("Max")
        self.aging_max_input.setMaximumWidth(80)
        self.aging_max_input.setToolTip(
            "Maximum number of days a password is valid before it must be changed."
        )
        self.aging_min_input = QLineEdit()
        self.aging_min_input.setPlaceholderText("Min")
        self.aging_min_input.setMaximumWidth(80)
        self.aging_min_input.setToolTip(
            "Minimum number of days that must pass before the password "
            "can be changed again."
        )
        self.aging_warn_input = QLineEdit()
        self.aging_warn_input.setPlaceholderText("Warn")
        self.aging_warn_input.setMaximumWidth(80)
        self.aging_warn_input.setToolTip(
            "How many days before expiration the user starts seeing a "
            "warning to change their password."
        )
        btn_apply_aging = QPushButton("Apply Aging")
        btn_apply_aging.clicked.connect(self.apply_password_aging)
        aging_row = QHBoxLayout()
        aging_row.addWidget(self.aging_max_input)
        aging_row.addWidget(self.aging_min_input)
        aging_row.addWidget(self.aging_warn_input)
        aging_row.addWidget(btn_apply_aging)
        right.addWidget(aging_label)
        right.addLayout(aging_row)

        expire_label = QLabel("Account Expiration")
        expire_label.setStyleSheet("font-weight: bold;")
        self.expire_combo = QComboBox()
        self.expire_combo.addItem("30 days", 30)
        self.expire_combo.addItem("60 days", 60)
        self.expire_combo.addItem("90 days", 90)
        self.expire_combo.addItem("180 days", 180)
        self.expire_combo.addItem("1 year", 365)
        self.expire_combo.addItem("Never", "never")
        self.expire_combo.addItem("Custom date...", "custom")
        self.expire_combo.setToolTip(
            "Days are counted from today. \"Never\" clears any existing "
            "expiration date."
        )
        self.expire_combo.currentIndexChanged.connect(self._on_expire_choice_changed)
        self.expire_combo.setMaximumWidth(160)
        self.expire_date_edit = QDateEdit()
        self.expire_date_edit.setCalendarPopup(True)
        self.expire_date_edit.setDisplayFormat("yyyy-MM-dd")
        self.expire_date_edit.setDate(QDate.currentDate().addDays(30))
        self.expire_date_edit.setVisible(False)
        self.expire_date_edit.setMaximumWidth(140)
        btn_set_expiration = QPushButton("Set Expiration")
        btn_set_expiration.clicked.connect(self.set_account_expiration)
        expire_row = QHBoxLayout()
        expire_row.addWidget(self.expire_combo)
        expire_row.addWidget(self.expire_date_edit)
        expire_row.addWidget(btn_set_expiration)
        right.addWidget(expire_label)
        right.addLayout(expire_row)

        right.addStretch()
        return self._scrollable(content)

    def _build_groups_tab(self):
        content = QWidget()
        right = QVBoxLayout(content)
        right.setSpacing(6)

        group_label = QLabel("Group Management")
        group_label.setStyleSheet("font-weight: bold;")
        self.group_dropdown = QComboBox()
        self.group_dropdown.setMaximumWidth(200)
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

        self.new_group_input = QLineEdit()
        self.new_group_input.setPlaceholderText("New group name")
        self.new_group_input.setMaximumWidth(220)
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

        right.addStretch()
        return self._scrollable(content)

    def _build_reports_tab(self):
        content = QWidget()
        right = QVBoxLayout(content)
        right.setSpacing(6)

        reports_label = QLabel("Reports (checked hosts)")
        reports_label.setStyleSheet("font-weight: bold;")
        btn_audit = QPushButton("Audit Privileged Users")
        btn_audit.clicked.connect(self.audit_privileged_users)
        btn_view_groups = QPushButton("View All Groups && Members")
        btn_view_groups.clicked.connect(self.view_all_groups)
        reports_row = QHBoxLayout()
        reports_row.addWidget(btn_audit)
        reports_row.addWidget(btn_view_groups)
        right.addWidget(reports_label)
        right.addLayout(reports_row)

        right.addStretch()
        return self._scrollable(content)

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
            self.sync_status.setStyleSheet(f"color: {STATUS_ERROR_COLOR};")
            self.sync_status.setText(f"Could not load hosts: {e}")
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

        # QListWidget.clear() above wipes the visual "current item"
        # highlight even though the checkbox check-states (preserved
        # via `checked`) and self.active_entry both survive the
        # rebuild untouched - restore the highlight here so selecting
        # a host and then refreshing/syncing doesn't visually look
        # like the selection was lost.
        if self.active_entry is not None:
            target_key = _entry_key(self.active_entry)
            for i in range(self.host_list.count()):
                item = self.host_list.item(i)
                entry = item.data(Qt.UserRole)
                if entry is not None and _entry_key(entry) == target_key:
                    self.host_list.setCurrentItem(item)
                    break

        # Purge cached data for hosts that no longer exist.
        current_keys = {_entry_key(e) for e in entries}
        stale_keys = set(self.host_data.keys()) - current_keys

        for key in stale_keys:
            del self.host_data[key]
            self.pending_sync.pop(key, None)

        if self.active_entry is not None and _entry_key(self.active_entry) in stale_keys:
            self.active_entry = None
            self.active_host_label.setText("Viewing: (no host selected)")
            self.user_list.clear()
            self.selected_user = None
            self._clear_user_detail()
            self.group_dropdown.clear()

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

    def on_host_selected(self, item):
        entry = item.data(Qt.UserRole)
        if entry is None:
            return
        self.active_entry = entry
        self.active_host_label.setText(f"Viewing: {entry['label']} [{entry['type_text']}]")
        self.refresh_user_panel_from_cache()

    # =========================================================
    # SYNC (full read from checked hosts - agent + SSH)
    # =========================================================
    def sync_checked_hosts(self):
        entries = self.checked_entries()

        if not entries:
            QMessageBox.information(self, "No hosts checked", "Check one or more hosts to sync.")
            return

        self.pending_sync = {}
        synced_now = 0

        for entry in entries:
            key = _entry_key(entry)
            result = api.sync_entry_users(entry)

            if result["sync"]:
                self._store_sync_result(key, result)
                synced_now += 1
            elif result["error"]:
                self._store_sync_error(key, result["error"])
            else:
                self.pending_sync[key] = (entry, result["task_id"])

        self.sync_total = len(entries)
        self.sync_deadline = time.time() + SYNC_TIMEOUT_S
        self.sync_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        self.sync_status.setText(f"Syncing {synced_now}/{self.sync_total} hosts...")

        self.refresh_user_panel_from_cache()

        if self.pending_sync:
            self.sync_timer.start(SYNC_POLL_MS)
        else:
            self.sync_status.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
            self.sync_status.setText(f"Synced {synced_now}/{self.sync_total} hosts.")

    def _store_sync_result(self, key, result):
        data = result["data"] or {}
        self.host_data[key] = {
            "users": data.get("users", []),
            "groups": data.get("groups", []),
            "sessions": data.get("sessions", []),
            "synced_at": time.time(),
            "error": None,
        }

    def _store_sync_error(self, key, error):
        previous = self.host_data.get(key, {"users": [], "groups": [], "sessions": []})
        self.host_data[key] = {**previous, "error": error}

    def _poll_sync(self):
        if not self.pending_sync:
            self.sync_timer.stop()
            return

        done = []

        for key, (entry, task_id) in list(self.pending_sync.items()):
            result = api.poll_entry_sync_result(entry, task_id)

            if result is None:
                continue  # agent hasn't reported back yet

            done.append(key)

            if result["error"]:
                self._store_sync_error(key, result["error"])
            else:
                self._store_sync_result(key, {"data": result["data"]})

        for key in done:
            del self.pending_sync[key]

        synced_so_far = self.sync_total - len(self.pending_sync)
        self.sync_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        self.sync_status.setText(f"Syncing {synced_so_far}/{self.sync_total} hosts...")

        timed_out = bool(self.sync_deadline) and time.time() > self.sync_deadline

        if not self.pending_sync or timed_out:
            self.sync_timer.stop()

            if timed_out and self.pending_sync:
                self.sync_status.setStyleSheet(f"color: {STATUS_ERROR_COLOR};")
                self.sync_status.setText(
                    f"Sync timed out waiting on {len(self.pending_sync)} host(s) "
                    f"(synced {synced_so_far}/{self.sync_total})."
                )
                self.pending_sync = {}
            else:
                self.sync_status.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
                self.sync_status.setText(f"Synced {synced_so_far}/{self.sync_total} hosts.")

            # Re-render once the sync batch lands. NOT gated on active_entry:
            # in the multi-host union/mismatch view you've only checked boxes
            # (no single active host), and that gate left the mismatch list
            # stale after adding a user to a missing host. refresh_user_panel_
            # from_cache() handles the no-data case itself.
            self.refresh_user_panel_from_cache()

    def _auto_resync(self, entries):
        # Background re-read after an action. Treat it as its OWN batch so the
        # "Synced X/Y" counter reflects just this resync - previously this
        # incremented self.sync_total without ever resetting it, so the total
        # crept up after every action and showed nonsense like "17/17 hosts"
        # on a 4-host fleet. Only add to the running total if a sync is
        # genuinely still in flight (timer active).
        fresh_batch = not self.sync_timer.isActive()
        if fresh_batch:
            self.sync_total = len(entries)
            self.sync_deadline = time.time() + SYNC_TIMEOUT_S

        for entry in entries:
            key = _entry_key(entry)
            result = api.sync_entry_users(entry)

            if result["sync"]:
                self._store_sync_result(key, result)
            elif result["error"]:
                self._store_sync_error(key, result["error"])
            else:
                self.pending_sync[key] = (entry, result["task_id"])
                if not fresh_batch:
                    self.sync_total += 1

        if self.pending_sync and not self.sync_timer.isActive():
            self.sync_timer.start(SYNC_POLL_MS)

        self.refresh_user_panel_from_cache()

    # =========================================================
    # USER PANEL (driven by cached sync data for the active host)
    # =========================================================
    def refresh_user_panel_from_cache(self):
        # Every action below (lock, unlock, set password, ...) calls
        # dispatch(), which auto-resyncs the host a few seconds later
        # and lands back here. Previously that unconditionally cleared
        # self.selected_user, so the user you'd just acted on visibly
        # deselected itself moments after every click. Remember who
        # was selected and put them right back if they're still
        # present, so the only time selection actually drops is when
        # that user no longer exists on the host (e.g. just deleted).
        previous_selection = self.selected_user

        self.selected_user = None
        self._clear_user_detail()
        self.load_groups_for_active_host()

        # The user list reflects every CHECKED host that has synced data, so
        # you can check two or more hosts and see all their users at once. If
        # nothing is checked, fall back to the single host you clicked to view.
        entries = self._user_panel_hosts()

        present = {}  # username -> set of host labels it exists on
        for e in entries:
            data = self.host_data.get(_entry_key(e), {})
            for u in data.get("users", []):
                present.setdefault(u["username"], set()).add(e["label"])
        self._user_present = present
        self._user_panel_entries = entries

        if not entries:
            self.user_list.clear()
            self.account_info.setText("No synced data yet - check one or more hosts and click Sync.")
            return

        self._render_user_list(self.user_search.text())
        self._set_sync_info(entries)

        if previous_selection and previous_selection in present:
            self._select_username_in_list(previous_selection)

    def _user_panel_hosts(self):
        """Hosts feeding the user list: all checked hosts with synced data,
        else the single active (clicked) host if it has data."""
        entries = [e for e in self.checked_entries()
                   if self.host_data.get(_entry_key(e))]
        if not entries and self.active_entry is not None \
                and self.host_data.get(_entry_key(self.active_entry)):
            entries = [self.active_entry]
        return entries

    def _add_user_header(self, text):
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemIsEnabled)  # shown, but not selectable
        f = item.font()
        f.setBold(True)
        item.setFont(f)
        item.setForeground(QColor(STATUS_NEUTRAL_COLOR))
        self.user_list.addItem(item)

    def _add_user_row(self, username, note=""):
        text = username if not note else f"{username}    ({note})"
        item = QListWidgetItem(text)
        item.setData(Qt.UserRole, username)  # real username, free of annotation
        self.user_list.addItem(item)

    def _render_user_list(self, filter_text=""):
        """(Re)draw the user list from self._user_present. One host: a flat
        list. Two or more: users present on every selected host first, then a
        'host mismatch' group for users that exist on only some of them."""
        self.user_list.clear()
        present = getattr(self, "_user_present", {})
        entries = getattr(self, "_user_panel_entries", [])
        ft = (filter_text or "").strip().lower()

        def show(u):
            return ft in u.lower()

        if len(entries) <= 1:
            for u in sorted(present):
                if show(u):
                    self._add_user_row(u)
            return

        all_hosts = {e["label"] for e in entries}
        consistent = sorted(u for u, s in present.items() if s == all_hosts and show(u))
        partial = sorted(u for u, s in present.items() if s != all_hosts and show(u))

        if consistent:
            self._add_user_header(f"On all {len(entries)} selected hosts  ({len(consistent)})")
            for u in consistent:
                self._add_user_row(u)
        if partial:
            self._add_user_header(f"Host mismatch — on some hosts only  ({len(partial)})")
            for u in partial:
                on = present[u]
                missing = all_hosts - on
                note = "on " + ", ".join(sorted(on))
                if missing:
                    note += "  —  missing on " + ", ".join(sorted(missing))
                self._add_user_row(u, note)

    def _set_sync_info(self, entries):
        times = [self.host_data.get(_entry_key(e), {}).get("synced_at") for e in entries]
        times = [t for t in times if t]
        errs = [e["label"] for e in entries
                if self.host_data.get(_entry_key(e), {}).get("error")]
        parts = []
        if len(entries) > 1:
            parts.append(f"{len(entries)} hosts selected")
        if times:
            parts.append("last synced " + time.strftime("%H:%M:%S", time.localtime(max(times))))
        if errs:
            parts.append(f"sync error on: {', '.join(errs)}")
        self.account_info.setText(" · ".join(parts))

    def _select_username_in_list(self, username):
        for i in range(self.user_list.count()):
            item = self.user_list.item(i)
            if item.data(Qt.UserRole) == username:
                self.user_list.setCurrentItem(item)
                self.selected_user = username
                self._ensure_active_has_user(username)
                self.load_user_detail(username)
                return

    def _ensure_active_has_user(self, username):
        """The detail panel loads from self.active_entry; in a multi-host
        view the clicked user may not exist on whatever host was last
        'viewed', so point active_entry at a selected host that does have it."""
        def has(entry):
            d = self.host_data.get(_entry_key(entry)) if entry else None
            return bool(d and any(u["username"] == username for u in d.get("users", [])))

        if has(self.active_entry):
            return
        for e in self._user_panel_hosts():
            if has(e):
                self.active_entry = e
                self.active_host_label.setText(f"Viewing: {e['label']} [{e['type_text']}]")
                self.load_groups_for_active_host()
                return

    def load_groups_for_active_host(self):
        self.group_dropdown.clear()
        if self.active_entry is None:
            return
        data = self.host_data.get(_entry_key(self.active_entry))
        if data:
            for g in data.get("groups", []):
                self.group_dropdown.addItem(g["name"])

    def filter_users(self, text):
        # Re-render the (possibly multi-host) list with the search filter
        # applied; the union map was built in refresh_user_panel_from_cache.
        self._render_user_list(text)

    def on_select_user(self, item):
        username = item.data(Qt.UserRole)
        if not username:
            return  # a group header row, not a user
        self.selected_user = username
        self._ensure_active_has_user(username)
        self.load_user_detail(username)

    def _clear_user_detail(self):
        self.account_info.setText("")
        self.status_label.setText("")
        self.status_label.setStyleSheet("")
        self.btn_status_by_host.setEnabled(False)
        self.btn_status_by_host.setToolTip("")
        self.sudo_status.setText("")
        self.user_groups_label.setText("")
        self.sessions.setPlainText("")
        self.current_shell_label.setText("-")
        self.account_shell_input.setText("")

    def _status_by_host(self, username):
        """For `username`, the lock status on every host this matters
        for: every currently checked host (since that's exactly the
        set Lock/Unlock above would actually run against) plus
        whichever host is actively being viewed, in case it isn't
        checked. Returns a list of (host_label, status) where status
        is "LOCKED"/"ACTIVE", or "NOT FOUND" (that host's last sync
        didn't have this user) / "NOT SYNCED" (no cached sync data for
        that host yet) when a real comparison isn't possible. This is
        the one place that answers "is this user's lock state actually
        the same everywhere I'm looking?" - load_user_detail()'s
        status_label uses it for the all-agree/MIXED verdict, and
        show_status_by_host()'s popup uses it for the full breakdown."""
        entries = list(self.checked_entries())
        keys = {_entry_key(e) for e in entries}

        if self.active_entry is not None and _entry_key(self.active_entry) not in keys:
            entries.append(self.active_entry)

        rows = []
        for entry in entries:
            data = self.host_data.get(_entry_key(entry))
            if not data:
                rows.append((entry["label"], "NOT SYNCED"))
                continue
            user = next((u for u in data.get("users", []) if u["username"] == username), None)
            if user is None:
                rows.append((entry["label"], "NOT FOUND"))
            else:
                rows.append((entry["label"], "LOCKED" if user.get("locked") else "ACTIVE"))
        return rows

    def show_status_by_host(self):
        if not self.selected_user:
            return

        rows = self._status_by_host(self.selected_user)

        colors = {
            "LOCKED": STATUS_ERROR_COLOR,
            "ACTIVE": STATUS_SUCCESS_COLOR,
            "NOT FOUND": STATUS_NEUTRAL_COLOR,
            "NOT SYNCED": STATUS_NEUTRAL_COLOR,
        }

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Status by host - {self.selected_user}")
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(f"Account status for '{self.selected_user}' by host:"))

        if not rows:
            layout.addWidget(QLabel("No hosts to compare - check one or more hosts."))
        for label, status in rows:
            row = QLabel(f"{label}:   {status}")
            row.setStyleSheet(f"color: {colors.get(status, STATUS_NEUTRAL_COLOR)}; font-weight: bold;")
            layout.addWidget(row)

        btn_close = QPushButton("Close")
        btn_close.clicked.connect(dlg.accept)
        layout.addWidget(btn_close)

        dlg.exec()

    def load_user_detail(self, username):
        if self.active_entry is None:
            return
        data = self.host_data.get(_entry_key(self.active_entry))
        if not data:
            return

        user = next((u for u in data.get("users", []) if u["username"] == username), None)
        if not user:
            return

        status_rows = self._status_by_host(username)
        known_statuses = {status for _, status in status_rows if status in ("LOCKED", "ACTIVE")}
        multi_host = len(status_rows) > 1

        if multi_host and len(known_statuses) > 1:
            self.status_label.setText("Account Status: MIXED across checked hosts")
            self.status_label.setStyleSheet(f"color: {STATUS_WARNING_COLOR}; font-weight: bold;")
        elif user.get("locked"):
            suffix = " (all checked hosts)" if multi_host else ""
            self.status_label.setText(f"Account Status: LOCKED{suffix}")
            self.status_label.setStyleSheet(f"color: {STATUS_ERROR_COLOR}; font-weight: bold;")
        else:
            suffix = " (all checked hosts)" if multi_host else ""
            self.status_label.setText(f"Account Status: ACTIVE{suffix}")
            self.status_label.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR}; font-weight: bold;")

        self.btn_status_by_host.setEnabled(multi_host)
        self.btn_status_by_host.setToolTip(
            "" if multi_host else "Check more than one host to compare status across them."
        )

        self.sudo_status.setText("Yes" if user.get("sudo") else "No")
        self.user_groups_label.setText(
            ", ".join(user.get("groups", [])) if user.get("groups") else "None"
        )
        self.current_shell_label.setText(user.get("shell") or "-")
        self.account_shell_input.setText(user.get("shell") or "")

        sessions = [s for s in data.get("sessions", []) if s.get("username") == username]
        self.sessions.setPlainText(
            "\n".join(s.get("tty", "-") for s in sessions) or "No active sessions"
        )

    # =========================================================
    # DISPATCH HELPER (runs a write command on every checked host -
    # SSH resolves immediately, agent hosts are polled until they
    # report back)
    # =========================================================
    def dispatch(self, command, label, auto_resync=True, entries=None):
        """entries defaults to whatever's checked above; pass an
        explicit list (e.g. every host from api.list_merged_hosts())
        to run somewhere wider than the checklist - see
        terminate_user() below."""
        explicit_entries = entries is not None

        if entries is None:
            entries = self.checked_entries()

        if not entries:
            if explicit_entries:
                QMessageBox.information(self, "No hosts", "There are no hosts to run this on.")
            else:
                QMessageBox.information(self, "No hosts checked", "Check one or more target hosts first.")
            return False

        ok_count = 0
        failed = []
        pending = []

        for entry in entries:
            result = api.run_on_entry(entry, command, description=label)

            if result["sync"]:
                if result["error"] or result.get("code") not in (0, None):
                    failed.append(entry["label"])
                else:
                    ok_count += 1
            else:
                if result["error"]:
                    failed.append(entry["label"])
                else:
                    key = _entry_key(entry)
                    self.pending_dispatch[key] = (entry, result["task_id"], label)
                    pending.append(entry["label"])

        msg = f"{label}: {ok_count}/{len(entries)} done"
        if pending:
            msg += f", {len(pending)} pending ({', '.join(pending)})"
        if failed:
            msg += f", failed on: {', '.join(failed)}"
        msg += "."

        if failed:
            self.dispatch_status.setStyleSheet(f"color: {STATUS_ERROR_COLOR};")
        elif pending:
            self.dispatch_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        else:
            self.dispatch_status.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
        self.dispatch_status.setText(msg)

        if self.pending_dispatch and not self.dispatch_poll_timer.isActive():
            self.dispatch_poll_timer.start(DISPATCH_POLL_MS)

        if auto_resync:
            QTimer.singleShot(AUTO_RESYNC_DELAY_MS, lambda: self._auto_resync(entries))

        return True

    def _poll_dispatch(self):
        if not self.pending_dispatch:
            self.dispatch_poll_timer.stop()
            return

        done = []

        for key, (entry, task_id, label) in list(self.pending_dispatch.items()):
            result = api.poll_entry_result(entry, task_id)
            if result is None:
                continue
            done.append(key)

        for key in done:
            del self.pending_dispatch[key]

        if not self.pending_dispatch:
            self.dispatch_poll_timer.stop()
            if "failed on:" not in self.dispatch_status.text():
                self.dispatch_status.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
            self.dispatch_status.setText(self.dispatch_status.text() + " All pending hosts reported back.")

    # =========================================================
    # ACTIONS (all dispatched to checked hosts, never local)
    # =========================================================
    def create_user(self):
        username = self.username_input.text().strip()
        if not username:
            return

        password = self.password_input.text()
        if password:
            ok, message = api.check_password_strength(password, self._password_policy())
            if not ok:
                QMessageBox.warning(self, "Weak password", message)
                return

        try:
            cmd = api.cmd_create_user(
                username,
                password,
                self.shell_input.text() or "/bin/bash",
            )
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        if self.dispatch(cmd, f"Create user '{username}'"):
            self.username_input.clear()
            self.password_input.clear()
            self.password_input.setEchoMode(QLineEdit.Password)
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

    def terminate_user(self):
        """Removes the selected user from every managed host - agent
        and SSH, checked or not - rather than just the ones currently
        ticked in the host checklist. Meant for fully cutting someone
        off (departure, compromised account, etc.), so it always asks
        first and always says exactly which hosts are affected."""
        if not self.selected_user:
            QMessageBox.information(self, "No user selected", "Select a user first.")
            return

        username = self.selected_user

        try:
            all_entries = api.list_merged_hosts()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        if not all_entries:
            QMessageBox.information(
                self, "No hosts", "There are no enrolled hosts to terminate this user from."
            )
            return

        host_names = "\n".join(f"  - {e['label']}" for e in all_entries)

        confirm = QMessageBox.warning(
            self,
            "Terminate user - this cannot be undone",
            f"This permanently deletes the user '{username}' from ALL "
            f"{len(all_entries)} managed host(s) - not just the ones "
            f"currently checked above:\n\n{host_names}\n\n"
            "Their account, home directory, and access on every one of "
            "these hosts will be removed. This cannot be undone.\n\n"
            "Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )

        if confirm != QMessageBox.Yes:
            return

        self.dispatch(
            api.cmd_delete_user(username),
            f"Terminate user '{username}' (all hosts)",
            entries=all_entries,
        )

    def lock_user(self):
        if not self.selected_user:
            return
        self.dispatch(api.cmd_lock_user(self.selected_user), f"Lock '{self.selected_user}'")

    def unlock_user(self):
        if not self.selected_user:
            return
        self.dispatch(api.cmd_unlock_user(self.selected_user), f"Unlock '{self.selected_user}'")

    def kill_sessions(self):
        if not self.selected_user:
            QMessageBox.information(self, "No user selected", "Select a user first.")
            return

        confirm = QMessageBox.question(
            self, "Kill sessions",
            f"End all active sessions for '{self.selected_user}' on checked hosts? "
            "The account is not locked - they can log back in immediately.",
            QMessageBox.Yes | QMessageBox.No
        )
        if confirm != QMessageBox.Yes:
            return

        self.dispatch(
            api.cmd_kill_user_sessions(self.selected_user),
            f"Kill sessions for '{self.selected_user}'"
        )

    def toggle_sudo(self):
        if not self.selected_user:
            return

        data = self.host_data.get(_entry_key(self.active_entry)) if self.active_entry else None
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

        ok, message = api.check_password_strength(password, self._password_policy())
        if not ok:
            QMessageBox.warning(self, "Weak password", message)
            return

        try:
            cmd = api.cmd_set_password(self.selected_user, password)
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        if self.dispatch(cmd, f"Set password for '{self.selected_user}'"):
            self.new_password_input.clear()
            self.new_password_input.setEchoMode(QLineEdit.Password)

    def _fill_generated_password(self, line_edit):
        # Echo mode is normally Password (dots) so a freshly-generated
        # value needs to switch to visible, or there'd be no way to
        # actually see/copy what was just generated.
        password = api.generate_strong_password(policy=self._password_policy())
        line_edit.setEchoMode(QLineEdit.Normal)
        line_edit.setText(password)
        line_edit.selectAll()

        # The field above is still the source of truth for Set
        # Password / Create User, but it's an easy place to lose a
        # password from (small, re-masks on focus loss) - this popout
        # is just a copyable, stays-on-screen-until-dismissed view of
        # the same value.
        _GeneratedPasswordDialog(self, password).exec()

    def _password_policy(self):
        """Best-effort fetch of the password sub-object of
        get_environmental_policy() (System Administration >
        Environmental Policies), so Generate / Set Password / Create
        User all enforce whatever's actually configured there instead
        of a hardcoded baseline. Falls back to None - which makes
        check_password_strength()/generate_strong_password() use
        their own built-in baseline - if the controller can't be
        reached, so a transient hiccup never blocks password entry."""
        try:
            return api.get_environmental_policy().get("password")
        except Exception:
            return None

    def force_password_reset(self):
        if not self.selected_user:
            QMessageBox.information(self, "No user selected", "Select a user first.")
            return

        self.dispatch(
            api.cmd_force_password_reset(self.selected_user),
            f"Force password reset for '{self.selected_user}' at next login"
        )

    @staticmethod
    def _parse_optional_int(text, field_name):
        """Blank means "leave this setting alone" (returns None);
        anything else must parse as a whole number or this raises
        ValueError with a message naming the offending field."""
        text = text.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            raise ValueError(f"{field_name} must be a whole number (or left blank).")

    def apply_password_aging(self):
        if not self.selected_user:
            QMessageBox.information(self, "No user selected", "Select a user first.")
            return

        try:
            max_days = self._parse_optional_int(self.aging_max_input.text(), "Max days")
            min_days = self._parse_optional_int(self.aging_min_input.text(), "Min days")
            warn_days = self._parse_optional_int(self.aging_warn_input.text(), "Warn days")
            cmd = api.cmd_set_password_aging(self.selected_user, max_days, min_days, warn_days)
        except ValueError as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        self.dispatch(cmd, f"Set password aging for '{self.selected_user}'")

    def _on_expire_choice_changed(self):
        self.expire_date_edit.setVisible(self.expire_combo.currentData() == "custom")

    def set_account_expiration(self):
        if not self.selected_user:
            QMessageBox.information(self, "No user selected", "Select a user first.")
            return

        choice = self.expire_combo.currentData()
        if choice == "never":
            date = ""
        elif choice == "custom":
            date = self.expire_date_edit.date().toString("yyyy-MM-dd")
        else:
            date = QDate.currentDate().addDays(int(choice)).toString("yyyy-MM-dd")

        label = f"Set expiration for '{self.selected_user}'" + (f" to {date}" if date else " (cleared)")
        self.dispatch(api.cmd_set_account_expiration(self.selected_user, date), label)

    def set_user_shell(self):
        if not self.selected_user:
            QMessageBox.information(self, "No user selected", "Select a user first.")
            return

        shell = self.account_shell_input.text().strip()
        if not shell:
            return

        self.dispatch(
            api.cmd_set_user_shell(self.selected_user, shell),
            f"Set shell for '{self.selected_user}'"
        )

    def set_user_comment(self):
        if not self.selected_user:
            QMessageBox.information(self, "No user selected", "Select a user first.")
            return

        comment = self.account_comment_input.text().strip()
        self.dispatch(
            api.cmd_set_user_comment(self.selected_user, comment),
            f"Set full name for '{self.selected_user}'"
        )

    def audit_privileged_users(self):
        entries = self.checked_entries()
        if not entries:
            QMessageBox.information(self, "No hosts checked", "Check one or more target hosts first.")
            return
        dialog = _CommandReportDialog(self, "Privileged User Audit", entries, api.cmd_audit_privileged_users())
        dialog.exec()

    def view_all_groups(self):
        entries = self.checked_entries()
        if not entries:
            QMessageBox.information(self, "No hosts checked", "Check one or more target hosts first.")
            return
        dialog = _CommandReportDialog(self, "Groups & Members", entries, api.cmd_list_groups_with_members())
        dialog.exec()

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
