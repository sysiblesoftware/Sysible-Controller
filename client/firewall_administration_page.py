from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout,
    QListWidget, QListWidgetItem, QLabel, QPushButton,
    QLineEdit, QTextEdit, QMessageBox, QGroupBox, QTabWidget, QComboBox, QCheckBox,
)
from PySide6.QtCore import Qt, QTimer

from client import api
from client import result_banner
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
FIREWALL_POLL_MS = 2000


def _entry_key(entry):
    """Hashable identity for a merged-host entry (api.list_merged_hosts()) -
    agent host_ids and SSH host names live in separate namespaces, so the
    kind has to be part of the key."""
    return (entry["kind"], entry["id"])


class FirewallAdministrationPage(QWidget):
    """
    Firewall Administration against a *merged* host list (agent-enrolled
    hosts AND SSH-enrolled hosts) - firewalld (service state, default zone,
    ports, zones, rich rules) plus the two lower-level backends it
    normally sits on top of, nftables and iptables, all dispatched to
    whichever hosts are checked, same as every other System Administration
    tool.

    See client/_api_firewall.py for the command builders.
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Firewall Administration")
        self.resize(1350, 820)

        self.firewall_results = {}   # entry_key -> {label, stdout, stderr, code, pending}
        self.firewall_pending = {}   # entry_key -> (entry, task_id)
        self.last_command_label = None
        self._collapsed_envs = set()

        main = QVBoxLayout()
        self.setLayout(main)

        main.addLayout(make_page_header("Firewall Administration"))

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
            "Target Hosts (agent-managed)",
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
        action_tabs.addTab(self._build_firewalld_tab(), "Firewalld")
        action_tabs.addTab(self._build_ports_tab(), "Ports")
        action_tabs.addTab(self._build_zones_tab(), "Zones")
        action_tabs.addTab(self._build_rich_rules_tab(), "Rich Rules")
        action_tabs.addTab(self._build_nftables_tab(), "nftables")
        action_tabs.addTab(self._build_iptables_tab(), "iptables")
        shrink_tabwidget_to_current_page(action_tabs, cap_height=True)
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

        self.firewall_poll_timer = QTimer()
        self.firewall_poll_timer.timeout.connect(self._poll_firewall)

        bus.host_removed.connect(self.load_hosts)

    # =========================================================
    # ACTION PANEL BUILDERS
    # =========================================================
    @staticmethod
    def _group(title):
        box = QGroupBox(title)
        lay = QVBoxLayout(box)
        return box, lay

    def _build_firewalld_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        _box1, _g1 = self._group("Service State")

        status_row = QHBoxLayout()
        btn_status = QPushButton("Status")
        btn_status.clicked.connect(self.run_firewalld_status)
        status_row.addWidget(btn_status)
        btn_enable = QPushButton("Enable && Start")
        btn_enable.clicked.connect(self.run_enable_firewalld)
        status_row.addWidget(btn_enable)
        btn_disable = QPushButton("Disable && Stop")
        btn_disable.clicked.connect(self.run_disable_firewalld)
        status_row.addWidget(btn_disable)
        btn_reload = QPushButton("Reload")
        btn_reload.clicked.connect(self.run_reload_firewalld)
        status_row.addWidget(btn_reload)
        status_row.addStretch()
        _g1.addLayout(status_row)


        layout.addWidget(_box1)

        _box2, _g2 = self._group("Default Zone")

        zone_row = QHBoxLayout()
        zone_row.addWidget(QLabel("Zone:"))
        self.default_zone_input = QLineEdit()
        self.default_zone_input.setPlaceholderText("e.g. public")
        zone_row.addWidget(self.default_zone_input, 1)
        btn_set_default_zone = QPushButton("Set Default Zone")
        btn_set_default_zone.clicked.connect(self.run_set_default_zone)
        zone_row.addWidget(btn_set_default_zone)
        _g2.addLayout(zone_row)

        layout.addWidget(_box2)
        layout.addStretch()
        return panel

    def _build_ports_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        _box1, _g1 = self._group("List")

        list_row = QHBoxLayout()
        list_row.addWidget(QLabel("Zone (optional):"))
        self.list_ports_zone_input = QLineEdit()
        self.list_ports_zone_input.setPlaceholderText("blank = default zone")
        list_row.addWidget(self.list_ports_zone_input, 1)
        btn_list_ports = QPushButton("List Ports (and Zone Details)")
        btn_list_ports.clicked.connect(self.run_list_ports)
        list_row.addWidget(btn_list_ports)
        _g1.addLayout(list_row)


        layout.addWidget(_box1)

        _box2, _g2 = self._group("Open / Close Port")

        port_row = QHBoxLayout()
        port_row.addWidget(QLabel("Port:"))
        self.port_input = QLineEdit()
        self.port_input.setPlaceholderText("e.g. 8080 or 8000-9000")
        self.port_input.setMaximumWidth(120)
        port_row.addWidget(self.port_input)
        port_row.addWidget(QLabel("Protocol:"))
        self.port_protocol_combo = QComboBox()
        self.port_protocol_combo.addItems(["tcp", "udp"])
        port_row.addWidget(self.port_protocol_combo)
        port_row.addWidget(QLabel("Zone (optional):"))
        self.port_zone_input = QLineEdit()
        self.port_zone_input.setPlaceholderText("blank = default zone")
        port_row.addWidget(self.port_zone_input, 1)
        _g2.addLayout(port_row)

        port_row2 = QHBoxLayout()
        self.port_permanent_check = QCheckBox("Permanent (and reload)")
        self.port_permanent_check.setChecked(True)
        port_row2.addWidget(self.port_permanent_check)
        port_row2.addStretch()
        btn_open_port = QPushButton("Open Port")
        btn_open_port.clicked.connect(self.run_open_port)
        port_row2.addWidget(btn_open_port)
        btn_close_port = QPushButton("Close Port")
        btn_close_port.clicked.connect(self.run_close_port)
        port_row2.addWidget(btn_close_port)
        _g2.addLayout(port_row2)

        layout.addWidget(_box2)
        layout.addStretch()
        return panel

    def _build_zones_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        list_row = QHBoxLayout()
        btn_list_zones = QPushButton("List Zones")
        btn_list_zones.clicked.connect(self.run_list_zones)
        list_row.addWidget(btn_list_zones)
        list_row.addStretch()
        layout.addLayout(list_row)


        _box1, _g1 = self._group("Create / Delete Zone")

        manage_row = QHBoxLayout()
        manage_row.addWidget(QLabel("Zone name:"))
        self.zone_name_input = QLineEdit()
        self.zone_name_input.setPlaceholderText("e.g. dmz")
        manage_row.addWidget(self.zone_name_input, 1)
        btn_create_zone = QPushButton("Create Zone")
        btn_create_zone.clicked.connect(self.run_create_zone)
        manage_row.addWidget(btn_create_zone)
        btn_delete_zone = QPushButton("Delete Zone")
        btn_delete_zone.clicked.connect(self.run_delete_zone)
        manage_row.addWidget(btn_delete_zone)
        _g1.addLayout(manage_row)

        manage_hint = QLabel("New zones are added permanently and firewalld is reloaded immediately so they show up right away.")
        theme.style_hint_label(manage_hint)
        manage_hint.setWordWrap(True)
        _g1.addWidget(manage_hint)

        layout.addWidget(_box1)
        layout.addStretch()
        return panel

    def _build_rich_rules_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        _box1, _g1 = self._group("List")

        list_row = QHBoxLayout()
        list_row.addWidget(QLabel("Zone (optional):"))
        self.list_rich_zone_input = QLineEdit()
        self.list_rich_zone_input.setPlaceholderText("blank = default zone")
        list_row.addWidget(self.list_rich_zone_input, 1)
        btn_list_rich = QPushButton("List Rich Rules")
        btn_list_rich.clicked.connect(self.run_list_rich_rules)
        list_row.addWidget(btn_list_rich)
        _g1.addLayout(list_row)


        layout.addWidget(_box1)

        _box2, _g2 = self._group("Add / Remove Rich Rule")

        rule_row = QHBoxLayout()
        rule_row.addWidget(QLabel("Rule:"))
        self.rich_rule_input = QLineEdit()
        self.rich_rule_input.setPlaceholderText(
            'e.g. rule family="ipv4" source address="192.168.0.0/24" service name="ssh" accept'
        )
        rule_row.addWidget(self.rich_rule_input, 1)
        _g2.addLayout(rule_row)

        rule_row2 = QHBoxLayout()
        rule_row2.addWidget(QLabel("Zone (optional):"))
        self.rich_rule_zone_input = QLineEdit()
        self.rich_rule_zone_input.setPlaceholderText("blank = default zone")
        rule_row2.addWidget(self.rich_rule_zone_input, 1)
        self.rich_rule_permanent_check = QCheckBox("Permanent (and reload)")
        self.rich_rule_permanent_check.setChecked(True)
        rule_row2.addWidget(self.rich_rule_permanent_check)
        btn_add_rich = QPushButton("Add Rich Rule")
        btn_add_rich.clicked.connect(self.run_add_rich_rule)
        rule_row2.addWidget(btn_add_rich)
        btn_remove_rich = QPushButton("Remove Rich Rule")
        btn_remove_rich.clicked.connect(self.run_remove_rich_rule)
        rule_row2.addWidget(btn_remove_rich)
        _g2.addLayout(rule_row2)

        layout.addWidget(_box2)
        layout.addStretch()
        return panel

    def _build_nftables_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        list_row = QHBoxLayout()
        btn_list_ruleset = QPushButton("List Ruleset")
        btn_list_ruleset.clicked.connect(self.run_nft_list_ruleset)
        list_row.addWidget(btn_list_ruleset)
        list_row.addStretch()
        btn_flush_ruleset = QPushButton("Flush Ruleset")
        btn_flush_ruleset.clicked.connect(self.run_nft_flush_ruleset)
        list_row.addWidget(btn_flush_ruleset)
        layout.addLayout(list_row)


        _box1, _g1 = self._group("Table / Chain / Rule")

        common_row = QHBoxLayout()
        common_row.addWidget(QLabel("Family:"))
        self.nft_family_combo = QComboBox()
        self.nft_family_combo.addItems(["ip", "ip6", "inet", "arp", "bridge", "netdev"])
        common_row.addWidget(self.nft_family_combo)
        common_row.addWidget(QLabel("Table:"))
        self.nft_table_input = QLineEdit()
        self.nft_table_input.setPlaceholderText("e.g. filter")
        common_row.addWidget(self.nft_table_input)
        common_row.addWidget(QLabel("Chain:"))
        self.nft_chain_input = QLineEdit()
        self.nft_chain_input.setPlaceholderText("e.g. input")
        common_row.addWidget(self.nft_chain_input)
        btn_add_table = QPushButton("Add Table")
        btn_add_table.clicked.connect(self.run_nft_add_table)
        common_row.addWidget(btn_add_table)
        _g1.addLayout(common_row)

        chain_row = QHBoxLayout()
        chain_row.addWidget(QLabel("Hook (blank = non-base chain):"))
        self.nft_hook_combo = QComboBox()
        self.nft_hook_combo.addItems(["", "prerouting", "input", "forward", "output", "postrouting"])
        chain_row.addWidget(self.nft_hook_combo)
        chain_row.addWidget(QLabel("Priority:"))
        self.nft_priority_input = QLineEdit("0")
        self.nft_priority_input.setMaximumWidth(60)
        chain_row.addWidget(self.nft_priority_input)
        chain_row.addWidget(QLabel("Policy:"))
        self.nft_policy_combo = QComboBox()
        self.nft_policy_combo.addItems(["accept", "drop"])
        chain_row.addWidget(self.nft_policy_combo)
        btn_add_chain = QPushButton("Add Chain")
        btn_add_chain.clicked.connect(self.run_nft_add_chain)
        chain_row.addWidget(btn_add_chain)
        _g1.addLayout(chain_row)

        rule_row = QHBoxLayout()
        rule_row.addWidget(QLabel("Rule:"))
        self.nft_rule_input = QLineEdit()
        self.nft_rule_input.setPlaceholderText("e.g. tcp dport 22 accept")
        rule_row.addWidget(self.nft_rule_input, 1)
        btn_add_rule = QPushButton("Add Rule")
        btn_add_rule.clicked.connect(self.run_nft_add_rule)
        rule_row.addWidget(btn_add_rule)
        _g1.addLayout(rule_row)

        delete_row = QHBoxLayout()
        delete_row.addWidget(QLabel("Rule handle (see List Ruleset -a output):"))
        self.nft_handle_input = QLineEdit()
        self.nft_handle_input.setMaximumWidth(80)
        delete_row.addWidget(self.nft_handle_input)
        btn_delete_rule = QPushButton("Delete Rule")
        btn_delete_rule.clicked.connect(self.run_nft_delete_rule)
        delete_row.addWidget(btn_delete_rule)
        delete_row.addStretch()
        _g1.addLayout(delete_row)

        layout.addWidget(_box1)
        layout.addStretch()
        return panel

    def _build_iptables_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        _box1, _g1 = self._group("List")

        list_row = QHBoxLayout()
        list_row.addWidget(QLabel("Table:"))
        self.iptables_table_combo = QComboBox()
        self.iptables_table_combo.addItems(["filter", "nat", "mangle", "raw", "security"])
        list_row.addWidget(self.iptables_table_combo)
        btn_list_iptables = QPushButton("List Rules")
        btn_list_iptables.clicked.connect(self.run_iptables_list)
        list_row.addWidget(btn_list_iptables)
        list_row.addStretch()
        btn_persist = QPushButton("Persist Rules (Save)")
        btn_persist.clicked.connect(self.run_iptables_save_persist)
        list_row.addWidget(btn_persist)
        _g1.addLayout(list_row)


        layout.addWidget(_box1)

        _box2, _g2 = self._group("Add / Delete Rule")

        chain_row = QHBoxLayout()
        chain_row.addWidget(QLabel("Chain:"))
        self.iptables_chain_input = QLineEdit()
        self.iptables_chain_input.setPlaceholderText("e.g. INPUT")
        chain_row.addWidget(self.iptables_chain_input, 1)
        self.iptables_append_check = QCheckBox("Append (unchecked = insert at top)")
        self.iptables_append_check.setChecked(True)
        chain_row.addWidget(self.iptables_append_check)
        _g2.addLayout(chain_row)

        rule_row = QHBoxLayout()
        rule_row.addWidget(QLabel("Rule:"))
        self.iptables_rule_input = QLineEdit()
        self.iptables_rule_input.setPlaceholderText("e.g. -p tcp --dport 22 -j ACCEPT")
        rule_row.addWidget(self.iptables_rule_input, 1)
        btn_add_rule = QPushButton("Add Rule")
        btn_add_rule.clicked.connect(self.run_iptables_add_rule)
        rule_row.addWidget(btn_add_rule)
        _g2.addLayout(rule_row)

        delete_row = QHBoxLayout()
        delete_row.addWidget(QLabel("Rule (or line #) to delete:"))
        self.iptables_delete_input = QLineEdit()
        self.iptables_delete_input.setPlaceholderText("e.g. -p tcp --dport 22 -j ACCEPT, or 3")
        delete_row.addWidget(self.iptables_delete_input, 1)
        btn_delete_rule = QPushButton("Delete Rule")
        btn_delete_rule.clicked.connect(self.run_iptables_delete_rule)
        delete_row.addWidget(btn_delete_rule)
        _g2.addLayout(delete_row)


        layout.addWidget(_box2)

        _box3, _g3 = self._group("Flush")

        flush_row = QHBoxLayout()
        flush_row.addWidget(QLabel("Chain (optional, blank = whole table):"))
        self.iptables_flush_chain_input = QLineEdit()
        flush_row.addWidget(self.iptables_flush_chain_input, 1)
        btn_flush = QPushButton("Flush")
        btn_flush.clicked.connect(self.run_iptables_flush)
        flush_row.addWidget(btn_flush)
        _g3.addLayout(flush_row)

        layout.addWidget(_box3)
        layout.addStretch()
        return panel

    def _build_results_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        self.firewall_status = QLabel("Pick an action above to run it on all checked hosts.")
        self.firewall_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        layout.addWidget(self.firewall_status)

        self.firewall_tabs = QTabWidget()
        self.firewall_tabs.setTabsClosable(True)
        self.firewall_tabs.tabCloseRequested.connect(self._close_firewall_tab)
        shrink_tabwidget_to_current_page(self.firewall_tabs)
        layout.addWidget(self.firewall_tabs)
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
            self.firewall_status.setStyleSheet(f"color: {STATUS_ERROR_COLOR};")
            self.firewall_status.setText(f"Could not load hosts: {e}")
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
    # FIREWALLD ACTIONS
    # =========================================================
    def run_firewalld_status(self):
        self._run_firewall_command(api.cmd_firewalld_status(), "Firewalld Status")

    def run_enable_firewalld(self):
        self._run_firewall_command(api.cmd_set_firewalld_enabled(True), "Enable Firewalld")

    def run_disable_firewalld(self):
        confirm = QMessageBox.question(
            self, "Confirm disabling firewalld",
            "Stop and disable firewalld on all checked hosts? This removes firewall "
            "protection on those hosts until it's re-enabled.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        self._run_firewall_command(api.cmd_set_firewalld_enabled(False), "Disable Firewalld")

    def run_reload_firewalld(self):
        self._run_firewall_command(api.cmd_reload_firewalld(), "Reload Firewalld")

    def run_set_default_zone(self):
        try:
            cmd = api.cmd_set_default_zone(self.default_zone_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_firewall_command(cmd, "Set Default Zone")

    # =========================================================
    # PORTS ACTIONS
    # =========================================================
    def run_list_ports(self):
        self._run_firewall_command(api.cmd_list_ports(self.list_ports_zone_input.text()), "List Ports")

    def run_open_port(self):
        try:
            cmd = api.cmd_open_port(
                self.port_input.text(), self.port_protocol_combo.currentText(),
                self.port_zone_input.text(), self.port_permanent_check.isChecked(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_firewall_command(cmd, "Open Port")

    def run_close_port(self):
        try:
            cmd = api.cmd_close_port(
                self.port_input.text(), self.port_protocol_combo.currentText(),
                self.port_zone_input.text(), self.port_permanent_check.isChecked(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_firewall_command(cmd, "Close Port")

    # =========================================================
    # ZONES ACTIONS
    # =========================================================
    def run_list_zones(self):
        self._run_firewall_command(api.cmd_list_zones(), "List Zones")

    def run_create_zone(self):
        try:
            cmd = api.cmd_create_zone(self.zone_name_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_firewall_command(cmd, "Create Zone")

    def run_delete_zone(self):
        zone_name = self.zone_name_input.text().strip()
        confirm = QMessageBox.question(
            self, "Confirm zone deletion",
            f"Delete zone '{zone_name}' on all checked hosts?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            cmd = api.cmd_delete_zone(zone_name)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_firewall_command(cmd, "Delete Zone")

    # =========================================================
    # RICH RULES ACTIONS
    # =========================================================
    def run_list_rich_rules(self):
        self._run_firewall_command(api.cmd_list_rich_rules(self.list_rich_zone_input.text()), "List Rich Rules")

    def run_add_rich_rule(self):
        try:
            cmd = api.cmd_add_rich_rule(
                self.rich_rule_input.text(), self.rich_rule_zone_input.text(),
                self.rich_rule_permanent_check.isChecked(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_firewall_command(cmd, "Add Rich Rule")

    def run_remove_rich_rule(self):
        confirm = QMessageBox.question(
            self, "Confirm rich rule removal",
            "Remove this rich rule on all checked hosts?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            cmd = api.cmd_remove_rich_rule(
                self.rich_rule_input.text(), self.rich_rule_zone_input.text(),
                self.rich_rule_permanent_check.isChecked(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_firewall_command(cmd, "Remove Rich Rule")

    # =========================================================
    # NFTABLES ACTIONS
    # =========================================================
    def run_nft_list_ruleset(self):
        self._run_firewall_command(api.cmd_nft_list_ruleset(), "List nftables Ruleset")

    def run_nft_flush_ruleset(self):
        confirm = QMessageBox.question(
            self, "Confirm ruleset flush",
            "Flush the ENTIRE nftables ruleset (every table/chain/rule) on all checked "
            "hosts? This is irreversible.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        self._run_firewall_command(api.cmd_nft_flush_ruleset(), "Flush nftables Ruleset")

    def run_nft_add_table(self):
        try:
            cmd = api.cmd_nft_add_table(self.nft_family_combo.currentText(), self.nft_table_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_firewall_command(cmd, "Add nftables Table")

    def run_nft_add_chain(self):
        try:
            cmd = api.cmd_nft_add_chain(
                self.nft_family_combo.currentText(), self.nft_table_input.text(), self.nft_chain_input.text(),
                self.nft_hook_combo.currentText(), self.nft_priority_input.text(), self.nft_policy_combo.currentText(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_firewall_command(cmd, "Add nftables Chain")

    def run_nft_add_rule(self):
        try:
            cmd = api.cmd_nft_add_rule(
                self.nft_family_combo.currentText(), self.nft_table_input.text(),
                self.nft_chain_input.text(), self.nft_rule_input.text(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_firewall_command(cmd, "Add nftables Rule")

    def run_nft_delete_rule(self):
        confirm = QMessageBox.question(
            self, "Confirm rule deletion",
            "Delete this nftables rule (by handle) on all checked hosts?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            cmd = api.cmd_nft_delete_rule(
                self.nft_family_combo.currentText(), self.nft_table_input.text(),
                self.nft_chain_input.text(), self.nft_handle_input.text(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_firewall_command(cmd, "Delete nftables Rule")

    # =========================================================
    # IPTABLES ACTIONS
    # =========================================================
    def run_iptables_list(self):
        self._run_firewall_command(api.cmd_iptables_list(self.iptables_table_combo.currentText()), "List iptables Rules")

    def run_iptables_save_persist(self):
        self._run_firewall_command(api.cmd_iptables_save_persist(), "Persist iptables Rules")

    def run_iptables_add_rule(self):
        try:
            cmd = api.cmd_iptables_add_rule(
                self.iptables_table_combo.currentText(), self.iptables_chain_input.text(),
                self.iptables_rule_input.text(), self.iptables_append_check.isChecked(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_firewall_command(cmd, "Add iptables Rule")

    def run_iptables_delete_rule(self):
        confirm = QMessageBox.question(
            self, "Confirm rule deletion",
            "Delete this iptables rule on all checked hosts?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            cmd = api.cmd_iptables_delete_rule(
                self.iptables_table_combo.currentText(), self.iptables_chain_input.text(),
                self.iptables_delete_input.text(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_firewall_command(cmd, "Delete iptables Rule")

    def run_iptables_flush(self):
        table = self.iptables_table_combo.currentText()
        chain = self.iptables_flush_chain_input.text().strip()
        target = f"chain '{chain}' in table '{table}'" if chain else f"table '{table}'"
        confirm = QMessageBox.question(
            self, "Confirm flush",
            f"Flush {target} on all checked hosts? This is irreversible.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            cmd = api.cmd_iptables_flush(table, chain)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_firewall_command(cmd, "Flush iptables")

    # =========================================================
    # DISPATCH + RESULTS (same pattern as Storage Administration /
    # File System Management / Network Management / Service Management)
    # =========================================================
    def _run_firewall_command(self, command, label):
        entries = self.checked_entries()

        if not entries:
            QMessageBox.information(self, "No hosts checked", "Check one or more target hosts first.")
            return

        self.last_command_label = label
        self.firewall_results = {}
        self.firewall_pending = {}
        self.firewall_tabs.clear()

        for entry in entries:
            key = _entry_key(entry)
            result = api.run_on_entry(entry, command)

            if result["sync"]:
                self.firewall_results[key] = {
                    "label": entry["label"],
                    "stdout": result["stdout"],
                    "stderr": result["stderr"] or result["error"] or "",
                    "code": result["code"],
                    "pending": False,
                }
            elif result["error"]:
                self.firewall_results[key] = {
                    "label": entry["label"], "stdout": "", "stderr": result["error"],
                    "code": None, "pending": False,
                }
            else:
                self.firewall_results[key] = {
                    "label": entry["label"], "stdout": "", "stderr": "",
                    "code": None, "pending": True,
                }
                self.firewall_pending[key] = (entry, result["task_id"])

            self._add_firewall_tab(key)

        self.firewall_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        self.firewall_status.setText(f"Running '{label}' on {len(entries)} host(s)...")

        if self.firewall_tabs.count() > 0:
            self.firewall_tabs.setCurrentIndex(0)

        if self.firewall_pending:
            self.firewall_poll_timer.start(FIREWALL_POLL_MS)
        else:
            self.firewall_status.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
            self.firewall_status.setText(f"'{label}' complete.")

    def _status_text(self, data):
        if data["pending"]:
            return "pending..."
        return "ok" if not data["stderr"] else "error"

    def _render_firewall_result(self, text_edit, data):
        if data["pending"]:
            text_edit.setPlainText("Waiting for this agent host to report back...")
            return

        label = getattr(self, "last_command_label", None) or "Action"
        text_edit.setHtml(result_banner.result_html(
            data, ok_label=f"{label} complete", fail_label=f"{label} failed"))

    def _close_firewall_tab(self, index):
        bar = self.firewall_tabs.tabBar()
        key = bar.tabData(index)
        self.firewall_tabs.removeTab(index)
        self.firewall_results.pop(key, None)
        self.firewall_pending.pop(key, None)

    def _add_firewall_tab(self, key):
        data = self.firewall_results.get(key)
        if not data:
            return
        status = self._status_text(data)

        text_edit = QTextEdit()
        text_edit.setReadOnly(True)
        text_edit.setStyleSheet("font-family: monospace;")
        self._render_firewall_result(text_edit, data)

        idx = self.firewall_tabs.addTab(text_edit, f"{data['label']}  [{status}]")
        self.firewall_tabs.tabBar().setTabData(idx, key)

    def _refresh_firewall_tab(self, key):
        bar = self.firewall_tabs.tabBar()
        for i in range(self.firewall_tabs.count()):
            if bar.tabData(i) != key:
                continue
            data = self.firewall_results.get(key)
            if data:
                status = self._status_text(data)
                self.firewall_tabs.setTabText(i, f"{data['label']}  [{status}]")
                self._render_firewall_result(self.firewall_tabs.widget(i), data)
            return

    def _poll_firewall(self):
        if not self.firewall_pending:
            self.firewall_poll_timer.stop()
            return

        done = []

        for key, (entry, task_id) in list(self.firewall_pending.items()):
            result = api.poll_entry_result(entry, task_id)
            if result is None:
                continue

            self.firewall_results[key] = {
                "label": entry["label"],
                "stdout": result["stdout"],
                "stderr": result["stderr"] or result["error"] or "",
                "code": result["code"],
                "pending": False,
            }
            self._refresh_firewall_tab(key)
            done.append(key)

        for key in done:
            del self.firewall_pending[key]

        if not self.firewall_pending:
            self.firewall_poll_timer.stop()
            self.firewall_status.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
            self.firewall_status.setText("All hosts reported back.")
