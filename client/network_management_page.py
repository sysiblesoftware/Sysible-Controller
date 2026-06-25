from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout,
    QListWidget, QListWidgetItem, QLabel, QPushButton,
    QLineEdit, QTextEdit, QMessageBox, QGroupBox, QTabWidget, QComboBox,
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
NET_POLL_MS = 2000


def _entry_key(entry):
    """Hashable identity for a merged-host entry (api.list_merged_hosts()) -
    agent host_ids and SSH host names live in separate namespaces, so the
    kind has to be part of the key."""
    return (entry["kind"], entry["id"])


class NetworkManagementPage(QWidget):
    """
    Network Management against a *merged* host list (agent-enrolled
    hosts AND SSH-enrolled hosts) - connectivity/DNS/port diagnostics,
    packet capture, and IP/DHCP/DNS/gateway/routing/hostname/bonding/
    teaming/VLAN/bridge/MTU configuration, all dispatched to whichever
    hosts are checked, same as every other System Administration tool.

    Diagnostics (ping, traceroute, DNS lookup, port/socket inspection,
    tcpdump) are built on standard Linux tools every supported distro
    ships or can be detected as missing - they only ever read state,
    so there's no networking-stack assumption to make.

    Everything that actually changes a host's network configuration
    is standardized on nmcli (NetworkManager's CLI) - see client/
    api.py's NETWORK MANAGEMENT section for why that's the one backend
    standardized on here rather than also trying to detect netplan/
    ifupdown/wicked. A host that isn't running NetworkManager gets a
    clear error instead of a silent no-op when a configuration action
    is run against it; the read-only diagnostics still work everywhere
    regardless.
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Network Management")
        self.resize(1400, 860)

        self.net_results = {}   # entry_key -> {label, stdout, stderr, code, pending}
        self.net_pending = {}   # entry_key -> (entry, task_id)
        self.last_command_label = None
        self._collapsed_envs = set()

        main = QVBoxLayout()
        self.setLayout(main)

        main.addLayout(make_page_header("Network Management"))

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
        # ACTIONS (tabbed - far more distinct actions live here than
        # any other System Administration tool, so they're grouped
        # into tabs the same way User & Group Administration's right
        # panel is, rather than one long scrolling column)
        # ---------------------------------------------------------
        action_tabs = QTabWidget()
        action_tabs.addTab(self._build_diagnostics_tab(), "Diagnostics")
        action_tabs.addTab(self._build_addressing_tab(), "Addressing and DHCP")
        action_tabs.addTab(self._build_dns_hostname_tab(), "DNS and Hostname")
        action_tabs.addTab(self._build_routing_tab(), "Gateway and Routing")
        action_tabs.addTab(self._build_advanced_tab(), "Bonding, Teaming, VLANs, Bridges")
        shrink_tabwidget_to_current_page(action_tabs, cap_height=True)
        content_layout.addWidget(action_tabs)

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

        self.net_poll_timer = QTimer()
        self.net_poll_timer.timeout.connect(self._poll_net)

        bus.host_removed.connect(self.load_hosts)

    # =========================================================
    # ACTION PANEL BUILDERS
    # =========================================================
    @staticmethod
    def _group(title):
        box = QGroupBox(title)
        lay = QVBoxLayout(box)
        return box, lay

    def _build_diagnostics_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        hint = QLabel(
            "These only ever read state - safe to run against any host, "
            "regardless of what manages its network configuration."
        )
        theme.style_hint_label(hint)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        ping_row = QHBoxLayout()
        ping_row.addWidget(QLabel("Target host/IP:"))
        self.ping_target_input = QLineEdit()
        self.ping_target_input.setPlaceholderText("e.g. 8.8.8.8 or example.com")
        ping_row.addWidget(self.ping_target_input, 1)
        btn_ping = QPushButton("Ping")
        btn_ping.clicked.connect(self.run_ping)
        ping_row.addWidget(btn_ping)
        btn_traceroute = QPushButton("Traceroute")
        btn_traceroute.clicked.connect(self.run_traceroute)
        ping_row.addWidget(btn_traceroute)
        layout.addLayout(ping_row)

        dns_row = QHBoxLayout()
        dns_row.addWidget(QLabel("DNS name:"))
        self.dns_name_input = QLineEdit()
        self.dns_name_input.setPlaceholderText("e.g. example.com")
        dns_row.addWidget(self.dns_name_input, 1)
        dns_row.addWidget(QLabel("DNS server (optional):"))
        self.dns_server_input = QLineEdit()
        self.dns_server_input.setMaximumWidth(140)
        dns_row.addWidget(self.dns_server_input)
        btn_dns = QPushButton("Test DNS Resolution")
        btn_dns.clicked.connect(self.run_dns_lookup)
        dns_row.addWidget(btn_dns)
        layout.addLayout(dns_row)

        sockets_row = QHBoxLayout()
        btn_monitor_ports = QPushButton("Monitor Ports (all active sockets)")
        btn_monitor_ports.clicked.connect(self.run_monitor_ports)
        sockets_row.addWidget(btn_monitor_ports)
        btn_listening = QPushButton("Check Listening Services")
        btn_listening.clicked.connect(self.run_listening_services)
        sockets_row.addWidget(btn_listening)
        layout.addLayout(sockets_row)

        capture_row = QHBoxLayout()
        capture_row.addWidget(QLabel("Interface:"))
        self.capture_iface_input = QLineEdit()
        self.capture_iface_input.setPlaceholderText("blank = any")
        self.capture_iface_input.setMaximumWidth(90)
        capture_row.addWidget(self.capture_iface_input)
        capture_row.addWidget(QLabel("Count:"))
        self.capture_count_input = QLineEdit("50")
        self.capture_count_input.setMaximumWidth(55)
        capture_row.addWidget(self.capture_count_input)
        capture_row.addWidget(QLabel("Timeout (s):"))
        self.capture_timeout_input = QLineEdit("10")
        self.capture_timeout_input.setMaximumWidth(55)
        capture_row.addWidget(self.capture_timeout_input)
        capture_row.addWidget(QLabel("Filter (optional):"))
        self.capture_filter_input = QLineEdit()
        self.capture_filter_input.setPlaceholderText("e.g. port 80 or host 10.0.0.5")
        capture_row.addWidget(self.capture_filter_input, 1)
        btn_capture = QPushButton("Capture Packets (tcpdump)")
        btn_capture.clicked.connect(self.run_tcpdump)
        capture_row.addWidget(btn_capture)
        layout.addLayout(capture_row)

        capture_hint = QLabel(
            "Capture is text output only (no .pcap file), capped by Count and Timeout."
        )
        theme.style_hint_label(capture_hint)
        layout.addWidget(capture_hint)

        layout.addStretch()
        return panel

    def _build_addressing_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        lookup_row = QHBoxLayout()
        btn_show_conns = QPushButton("Show Connections")
        btn_show_conns.clicked.connect(self.run_show_connections)
        lookup_row.addWidget(btn_show_conns)
        btn_show_devices = QPushButton("Show Device Status")
        btn_show_devices.clicked.connect(self.run_show_devices)
        lookup_row.addWidget(btn_show_devices)
        lookup_row.addWidget(QLabel("Interface (optional):"))
        self.show_ip_iface_input = QLineEdit()
        self.show_ip_iface_input.setMaximumWidth(90)
        lookup_row.addWidget(self.show_ip_iface_input)
        btn_show_ip = QPushButton("Show IP Configuration")
        btn_show_ip.clicked.connect(self.run_show_ip_config)
        lookup_row.addWidget(btn_show_ip)
        layout.addLayout(lookup_row)

        hint = QLabel(
            "Connection name below must match an existing nmcli connection profile - "
            "use Show Connections to find it. Configuration actions require "
            "NetworkManager active on the target host."
        )
        theme.style_hint_label(hint)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        conn_row = QHBoxLayout()
        conn_row.addWidget(QLabel("Connection name:"))
        self.addr_connection_input = QLineEdit()
        self.addr_connection_input.setPlaceholderText("e.g. eth0 or 'Wired connection 1'")
        conn_row.addWidget(self.addr_connection_input, 1)
        layout.addLayout(conn_row)

        static_row = QHBoxLayout()
        static_row.addWidget(QLabel("Static IP/CIDR:"))
        self.static_ip_input = QLineEdit()
        self.static_ip_input.setPlaceholderText("e.g. 192.168.1.50/24")
        static_row.addWidget(self.static_ip_input, 1)
        static_row.addWidget(QLabel("Gateway (optional):"))
        self.static_gateway_input = QLineEdit()
        self.static_gateway_input.setMaximumWidth(120)
        static_row.addWidget(self.static_gateway_input)
        static_row.addWidget(QLabel("DNS (optional):"))
        self.static_dns_input = QLineEdit()
        self.static_dns_input.setMaximumWidth(140)
        static_row.addWidget(self.static_dns_input)
        btn_static = QPushButton("Apply Static IP")
        btn_static.clicked.connect(self.run_configure_static_ip)
        static_row.addWidget(btn_static)
        layout.addLayout(static_row)

        dhcp_row = QHBoxLayout()
        btn_dhcp = QPushButton("Switch to DHCP")
        btn_dhcp.clicked.connect(self.run_configure_dhcp)
        dhcp_row.addWidget(btn_dhcp)
        dhcp_row.addStretch()
        dhcp_row.addWidget(QLabel("MTU:"))
        self.mtu_input = QLineEdit("1500")
        self.mtu_input.setMaximumWidth(70)
        dhcp_row.addWidget(self.mtu_input)
        btn_mtu = QPushButton("Apply MTU")
        btn_mtu.clicked.connect(self.run_set_mtu)
        dhcp_row.addWidget(btn_mtu)
        layout.addLayout(dhcp_row)

        layout.addStretch()
        return panel

    def _build_dns_hostname_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        dns_row = QHBoxLayout()
        dns_row.addWidget(QLabel("Connection name:"))
        self.dns_connection_input = QLineEdit()
        self.dns_connection_input.setPlaceholderText("e.g. eth0")
        dns_row.addWidget(self.dns_connection_input, 1)
        dns_row.addWidget(QLabel("DNS servers:"))
        self.dns_servers_input = QLineEdit()
        self.dns_servers_input.setPlaceholderText("e.g. 8.8.8.8 1.1.1.1")
        dns_row.addWidget(self.dns_servers_input, 1)
        btn_dns_apply = QPushButton("Apply DNS Settings")
        btn_dns_apply.clicked.connect(self.run_set_dns)
        dns_row.addWidget(btn_dns_apply)
        layout.addLayout(dns_row)


        host_row = QHBoxLayout()
        btn_show_hostname = QPushButton("Show Current Hostname")
        btn_show_hostname.clicked.connect(self.run_show_hostname)
        host_row.addWidget(btn_show_hostname)
        host_row.addWidget(QLabel("New hostname:"))
        self.new_hostname_input = QLineEdit()
        self.new_hostname_input.setPlaceholderText("e.g. web-server-01")
        host_row.addWidget(self.new_hostname_input, 1)
        btn_set_hostname = QPushButton("Set Hostname")
        btn_set_hostname.clicked.connect(self.run_set_hostname)
        host_row.addWidget(btn_set_hostname)
        layout.addLayout(host_row)

        layout.addStretch()
        return panel

    def _build_routing_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        gw_row = QHBoxLayout()
        gw_row.addWidget(QLabel("Connection name:"))
        self.gateway_connection_input = QLineEdit()
        gw_row.addWidget(self.gateway_connection_input, 1)
        gw_row.addWidget(QLabel("Gateway IP:"))
        self.gateway_ip_input = QLineEdit()
        self.gateway_ip_input.setMaximumWidth(140)
        gw_row.addWidget(self.gateway_ip_input)
        btn_gateway = QPushButton("Apply Gateway")
        btn_gateway.clicked.connect(self.run_set_gateway)
        gw_row.addWidget(btn_gateway)
        layout.addLayout(gw_row)

        layout.addSpacing(10)

        btn_routes = QPushButton("Show Routing Table")
        btn_routes.clicked.connect(self.run_show_routes)
        layout.addWidget(btn_routes)

        layout.addSpacing(10)

        route_row = QHBoxLayout()
        route_row.addWidget(QLabel("Connection name:"))
        self.route_connection_input = QLineEdit()
        route_row.addWidget(self.route_connection_input, 1)
        route_row.addWidget(QLabel("Destination CIDR:"))
        self.route_dest_input = QLineEdit()
        self.route_dest_input.setPlaceholderText("e.g. 10.0.5.0/24")
        route_row.addWidget(self.route_dest_input, 1)
        route_row.addWidget(QLabel("Via gateway:"))
        self.route_gateway_input = QLineEdit()
        self.route_gateway_input.setMaximumWidth(140)
        route_row.addWidget(self.route_gateway_input)
        btn_add_route = QPushButton("Add Static Route")
        btn_add_route.clicked.connect(self.run_add_static_route)
        route_row.addWidget(btn_add_route)
        layout.addLayout(route_row)

        layout.addStretch()
        return panel

    def _build_advanced_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        warn = QLabel(
            "These create new network interfaces on the target host(s) - double-check "
            "interface names (Show Device Status, in Addressing and DHCP) before applying."
        )
        theme.style_hint_label(warn)
        warn.setWordWrap(True)
        layout.addWidget(warn)

        _box1, _g1 = self._group("Bonding")
        bond_row = QHBoxLayout()
        bond_row.addWidget(QLabel("Bond name:"))
        self.bond_name_input = QLineEdit("bond0")
        self.bond_name_input.setMaximumWidth(90)
        bond_row.addWidget(self.bond_name_input)
        bond_row.addWidget(QLabel("Mode:"))
        self.bond_mode_combo = QComboBox()
        self.bond_mode_combo.addItems([
            "active-backup", "balance-rr", "balance-xor", "broadcast",
            "802.3ad", "balance-tlb", "balance-alb",
        ])
        bond_row.addWidget(self.bond_mode_combo)
        bond_row.addWidget(QLabel("Slave interfaces:"))
        self.bond_slaves_input = QLineEdit()
        self.bond_slaves_input.setPlaceholderText("e.g. eth0 eth1")
        bond_row.addWidget(self.bond_slaves_input, 1)
        btn_bond = QPushButton("Create Bond")
        btn_bond.clicked.connect(self.run_configure_bonding)
        bond_row.addWidget(btn_bond)
        _g1.addLayout(bond_row)

        layout.addSpacing(12)

        layout.addWidget(_box1)

        _box2, _g2 = self._group("Teaming")
        team_row = QHBoxLayout()
        team_row.addWidget(QLabel("Team name:"))
        self.team_name_input = QLineEdit("team0")
        self.team_name_input.setMaximumWidth(90)
        team_row.addWidget(self.team_name_input)
        team_row.addWidget(QLabel("Runner:"))
        self.team_runner_combo = QComboBox()
        self.team_runner_combo.addItems(
            ["roundrobin", "activebackup", "loadbalance", "lacp", "broadcast"]
        )
        team_row.addWidget(self.team_runner_combo)
        team_row.addWidget(QLabel("Slave interfaces:"))
        self.team_slaves_input = QLineEdit()
        self.team_slaves_input.setPlaceholderText("e.g. eth0 eth1")
        team_row.addWidget(self.team_slaves_input, 1)
        btn_team = QPushButton("Create Team")
        btn_team.clicked.connect(self.run_configure_teaming)
        team_row.addWidget(btn_team)
        _g2.addLayout(team_row)

        layout.addSpacing(12)

        layout.addWidget(_box2)

        _box3, _g3 = self._group("VLANs")
        vlan_row = QHBoxLayout()
        vlan_row.addWidget(QLabel("Parent interface:"))
        self.vlan_parent_input = QLineEdit()
        self.vlan_parent_input.setPlaceholderText("e.g. eth0")
        self.vlan_parent_input.setMaximumWidth(90)
        vlan_row.addWidget(self.vlan_parent_input)
        vlan_row.addWidget(QLabel("VLAN ID:"))
        self.vlan_id_input = QLineEdit()
        self.vlan_id_input.setMaximumWidth(55)
        vlan_row.addWidget(self.vlan_id_input)
        vlan_row.addWidget(QLabel("VLAN interface name (optional):"))
        self.vlan_name_input = QLineEdit()
        self.vlan_name_input.setPlaceholderText("default: <parent>.<id>")
        vlan_row.addWidget(self.vlan_name_input, 1)
        btn_vlan = QPushButton("Create VLAN")
        btn_vlan.clicked.connect(self.run_configure_vlan)
        vlan_row.addWidget(btn_vlan)
        _g3.addLayout(vlan_row)

        layout.addSpacing(12)

        layout.addWidget(_box3)

        _box4, _g4 = self._group("Bridges")
        bridge_row = QHBoxLayout()
        bridge_row.addWidget(QLabel("Bridge name:"))
        self.bridge_name_input = QLineEdit("br0")
        self.bridge_name_input.setMaximumWidth(90)
        bridge_row.addWidget(self.bridge_name_input)
        bridge_row.addWidget(QLabel("Slave interfaces:"))
        self.bridge_slaves_input = QLineEdit()
        self.bridge_slaves_input.setPlaceholderText("e.g. eth0 eth1")
        bridge_row.addWidget(self.bridge_slaves_input, 1)
        btn_bridge = QPushButton("Create Bridge")
        btn_bridge.clicked.connect(self.run_configure_bridge)
        bridge_row.addWidget(btn_bridge)
        _g4.addLayout(bridge_row)

        layout.addWidget(_box4)
        layout.addStretch()
        return panel

    def clear_all_results(self):
        """Close every per-host result tab at once."""
        self.net_tabs.clear()
        self.net_results = {}
        self.net_pending = {}

    def _build_results_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        self.net_status = QLabel("Pick an action above to run it on all checked hosts.")
        self.net_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        _hdr = QHBoxLayout()
        _hdr.addWidget(self.net_status)
        _hdr.addStretch()
        _btn_clear_all = QPushButton("Clear All Results")
        _btn_clear_all.setToolTip("Close every per-host result tab below.")
        _btn_clear_all.clicked.connect(self.clear_all_results)
        _hdr.addWidget(_btn_clear_all)
        layout.addLayout(_hdr)

        self.net_tabs = QTabWidget()
        self.net_tabs.setTabsClosable(True)
        self.net_tabs.tabCloseRequested.connect(self._close_net_tab)
        shrink_tabwidget_to_current_page(self.net_tabs)
        layout.addWidget(self.net_tabs)
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
            self.net_status.setStyleSheet(f"color: {STATUS_ERROR_COLOR};")
            self.net_status.setText(f"Could not load hosts: {e}")
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
    # DIAGNOSTICS ACTIONS
    # =========================================================
    def run_ping(self):
        target = self.ping_target_input.text().strip()
        try:
            cmd = api.cmd_ping(target)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_net_command(cmd, f"Ping '{target}'")

    def run_traceroute(self):
        target = self.ping_target_input.text().strip()
        try:
            cmd = api.cmd_traceroute(target)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_net_command(cmd, f"Traceroute '{target}'")

    def run_dns_lookup(self):
        name = self.dns_name_input.text().strip()
        server = self.dns_server_input.text().strip()
        try:
            cmd = api.cmd_dns_lookup(name, server)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_net_command(cmd, f"DNS Lookup '{name}'")

    def run_monitor_ports(self):
        self._run_net_command(api.cmd_monitor_ports(), "Monitor Ports")

    def run_listening_services(self):
        self._run_net_command(api.cmd_listening_services(), "Listening Services")

    def run_tcpdump(self):
        try:
            cmd = api.cmd_tcpdump_capture(
                self.capture_iface_input.text(),
                self.capture_count_input.text() or 50,
                self.capture_timeout_input.text() or 10,
                self.capture_filter_input.text(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_net_command(cmd, "Packet Capture (tcpdump)")

    # =========================================================
    # ADDRESSING / DHCP / MTU ACTIONS
    # =========================================================
    def run_show_connections(self):
        self._run_net_command(api.cmd_list_connections(), "Show Connections")

    def run_show_devices(self):
        self._run_net_command(api.cmd_list_devices(), "Show Device Status")

    def run_show_ip_config(self):
        try:
            cmd = api.cmd_show_ip_config(self.show_ip_iface_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_net_command(cmd, "Show IP Configuration")

    def run_configure_static_ip(self):
        try:
            cmd = api.cmd_configure_static_ip(
                self.addr_connection_input.text(),
                self.static_ip_input.text(),
                self.static_gateway_input.text(),
                self.static_dns_input.text(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_net_command(cmd, "Apply Static IP")

    def run_configure_dhcp(self):
        try:
            cmd = api.cmd_configure_dhcp(self.addr_connection_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_net_command(cmd, "Switch to DHCP")

    def run_set_mtu(self):
        try:
            cmd = api.cmd_set_mtu(self.addr_connection_input.text(), self.mtu_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_net_command(cmd, "Apply MTU")

    # =========================================================
    # DNS / HOSTNAME ACTIONS
    # =========================================================
    def run_set_dns(self):
        try:
            cmd = api.cmd_set_dns(self.dns_connection_input.text(), self.dns_servers_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_net_command(cmd, "Apply DNS Settings")

    def run_show_hostname(self):
        self._run_net_command(api.cmd_show_hostname(), "Show Hostname")

    def run_set_hostname(self):
        new_name = self.new_hostname_input.text().strip()
        try:
            cmd = api.cmd_set_hostname(new_name)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_net_command(cmd, f"Set Hostname '{new_name}'")

    # =========================================================
    # GATEWAY / ROUTING ACTIONS
    # =========================================================
    def run_set_gateway(self):
        try:
            cmd = api.cmd_set_gateway(self.gateway_connection_input.text(), self.gateway_ip_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_net_command(cmd, "Apply Gateway")

    def run_show_routes(self):
        self._run_net_command(api.cmd_show_routes(), "Show Routing Table")

    def run_add_static_route(self):
        try:
            cmd = api.cmd_add_static_route(
                self.route_connection_input.text(),
                self.route_dest_input.text(),
                self.route_gateway_input.text(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_net_command(cmd, "Add Static Route")

    # =========================================================
    # BONDING / TEAMING / VLAN / BRIDGE ACTIONS
    # =========================================================
    def run_configure_bonding(self):
        try:
            cmd = api.cmd_configure_bonding(
                self.bond_name_input.text(),
                self.bond_mode_combo.currentText(),
                self.bond_slaves_input.text(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_net_command(cmd, "Create Bond")

    def run_configure_teaming(self):
        try:
            cmd = api.cmd_configure_teaming(
                self.team_name_input.text(),
                self.team_runner_combo.currentText(),
                self.team_slaves_input.text(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_net_command(cmd, "Create Team")

    def run_configure_vlan(self):
        try:
            cmd = api.cmd_configure_vlan(
                self.vlan_parent_input.text(),
                self.vlan_id_input.text(),
                self.vlan_name_input.text(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_net_command(cmd, "Create VLAN")

    def run_configure_bridge(self):
        try:
            cmd = api.cmd_configure_bridge(self.bridge_name_input.text(), self.bridge_slaves_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_net_command(cmd, "Create Bridge")

    # =========================================================
    # DISPATCH + RESULTS (same pattern as Host Software Management /
    # Service Management)
    # =========================================================
    def _run_net_command(self, command, label):
        entries = self.checked_entries()

        if not entries:
            QMessageBox.information(self, "No hosts checked", "Check one or more target hosts first.")
            return

        self.last_command_label = label
        self.net_results = {}
        self.net_pending = {}
        self.net_tabs.clear()

        for entry in entries:
            key = _entry_key(entry)
            result = api.run_on_entry(entry, command)

            if result["sync"]:
                self.net_results[key] = {
                    "label": entry["label"],
                    "stdout": result["stdout"],
                    "stderr": result["stderr"] or result["error"] or "",
                    "code": result["code"],
                    "pending": False,
                }
            elif result["error"]:
                self.net_results[key] = {
                    "label": entry["label"], "stdout": "", "stderr": result["error"],
                    "code": None, "pending": False,
                }
            else:
                self.net_results[key] = {
                    "label": entry["label"], "stdout": "", "stderr": "",
                    "code": None, "pending": True,
                }
                self.net_pending[key] = (entry, result["task_id"])

            self._add_net_tab(key)

        self.net_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        self.net_status.setText(f"Running '{label}' on {len(entries)} host(s)...")

        if self.net_tabs.count() > 0:
            self.net_tabs.setCurrentIndex(0)

        if self.net_pending:
            self.net_poll_timer.start(NET_POLL_MS)
        else:
            self.net_status.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
            self.net_status.setText(f"'{label}' complete.")

    def _status_text(self, data):
        if data["pending"]:
            return "pending..."
        # Success is the exit code where we have one (the same rule the
        # result banner uses); stderr alone is NOT failure - many commands
        # write progress/warnings to stderr on success.
        code = data.get("code")
        failed = (code != 0) if code is not None else (bool(data["stderr"]) and not data["stdout"])
        return "error" if failed else "ok"

    def _render_net_result(self, text_edit, data):
        if data["pending"]:
            text_edit.setPlainText("Waiting for this agent host to report back...")
            return

        label = getattr(self, "last_command_label", None) or "Action"
        text_edit.setHtml(result_banner.result_html(
            data, ok_label=f"{label} complete", fail_label=f"{label} failed"))

    def _close_net_tab(self, index):
        bar = self.net_tabs.tabBar()
        key = bar.tabData(index)
        self.net_tabs.removeTab(index)
        self.net_results.pop(key, None)
        self.net_pending.pop(key, None)

    def _add_net_tab(self, key):
        data = self.net_results.get(key)
        if not data:
            return
        status = self._status_text(data)

        text_edit = QTextEdit()
        text_edit.setReadOnly(True)
        text_edit.setStyleSheet("font-family: monospace;")
        self._render_net_result(text_edit, data)

        idx = self.net_tabs.addTab(text_edit, f"{data['label']}  [{status}]")
        self.net_tabs.tabBar().setTabData(idx, key)

    def _refresh_net_tab(self, key):
        bar = self.net_tabs.tabBar()
        for i in range(self.net_tabs.count()):
            if bar.tabData(i) != key:
                continue
            data = self.net_results.get(key)
            if data:
                status = self._status_text(data)
                self.net_tabs.setTabText(i, f"{data['label']}  [{status}]")
                self._render_net_result(self.net_tabs.widget(i), data)
            return

    def _poll_net(self):
        if not self.net_pending:
            self.net_poll_timer.stop()
            return

        done = []

        for key, (entry, task_id) in list(self.net_pending.items()):
            result = api.poll_entry_result(entry, task_id)
            if result is None:
                continue

            self.net_results[key] = {
                "label": entry["label"],
                "stdout": result["stdout"],
                "stderr": result["stderr"] or result["error"] or "",
                "code": result["code"],
                "pending": False,
            }
            self._refresh_net_tab(key)
            done.append(key)

        for key in done:
            del self.net_pending[key]

        if not self.net_pending:
            self.net_poll_timer.stop()
            self.net_status.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
            self.net_status.setText("All hosts reported back.")
