from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout,
    QListWidget, QListWidgetItem, QLabel, QPushButton,
    QLineEdit, QTextEdit, QMessageBox, QTabWidget, QComboBox, QCheckBox,
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
from client.tab_sizing import shrink_tabwidget_to_current_page
from client.host_panel import build_host_panel

HOST_REFRESH_MS = 10000
SECURITY_POLL_MS = 2000


def _entry_key(entry):
    """Hashable identity for a merged-host entry (api.list_merged_hosts()) -
    agent host_ids and SSH host names live in separate namespaces, so the
    kind has to be part of the key."""
    return (entry["kind"], entry["id"])


def _blank_to_none(text):
    text = (text or "").strip()
    return text if text else None


class SecurityAdministrationPage(QWidget):
    """
    Security Administration against a *merged* host list (agent-enrolled
    hosts AND SSH-enrolled hosts) - SELinux (mode/booleans, denial
    troubleshooting, file contexts, policy modules), SSH hardening
    (sshd options, root login, key-based auth, key rotation), audit
    logs, failed-login review, security updates, password policy,
    baseline system hardening, and vulnerability scans, all dispatched
    to whichever hosts are checked, same as every other System
    Administration tool.

    See client/_api_security.py for the command builders.
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Security Administration")
        self.resize(1350, 820)

        self.security_results = {}   # entry_key -> {label, stdout, stderr, code, pending}
        self.security_pending = {}   # entry_key -> (entry, task_id)
        self.last_command_label = None
        self._collapsed_envs = set()

        main = QVBoxLayout()
        self.setLayout(main)

        main.addLayout(make_page_header("Security Administration"))

        body = QHBoxLayout()

        # =========================================================
        # TARGET HOSTS (agent + SSH, merged) - left column, full height
        # =========================================================
        self.host_list = QListWidget()
        connect_group_toggle(self.host_list)

        btn_refresh_hosts = QPushButton("Refresh Hosts")
        btn_refresh_hosts.clicked.connect(self.load_hosts)

        btn_select_all = QPushButton("Select All")
        btn_select_all.clicked.connect(self.select_all_hosts)

        btn_deselect_all = QPushButton("Deselect All")
        btn_deselect_all.clicked.connect(self.deselect_all_hosts)

        btn_collapse_all, btn_expand_all = add_collapse_expand_buttons(self.host_list)

        host_panel = build_host_panel(
            "Target Hosts (agent + SSH)",
            self.host_list,
            [
                [btn_refresh_hosts, btn_select_all, btn_deselect_all],
                [btn_collapse_all, btn_expand_all],
            ],
        )
        body.addWidget(host_panel)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)

        # =========================================================
        # ACTIONS (tabbed - one tab per feature group)
        # =========================================================
        action_tabs = QTabWidget()
        action_tabs.addTab(self._build_selinux_tab(), "SELinux")
        action_tabs.addTab(self._build_ssh_tab(), "SSH")
        action_tabs.addTab(self._build_audit_logins_tab(), "Audit && Logins")
        action_tabs.addTab(self._build_updates_policy_tab(), "Updates && Policy")
        action_tabs.addTab(self._build_hardening_scans_tab(), "Hardening && Scans")
        shrink_tabwidget_to_current_page(action_tabs)
        content_layout.addWidget(action_tabs)

        # =========================================================
        # RESULTS
        # =========================================================
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

        self.security_poll_timer = QTimer()
        self.security_poll_timer.timeout.connect(self._poll_security)

        bus.host_removed.connect(self.load_hosts)

    # =========================================================
    # ACTION PANEL BUILDERS
    # =========================================================
    def _build_selinux_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        mode_title = QLabel("Mode")
        mode_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(mode_title)

        status_row = QHBoxLayout()
        btn_status = QPushButton("Status")
        btn_status.clicked.connect(self.run_selinux_status)
        status_row.addWidget(btn_status)
        status_row.addStretch()
        layout.addLayout(status_row)

        runtime_row = QHBoxLayout()
        runtime_row.addWidget(QLabel("Runtime mode (reverts on reboot):"))
        self.selinux_runtime_mode_combo = QComboBox()
        self.selinux_runtime_mode_combo.addItems(["enforcing", "permissive"])
        runtime_row.addWidget(self.selinux_runtime_mode_combo)
        btn_set_runtime = QPushButton("Set Runtime Mode")
        btn_set_runtime.clicked.connect(self.run_set_selinux_runtime_mode)
        runtime_row.addWidget(btn_set_runtime)
        runtime_row.addStretch()
        layout.addLayout(runtime_row)

        persist_row = QHBoxLayout()
        persist_row.addWidget(QLabel("Persistent mode (survives reboot):"))
        self.selinux_persist_mode_combo = QComboBox()
        self.selinux_persist_mode_combo.addItems(["enforcing", "permissive", "disabled"])
        persist_row.addWidget(self.selinux_persist_mode_combo)
        btn_set_persist = QPushButton("Set Persistent Mode")
        btn_set_persist.clicked.connect(self.run_set_selinux_persist_mode)
        persist_row.addWidget(btn_set_persist)
        persist_row.addStretch()
        layout.addLayout(persist_row)

        layout.addSpacing(14)

        bool_title = QLabel("Booleans")
        bool_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(bool_title)

        bool_list_row = QHBoxLayout()
        bool_list_row.addWidget(QLabel("Filter (optional):"))
        self.selinux_bool_filter_input = QLineEdit()
        self.selinux_bool_filter_input.setPlaceholderText("e.g. httpd")
        bool_list_row.addWidget(self.selinux_bool_filter_input, 1)
        btn_list_bools = QPushButton("List Booleans")
        btn_list_bools.clicked.connect(self.run_list_selinux_booleans)
        bool_list_row.addWidget(btn_list_bools)
        layout.addLayout(bool_list_row)

        bool_set_row = QHBoxLayout()
        bool_set_row.addWidget(QLabel("Name:"))
        self.selinux_bool_name_input = QLineEdit()
        self.selinux_bool_name_input.setPlaceholderText("e.g. httpd_can_network_connect")
        bool_set_row.addWidget(self.selinux_bool_name_input, 1)
        self.selinux_bool_enabled_check = QCheckBox("Enabled")
        bool_set_row.addWidget(self.selinux_bool_enabled_check)
        self.selinux_bool_permanent_check = QCheckBox("Permanent")
        self.selinux_bool_permanent_check.setChecked(True)
        bool_set_row.addWidget(self.selinux_bool_permanent_check)
        btn_set_bool = QPushButton("Set Boolean")
        btn_set_bool.clicked.connect(self.run_set_selinux_boolean)
        bool_set_row.addWidget(btn_set_bool)
        layout.addLayout(bool_set_row)

        layout.addSpacing(14)

        denial_title = QLabel("Denials")
        denial_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(denial_title)

        denial_row = QHBoxLayout()
        denial_row.addWidget(QLabel("Lines:"))
        self.selinux_denial_lines_input = QLineEdit("50")
        self.selinux_denial_lines_input.setMaximumWidth(70)
        denial_row.addWidget(self.selinux_denial_lines_input)
        btn_recent_denials = QPushButton("Recent Denials")
        btn_recent_denials.clicked.connect(self.run_selinux_recent_denials)
        denial_row.addWidget(btn_recent_denials)
        btn_explain_denials = QPushButton("Explain Denials")
        btn_explain_denials.clicked.connect(self.run_selinux_explain_denials)
        denial_row.addWidget(btn_explain_denials)
        btn_journal_denials = QPushButton("Journal Denials")
        btn_journal_denials.clicked.connect(self.run_selinux_journal_denials)
        denial_row.addWidget(btn_journal_denials)
        denial_row.addStretch()
        layout.addLayout(denial_row)

        layout.addSpacing(14)

        context_title = QLabel("File Contexts")
        context_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(context_title)

        context_row = QHBoxLayout()
        context_row.addWidget(QLabel("Path:"))
        self.selinux_context_path_input = QLineEdit()
        self.selinux_context_path_input.setPlaceholderText("e.g. /var/www/html")
        context_row.addWidget(self.selinux_context_path_input, 1)
        btn_get_context = QPushButton("Get Context")
        btn_get_context.clicked.connect(self.run_selinux_get_context)
        context_row.addWidget(btn_get_context)
        self.selinux_context_recursive_check = QCheckBox("Recursive")
        context_row.addWidget(self.selinux_context_recursive_check)
        btn_restore_context = QPushButton("Restore Context")
        btn_restore_context.clicked.connect(self.run_selinux_restore_context)
        context_row.addWidget(btn_restore_context)
        layout.addLayout(context_row)

        layout.addSpacing(14)

        policy_title = QLabel("Policies (file-context rules + module generation)")
        policy_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(policy_title)

        fcontext_list_row = QHBoxLayout()
        fcontext_list_row.addWidget(QLabel("Filter (optional):"))
        self.selinux_fcontext_filter_input = QLineEdit()
        fcontext_list_row.addWidget(self.selinux_fcontext_filter_input, 1)
        btn_list_fcontext = QPushButton("List File Context Rules")
        btn_list_fcontext.clicked.connect(self.run_list_selinux_fcontext)
        fcontext_list_row.addWidget(btn_list_fcontext)
        layout.addLayout(fcontext_list_row)

        fcontext_row = QHBoxLayout()
        fcontext_row.addWidget(QLabel("Path spec:"))
        self.selinux_fcontext_path_input = QLineEdit()
        self.selinux_fcontext_path_input.setPlaceholderText('e.g. /srv/myapp(/.*)?')
        fcontext_row.addWidget(self.selinux_fcontext_path_input, 1)
        fcontext_row.addWidget(QLabel("SELinux type:"))
        self.selinux_fcontext_type_input = QLineEdit()
        self.selinux_fcontext_type_input.setPlaceholderText("e.g. httpd_sys_content_t")
        fcontext_row.addWidget(self.selinux_fcontext_type_input, 1)
        layout.addLayout(fcontext_row)

        fcontext_row2 = QHBoxLayout()
        fcontext_row2.addStretch()
        btn_add_fcontext = QPushButton("Add Rule")
        btn_add_fcontext.clicked.connect(self.run_add_selinux_fcontext)
        fcontext_row2.addWidget(btn_add_fcontext)
        btn_remove_fcontext = QPushButton("Remove Rule")
        btn_remove_fcontext.clicked.connect(self.run_remove_selinux_fcontext)
        fcontext_row2.addWidget(btn_remove_fcontext)
        layout.addLayout(fcontext_row2)

        module_row = QHBoxLayout()
        module_row.addWidget(QLabel("Module name:"))
        self.selinux_module_name_input = QLineEdit()
        self.selinux_module_name_input.setPlaceholderText("e.g. myapp_local")
        module_row.addWidget(self.selinux_module_name_input, 1)
        btn_generate_policy = QPushButton("Generate Policy From Denials")
        btn_generate_policy.clicked.connect(self.run_generate_selinux_policy)
        module_row.addWidget(btn_generate_policy)
        layout.addLayout(module_row)

        gen_hint = QLabel("Review denials first - this grants whatever the recent denials were asking for.")
        theme.style_hint_label(gen_hint)
        gen_hint.setWordWrap(True)
        layout.addWidget(gen_hint)

        layout.addStretch()
        return panel

    def _build_ssh_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        service_title = QLabel("Service")
        service_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(service_title)

        service_row = QHBoxLayout()
        btn_status = QPushButton("Status")
        btn_status.clicked.connect(self.run_sshd_status)
        service_row.addWidget(btn_status)
        btn_reload = QPushButton("Reload sshd")
        btn_reload.clicked.connect(self.run_sshd_reload)
        service_row.addWidget(btn_reload)
        service_row.addStretch()
        layout.addLayout(service_row)

        effective_row = QHBoxLayout()
        effective_row.addWidget(QLabel("Directive (optional, blank = all):"))
        self.sshd_effective_key_input = QLineEdit()
        self.sshd_effective_key_input.setPlaceholderText("e.g. PermitRootLogin")
        effective_row.addWidget(self.sshd_effective_key_input, 1)
        btn_effective = QPushButton("Effective Config")
        btn_effective.clicked.connect(self.run_sshd_effective_config)
        effective_row.addWidget(btn_effective)
        layout.addLayout(effective_row)

        layout.addSpacing(14)

        option_title = QLabel("Set sshd_config Option")
        option_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(option_title)

        option_row = QHBoxLayout()
        option_row.addWidget(QLabel("Directive:"))
        self.sshd_option_key_input = QLineEdit()
        self.sshd_option_key_input.setPlaceholderText("e.g. X11Forwarding")
        option_row.addWidget(self.sshd_option_key_input, 1)
        option_row.addWidget(QLabel("Value:"))
        self.sshd_option_value_input = QLineEdit()
        self.sshd_option_value_input.setPlaceholderText("e.g. no")
        option_row.addWidget(self.sshd_option_value_input, 1)
        btn_set_option = QPushButton("Set Option")
        btn_set_option.clicked.connect(self.run_sshd_set_option)
        option_row.addWidget(btn_set_option)
        layout.addLayout(option_row)

        option_hint = QLabel("Validated with sshd -t before being applied; reload sshd afterward to take effect.")
        theme.style_hint_label(option_hint)
        option_hint.setWordWrap(True)
        layout.addWidget(option_hint)

        layout.addSpacing(14)

        root_title = QLabel("Root Login")
        root_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(root_title)

        root_row = QHBoxLayout()
        btn_disable_root = QPushButton("Disable Root Login")
        btn_disable_root.clicked.connect(self.run_disable_root_login)
        root_row.addWidget(btn_disable_root)
        btn_allow_root = QPushButton("Allow Root Login")
        btn_allow_root.clicked.connect(self.run_allow_root_login)
        root_row.addWidget(btn_allow_root)
        root_row.addStretch()
        layout.addLayout(root_row)

        layout.addSpacing(14)

        keyauth_title = QLabel("Key-Based Authentication")
        keyauth_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(keyauth_title)

        keyauth_row = QHBoxLayout()
        btn_enable_pubkey = QPushButton("Enable Pubkey Auth")
        btn_enable_pubkey.clicked.connect(self.run_enable_pubkey_auth)
        keyauth_row.addWidget(btn_enable_pubkey)
        btn_disable_pubkey = QPushButton("Disable Pubkey Auth")
        btn_disable_pubkey.clicked.connect(self.run_disable_pubkey_auth)
        keyauth_row.addWidget(btn_disable_pubkey)
        btn_disable_password = QPushButton("Disable Password Auth")
        btn_disable_password.clicked.connect(self.run_disable_password_auth)
        keyauth_row.addWidget(btn_disable_password)
        btn_enable_password = QPushButton("Enable Password Auth")
        btn_enable_password.clicked.connect(self.run_enable_password_auth)
        keyauth_row.addWidget(btn_enable_password)
        layout.addLayout(keyauth_row)

        akeys_row = QHBoxLayout()
        akeys_row.addWidget(QLabel("User:"))
        self.ssh_user_input = QLineEdit()
        self.ssh_user_input.setPlaceholderText("e.g. deploy")
        akeys_row.addWidget(self.ssh_user_input, 1)
        btn_list_keys = QPushButton("List Authorized Keys")
        btn_list_keys.clicked.connect(self.run_list_authorized_keys)
        akeys_row.addWidget(btn_list_keys)
        layout.addLayout(akeys_row)

        install_row = QHBoxLayout()
        install_row.addWidget(QLabel("Public key:"))
        self.ssh_public_key_input = QLineEdit()
        self.ssh_public_key_input.setPlaceholderText("ssh-ed25519 AAAA... comment")
        install_row.addWidget(self.ssh_public_key_input, 1)
        btn_install_key = QPushButton("Install Key")
        btn_install_key.clicked.connect(self.run_install_authorized_key)
        install_row.addWidget(btn_install_key)
        layout.addLayout(install_row)

        layout.addSpacing(14)

        rotate_title = QLabel("Rotate SSH Keys")
        rotate_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(rotate_title)

        remove_row = QHBoxLayout()
        remove_row.addWidget(QLabel("Match text (key comment/fingerprint):"))
        self.ssh_remove_match_input = QLineEdit()
        remove_row.addWidget(self.ssh_remove_match_input, 1)
        btn_remove_key = QPushButton("Remove Matching Key(s)")
        btn_remove_key.clicked.connect(self.run_remove_authorized_key)
        remove_row.addWidget(btn_remove_key)
        layout.addLayout(remove_row)

        hostkey_row = QHBoxLayout()
        btn_rotate_hostkeys = QPushButton("Rotate Host Keys")
        btn_rotate_hostkeys.clicked.connect(self.run_rotate_host_keys)
        hostkey_row.addWidget(btn_rotate_hostkeys)
        hostkey_row.addStretch()
        layout.addLayout(hostkey_row)

        rotate_hint = QLabel("Regenerates this host's SSH identity and restarts sshd - every client that has "
                              "connected before will see a \"host key changed\" warning.")
        theme.style_hint_label(rotate_hint)
        rotate_hint.setWordWrap(True)
        layout.addWidget(rotate_hint)

        layout.addStretch()
        return panel

    def _build_audit_logins_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        audit_title = QLabel("Audit Logs")
        audit_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(audit_title)

        audit_row = QHBoxLayout()
        btn_auditd_status = QPushButton("Auditd Status")
        btn_auditd_status.clicked.connect(self.run_auditd_status)
        audit_row.addWidget(btn_auditd_status)
        audit_row.addWidget(QLabel("Lines:"))
        self.audit_lines_input = QLineEdit("200")
        self.audit_lines_input.setMaximumWidth(70)
        audit_row.addWidget(self.audit_lines_input)
        btn_tail_audit = QPushButton("Tail Audit Log")
        btn_tail_audit.clicked.connect(self.run_tail_audit_log)
        audit_row.addWidget(btn_tail_audit)
        audit_row.addStretch()
        layout.addLayout(audit_row)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Search:"))
        self.audit_search_input = QLineEdit()
        self.audit_search_input.setPlaceholderText("e.g. denied")
        search_row.addWidget(self.audit_search_input, 1)
        btn_search_audit = QPushButton("Search Audit Log")
        btn_search_audit.clicked.connect(self.run_search_audit_log)
        search_row.addWidget(btn_search_audit)
        layout.addLayout(search_row)

        layout.addSpacing(14)

        logins_title = QLabel("Failed Logins")
        logins_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(logins_title)

        logins_row = QHBoxLayout()
        logins_row.addWidget(QLabel("Lines:"))
        self.failed_login_lines_input = QLineEdit("50")
        self.failed_login_lines_input.setMaximumWidth(70)
        logins_row.addWidget(self.failed_login_lines_input)
        btn_list_failed = QPushButton("List Failed Logins")
        btn_list_failed.clicked.connect(self.run_list_failed_logins)
        logins_row.addWidget(btn_list_failed)
        logins_row.addWidget(QLabel("Top N:"))
        self.failed_login_topn_input = QLineEdit("20")
        self.failed_login_topn_input.setMaximumWidth(60)
        logins_row.addWidget(self.failed_login_topn_input)
        btn_failed_summary = QPushButton("Failed Login Summary (by IP)")
        btn_failed_summary.clicked.connect(self.run_failed_login_summary)
        logins_row.addWidget(btn_failed_summary)
        layout.addLayout(logins_row)

        locked_row = QHBoxLayout()
        btn_locked_accounts = QPushButton("List Locked Accounts")
        btn_locked_accounts.clicked.connect(self.run_list_locked_accounts)
        locked_row.addWidget(btn_locked_accounts)
        locked_row.addStretch()
        layout.addLayout(locked_row)

        layout.addStretch()
        return panel

    def _build_updates_policy_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        updates_title = QLabel("Security Updates")
        updates_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(updates_title)

        updates_row = QHBoxLayout()
        btn_check_updates = QPushButton("Check for Updates")
        btn_check_updates.clicked.connect(self.run_check_security_updates)
        updates_row.addWidget(btn_check_updates)
        btn_install_updates = QPushButton("Install Security Updates")
        btn_install_updates.clicked.connect(self.run_install_security_updates)
        updates_row.addWidget(btn_install_updates)
        updates_row.addStretch()
        layout.addLayout(updates_row)

        layout.addSpacing(14)

        policy_title = QLabel("Password Policy")
        policy_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(policy_title)

        policy_row = QHBoxLayout()
        btn_get_policy = QPushButton("Get Current Policy")
        btn_get_policy.clicked.connect(self.run_get_password_policy)
        policy_row.addWidget(btn_get_policy)
        policy_row.addStretch()
        layout.addLayout(policy_row)

        pwquality_row = QHBoxLayout()
        pwquality_row.addWidget(QLabel("pwquality option:"))
        self.pwquality_key_input = QLineEdit()
        self.pwquality_key_input.setPlaceholderText("e.g. minlen")
        pwquality_row.addWidget(self.pwquality_key_input, 1)
        pwquality_row.addWidget(QLabel("Value:"))
        self.pwquality_value_input = QLineEdit()
        self.pwquality_value_input.setPlaceholderText("e.g. 12")
        pwquality_row.addWidget(self.pwquality_value_input, 1)
        btn_set_pwquality = QPushButton("Set pwquality Option")
        btn_set_pwquality.clicked.connect(self.run_set_pwquality_option)
        pwquality_row.addWidget(btn_set_pwquality)
        layout.addLayout(pwquality_row)

        aging_row = QHBoxLayout()
        aging_row.addWidget(QLabel("Max days:"))
        self.password_max_days_input = QLineEdit()
        self.password_max_days_input.setMaximumWidth(60)
        aging_row.addWidget(self.password_max_days_input)
        aging_row.addWidget(QLabel("Min days:"))
        self.password_min_days_input = QLineEdit()
        self.password_min_days_input.setMaximumWidth(60)
        aging_row.addWidget(self.password_min_days_input)
        aging_row.addWidget(QLabel("Warn days:"))
        self.password_warn_days_input = QLineEdit()
        self.password_warn_days_input.setMaximumWidth(60)
        aging_row.addWidget(self.password_warn_days_input)
        btn_set_aging = QPushButton("Set Password Aging")
        btn_set_aging.clicked.connect(self.run_set_password_aging)
        aging_row.addWidget(btn_set_aging)
        layout.addLayout(aging_row)

        aging_hint = QLabel("Leave a field blank to leave that setting untouched. Applies to new accounts only.")
        theme.style_hint_label(aging_hint)
        aging_hint.setWordWrap(True)
        layout.addWidget(aging_hint)

        lockout_row = QHBoxLayout()
        lockout_row.addWidget(QLabel("Failed attempts:"))
        self.lockout_attempts_input = QLineEdit("5")
        self.lockout_attempts_input.setMaximumWidth(60)
        lockout_row.addWidget(self.lockout_attempts_input)
        lockout_row.addWidget(QLabel("Unlock after (seconds, 0 = admin reset):"))
        self.lockout_unlock_seconds_input = QLineEdit("600")
        self.lockout_unlock_seconds_input.setMaximumWidth(80)
        lockout_row.addWidget(self.lockout_unlock_seconds_input)
        btn_set_lockout = QPushButton("Set Account Lockout")
        btn_set_lockout.clicked.connect(self.run_set_account_lockout)
        lockout_row.addWidget(btn_set_lockout)
        layout.addLayout(lockout_row)

        layout.addStretch()
        return panel

    def _build_hardening_scans_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        harden_title = QLabel("Harden System")
        harden_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(harden_title)

        harden_row = QHBoxLayout()
        btn_overview = QPushButton("Hardening Overview")
        btn_overview.clicked.connect(self.run_hardening_overview)
        harden_row.addWidget(btn_overview)
        btn_apply_sysctl = QPushButton("Apply Sysctl Hardening")
        btn_apply_sysctl.clicked.connect(self.run_apply_sysctl_hardening)
        harden_row.addWidget(btn_apply_sysctl)
        btn_disable_coredumps = QPushButton("Disable Core Dumps")
        btn_disable_coredumps.clicked.connect(self.run_disable_core_dumps)
        harden_row.addWidget(btn_disable_coredumps)
        layout.addLayout(harden_row)

        audit_row = QHBoxLayout()
        audit_row.addWidget(QLabel("Path:"))
        self.hardening_path_input = QLineEdit("/etc")
        audit_row.addWidget(self.hardening_path_input, 1)
        btn_world_writable = QPushButton("List World-Writable Files")
        btn_world_writable.clicked.connect(self.run_list_world_writable_files)
        audit_row.addWidget(btn_world_writable)
        btn_suid = QPushButton("List SUID Binaries")
        btn_suid.clicked.connect(self.run_list_suid_binaries)
        audit_row.addWidget(btn_suid)
        layout.addLayout(audit_row)

        layout.addSpacing(14)

        scans_title = QLabel("Vulnerability Scans")
        scans_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(scans_title)

        scans_row = QHBoxLayout()
        btn_lynis_status = QPushButton("Lynis Status")
        btn_lynis_status.clicked.connect(self.run_lynis_status)
        scans_row.addWidget(btn_lynis_status)
        btn_install_lynis = QPushButton("Install Lynis")
        btn_install_lynis.clicked.connect(self.run_install_lynis)
        scans_row.addWidget(btn_install_lynis)
        btn_run_lynis = QPushButton("Run Lynis Scan")
        btn_run_lynis.clicked.connect(self.run_lynis_scan)
        scans_row.addWidget(btn_run_lynis)
        btn_run_rkhunter = QPushButton("Run rkhunter Scan")
        btn_run_rkhunter.clicked.connect(self.run_rkhunter_scan)
        scans_row.addWidget(btn_run_rkhunter)
        layout.addLayout(scans_row)

        scans_hint = QLabel("Scans can take several minutes per host; results appear in the tab below once each "
                             "host reports back.")
        theme.style_hint_label(scans_hint)
        scans_hint.setWordWrap(True)
        layout.addWidget(scans_hint)

        layout.addStretch()
        return panel

    def _build_results_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        self.security_status = QLabel("Pick an action above to run it on all checked hosts.")
        self.security_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        layout.addWidget(self.security_status)

        self.security_tabs = QTabWidget()
        self.security_tabs.setTabsClosable(True)
        self.security_tabs.tabCloseRequested.connect(self._close_security_tab)
        shrink_tabwidget_to_current_page(self.security_tabs)
        layout.addWidget(self.security_tabs)
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
            self.security_status.setStyleSheet(f"color: {STATUS_ERROR_COLOR};")
            self.security_status.setText(f"Could not load hosts: {e}")
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
    # SELINUX ACTIONS
    # =========================================================
    def run_selinux_status(self):
        self._run_security_command(api.cmd_selinux_status(), "SELinux Status")

    def run_set_selinux_runtime_mode(self):
        try:
            cmd = api.cmd_set_selinux_mode(self.selinux_runtime_mode_combo.currentText())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_security_command(cmd, "Set SELinux Runtime Mode")

    def run_set_selinux_persist_mode(self):
        mode = self.selinux_persist_mode_combo.currentText()
        confirm = QMessageBox.question(
            self, "Confirm SELinux mode change",
            f"Persistently set SELinux mode to '{mode}' on all checked hosts?"
            + (" This requires a reboot to take full effect." if mode == "disabled" else ""),
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            cmd = api.cmd_set_selinux_config_mode(mode)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_security_command(cmd, "Set SELinux Persistent Mode")

    def run_list_selinux_booleans(self):
        self._run_security_command(api.cmd_selinux_list_booleans(self.selinux_bool_filter_input.text()), "List SELinux Booleans")

    def run_set_selinux_boolean(self):
        try:
            cmd = api.cmd_set_selinux_boolean(
                self.selinux_bool_name_input.text(),
                self.selinux_bool_enabled_check.isChecked(),
                self.selinux_bool_permanent_check.isChecked(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_security_command(cmd, "Set SELinux Boolean")

    def run_selinux_recent_denials(self):
        try:
            cmd = api.cmd_selinux_recent_denials(self.selinux_denial_lines_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_security_command(cmd, "Recent SELinux Denials")

    def run_selinux_explain_denials(self):
        try:
            cmd = api.cmd_selinux_explain_denials(self.selinux_denial_lines_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_security_command(cmd, "Explain SELinux Denials")

    def run_selinux_journal_denials(self):
        try:
            cmd = api.cmd_selinux_journal_denials(self.selinux_denial_lines_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_security_command(cmd, "SELinux Journal Denials")

    def run_selinux_get_context(self):
        try:
            cmd = api.cmd_selinux_get_context(self.selinux_context_path_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_security_command(cmd, "Get SELinux Context")

    def run_selinux_restore_context(self):
        try:
            cmd = api.cmd_selinux_restore_context(
                self.selinux_context_path_input.text(),
                self.selinux_context_recursive_check.isChecked(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_security_command(cmd, "Restore SELinux Context")

    def run_list_selinux_fcontext(self):
        self._run_security_command(api.cmd_selinux_list_fcontext(self.selinux_fcontext_filter_input.text()), "List File Context Rules")

    def run_add_selinux_fcontext(self):
        try:
            cmd = api.cmd_selinux_add_fcontext(
                self.selinux_fcontext_path_input.text(), self.selinux_fcontext_type_input.text(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_security_command(cmd, "Add File Context Rule")

    def run_remove_selinux_fcontext(self):
        confirm = QMessageBox.question(
            self, "Confirm rule removal",
            "Remove this file context rule on all checked hosts?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            cmd = api.cmd_selinux_remove_fcontext(
                self.selinux_fcontext_path_input.text(), self.selinux_fcontext_type_input.text(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_security_command(cmd, "Remove File Context Rule")

    def run_generate_selinux_policy(self):
        confirm = QMessageBox.question(
            self, "Confirm policy generation",
            "Generate and load a new SELinux policy module from the recent AVC denials on all "
            "checked hosts? This grants whatever those denials were asking for - only proceed if "
            "you've confirmed the access is legitimate.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            cmd = api.cmd_selinux_generate_policy_from_denials(self.selinux_module_name_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_security_command(cmd, "Generate SELinux Policy")

    # =========================================================
    # SSH ACTIONS
    # =========================================================
    def run_sshd_status(self):
        self._run_security_command(api.cmd_sshd_status(), "sshd Status")

    def run_sshd_reload(self):
        self._run_security_command(api.cmd_sshd_reload(), "Reload sshd")

    def run_sshd_effective_config(self):
        try:
            cmd = api.cmd_sshd_get_effective_config(self.sshd_effective_key_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_security_command(cmd, "sshd Effective Config")

    def run_sshd_set_option(self):
        try:
            cmd = api.cmd_sshd_set_option(self.sshd_option_key_input.text(), self.sshd_option_value_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_security_command(cmd, "Set sshd Option")

    def run_disable_root_login(self):
        self._run_security_command(api.cmd_set_root_login(False), "Disable Root Login")

    def run_allow_root_login(self):
        confirm = QMessageBox.question(
            self, "Confirm allowing root login",
            "Allow root login over SSH on all checked hosts? This weakens the hosts' security posture.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        self._run_security_command(api.cmd_set_root_login(True), "Allow Root Login")

    def run_enable_pubkey_auth(self):
        self._run_security_command(api.cmd_set_pubkey_auth(True), "Enable Pubkey Auth")

    def run_disable_pubkey_auth(self):
        confirm = QMessageBox.question(
            self, "Confirm disabling pubkey auth",
            "Disable key-based SSH authentication on all checked hosts?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        self._run_security_command(api.cmd_set_pubkey_auth(False), "Disable Pubkey Auth")

    def run_disable_password_auth(self):
        self._run_security_command(api.cmd_set_password_auth(False), "Disable Password Auth")

    def run_enable_password_auth(self):
        confirm = QMessageBox.question(
            self, "Confirm enabling password auth",
            "Enable password-based SSH authentication on all checked hosts? This weakens the "
            "hosts' security posture.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        self._run_security_command(api.cmd_set_password_auth(True), "Enable Password Auth")

    def run_list_authorized_keys(self):
        try:
            cmd = api.cmd_list_authorized_keys(self.ssh_user_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_security_command(cmd, "List Authorized Keys")

    def run_install_authorized_key(self):
        try:
            cmd = api.cmd_install_authorized_key(self.ssh_user_input.text(), self.ssh_public_key_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_security_command(cmd, "Install Authorized Key")

    def run_remove_authorized_key(self):
        confirm = QMessageBox.question(
            self, "Confirm key removal",
            "Remove every authorized_keys line matching this text on all checked hosts?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            cmd = api.cmd_remove_authorized_key(self.ssh_user_input.text(), self.ssh_remove_match_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_security_command(cmd, "Remove Authorized Key")

    def run_rotate_host_keys(self):
        confirm = QMessageBox.question(
            self, "Confirm host key rotation",
            "Regenerate SSH host keys and restart sshd on all checked hosts? Every client that has "
            "connected before will see a \"host key changed\" warning. This is irreversible.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        self._run_security_command(api.cmd_rotate_host_keys(), "Rotate SSH Host Keys")

    # =========================================================
    # AUDIT & LOGINS ACTIONS
    # =========================================================
    def run_auditd_status(self):
        self._run_security_command(api.cmd_auditd_status(), "Auditd Status")

    def run_tail_audit_log(self):
        try:
            cmd = api.cmd_tail_audit_log(self.audit_lines_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_security_command(cmd, "Tail Audit Log")

    def run_search_audit_log(self):
        try:
            cmd = api.cmd_search_audit_log(self.audit_search_input.text(), self.audit_lines_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_security_command(cmd, "Search Audit Log")

    def run_list_failed_logins(self):
        try:
            cmd = api.cmd_list_failed_logins(self.failed_login_lines_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_security_command(cmd, "List Failed Logins")

    def run_failed_login_summary(self):
        try:
            cmd = api.cmd_failed_login_summary(self.failed_login_topn_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_security_command(cmd, "Failed Login Summary")

    def run_list_locked_accounts(self):
        self._run_security_command(api.cmd_list_locked_accounts(), "List Locked Accounts")

    # =========================================================
    # UPDATES & POLICY ACTIONS
    # =========================================================
    def run_check_security_updates(self):
        self._run_security_command(api.cmd_check_security_updates(), "Check Security Updates")

    def run_install_security_updates(self):
        self._run_security_command(api.cmd_install_security_updates(), "Install Security Updates")

    def run_get_password_policy(self):
        self._run_security_command(api.cmd_get_password_policy(), "Get Password Policy")

    def run_set_pwquality_option(self):
        try:
            cmd = api.cmd_set_pwquality_option(self.pwquality_key_input.text(), self.pwquality_value_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_security_command(cmd, "Set pwquality Option")

    def run_set_password_aging(self):
        try:
            cmd = api.cmd_set_password_aging(
                _blank_to_none(self.password_max_days_input.text()),
                _blank_to_none(self.password_min_days_input.text()),
                _blank_to_none(self.password_warn_days_input.text()),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_security_command(cmd, "Set Password Aging")

    def run_set_account_lockout(self):
        try:
            cmd = api.cmd_set_account_lockout(self.lockout_attempts_input.text(), self.lockout_unlock_seconds_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_security_command(cmd, "Set Account Lockout")

    # =========================================================
    # HARDENING & SCANS ACTIONS
    # =========================================================
    def run_hardening_overview(self):
        self._run_security_command(api.cmd_get_hardening_overview(), "Hardening Overview")

    def run_apply_sysctl_hardening(self):
        self._run_security_command(api.cmd_apply_sysctl_hardening(), "Apply Sysctl Hardening")

    def run_disable_core_dumps(self):
        self._run_security_command(api.cmd_disable_core_dumps(), "Disable Core Dumps")

    def run_list_world_writable_files(self):
        try:
            cmd = api.cmd_list_world_writable_files(self.hardening_path_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_security_command(cmd, "List World-Writable Files")

    def run_list_suid_binaries(self):
        try:
            cmd = api.cmd_list_suid_binaries(self.hardening_path_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_security_command(cmd, "List SUID Binaries")

    def run_lynis_status(self):
        self._run_security_command(api.cmd_lynis_status(), "Lynis Status")

    def run_install_lynis(self):
        self._run_security_command(api.cmd_install_lynis(), "Install Lynis")

    def run_lynis_scan(self):
        self._run_security_command(api.cmd_run_lynis_scan(), "Run Lynis Scan")

    def run_rkhunter_scan(self):
        self._run_security_command(api.cmd_run_rkhunter_scan(), "Run rkhunter Scan")

    # =========================================================
    # DISPATCH + RESULTS (same pattern as Storage Administration /
    # Firewall Administration / Network Management / Service Management)
    # =========================================================
    def _run_security_command(self, command, label):
        entries = self.checked_entries()

        if not entries:
            QMessageBox.information(self, "No hosts checked", "Check one or more target hosts first.")
            return

        self.last_command_label = label
        self.security_results = {}
        self.security_pending = {}
        self.security_tabs.clear()

        for entry in entries:
            key = _entry_key(entry)
            result = api.run_on_entry(entry, command)

            if result["sync"]:
                self.security_results[key] = {
                    "label": entry["label"],
                    "stdout": result["stdout"],
                    "stderr": result["stderr"] or result["error"] or "",
                    "code": result["code"],
                    "pending": False,
                }
            elif result["error"]:
                self.security_results[key] = {
                    "label": entry["label"], "stdout": "", "stderr": result["error"],
                    "code": None, "pending": False,
                }
            else:
                self.security_results[key] = {
                    "label": entry["label"], "stdout": "", "stderr": "",
                    "code": None, "pending": True,
                }
                self.security_pending[key] = (entry, result["task_id"])

            self._add_security_tab(key)

        self.security_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        self.security_status.setText(f"Running '{label}' on {len(entries)} host(s)...")

        if self.security_tabs.count() > 0:
            self.security_tabs.setCurrentIndex(0)

        if self.security_pending:
            self.security_poll_timer.start(SECURITY_POLL_MS)
        else:
            self.security_status.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
            self.security_status.setText(f"'{label}' complete.")

    def _status_text(self, data):
        if data["pending"]:
            return "pending..."
        return "ok" if not data["stderr"] else "error"

    def _render_security_result(self, text_edit, data):
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

    def _close_security_tab(self, index):
        bar = self.security_tabs.tabBar()
        key = bar.tabData(index)
        self.security_tabs.removeTab(index)
        self.security_results.pop(key, None)
        self.security_pending.pop(key, None)

    def _add_security_tab(self, key):
        data = self.security_results.get(key)
        if not data:
            return
        status = self._status_text(data)

        text_edit = QTextEdit()
        text_edit.setReadOnly(True)
        text_edit.setStyleSheet("font-family: monospace;")
        self._render_security_result(text_edit, data)

        idx = self.security_tabs.addTab(text_edit, f"{data['label']}  [{status}]")
        self.security_tabs.tabBar().setTabData(idx, key)

    def _refresh_security_tab(self, key):
        bar = self.security_tabs.tabBar()
        for i in range(self.security_tabs.count()):
            if bar.tabData(i) != key:
                continue
            data = self.security_results.get(key)
            if data:
                status = self._status_text(data)
                self.security_tabs.setTabText(i, f"{data['label']}  [{status}]")
                self._render_security_result(self.security_tabs.widget(i), data)
            return

    def _poll_security(self):
        if not self.security_pending:
            self.security_poll_timer.stop()
            return

        done = []

        for key, (entry, task_id) in list(self.security_pending.items()):
            result = api.poll_entry_result(entry, task_id)
            if result is None:
                continue

            self.security_results[key] = {
                "label": entry["label"],
                "stdout": result["stdout"],
                "stderr": result["stderr"] or result["error"] or "",
                "code": result["code"],
                "pending": False,
            }
            self._refresh_security_tab(key)
            done.append(key)

        for key in done:
            del self.security_pending[key]

        if not self.security_pending:
            self.security_poll_timer.stop()
            self.security_status.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
            self.security_status.setText("All hosts reported back.")
