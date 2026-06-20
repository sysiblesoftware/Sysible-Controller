"""NETWORK MANAGEMENT dual-host command builders - split out of
client/api.py to keep individual file sizes manageable. Imported via
`from client._api_network import *` at the bottom of client/api.py.

Two different families of action live here:
  - Diagnostics/monitoring (ping, traceroute, DNS lookups, socket
    inspection, packet capture) only ever read state, so they're built
    straight on standard Linux tools every distro already ships.
  - Everything that actually *changes* a host's network configuration
    (addressing, DHCP/static, DNS, gateway, routing, hostname,
    bonding/teaming/VLANs/bridges, MTU) is standardized on nmcli
    (NetworkManager's CLI) as the one cross-distro backend. A host
    that genuinely isn't running NetworkManager gets a clear, specific
    error instead of a command that silently no-ops - see
    _require_nmcli_fragment().
"""
import json
import re
import shlex


def _require_nmcli_fragment() -> str:
    """Shell fragment that exits with a clear stderr message instead
    of a bare "command not found" if either nmcli itself isn't
    installed, or NetworkManager is installed but not actually
    running."""
    return (
        "if ! command -v nmcli >/dev/null 2>&1; then "
        "echo 'NetworkManager (nmcli) is not installed on this host - network "
        "configuration here is standardized on NetworkManager, so install/enable "
        "it on this host first.' >&2; exit 1; fi; "
        "if ! nmcli -t -f RUNNING general status 2>/dev/null | grep -q '^running$'; then "
        "echo 'NetworkManager is installed on this host but is not running "
        "(check: systemctl status NetworkManager) - start and enable it before "
        "configuring networking here.' >&2; exit 1; fi; "
    )


_SAFE_IFACE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")


def _validate_iface(name: str, label: str = "Interface name") -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError(f"{label} is required.")
    if not _SAFE_IFACE_RE.match(name):
        raise ValueError(
            f"{label} may only contain letters, numbers, dots, dashes, and "
            "underscores, and must be 15 characters or fewer (the Linux "
            "interface name limit)."
        )
    return name


def _validate_connection_name(name: str) -> str:
    """nmcli connection *profile* names are admin-chosen labels - this
    only rejects blank input rather than restricting the character
    set."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Connection name is required (use Show Connections to find it).")
    return name


_SAFE_HOSTNAME_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)


def _validate_hostname(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("Hostname is required.")
    if len(name) > 253 or not _SAFE_HOSTNAME_RE.match(name):
        raise ValueError("That doesn't look like a valid hostname.")
    return name


def _validate_int_range(value, lo: int, hi: int, label: str) -> int:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a whole number.")
    if not (lo <= n <= hi):
        raise ValueError(f"{label} must be between {lo} and {hi}.")
    return n


def _ifaces_list(text: str, label: str = "Interface(s)") -> list:
    names = (text or "").split()
    if not names:
        raise ValueError(f"{label} is required (space-separated for more than one).")
    return [_validate_iface(n, label) for n in names]


# --- diagnostics / monitoring (universal tools) -------------------------

def cmd_ping(target: str, count=4) -> str:
    target = (target or "").strip()
    if not target:
        raise ValueError("Target host or IP is required.")
    count = _validate_int_range(count, 1, 20, "Ping count")
    q = shlex.quote(target)
    return f"ping -c {count} -W 2 {q} 2>&1"


def cmd_traceroute(target: str) -> str:
    target = (target or "").strip()
    if not target:
        raise ValueError("Target host or IP is required.")
    q = shlex.quote(target)
    return (
        "if command -v traceroute >/dev/null 2>&1; then "
        f"traceroute -m 15 -w 2 {q} 2>&1; "
        "elif command -v tracepath >/dev/null 2>&1; then "
        f"tracepath {q} 2>&1; "
        "else echo 'Neither traceroute nor tracepath found on this host.' >&2; exit 1; fi"
    )


def cmd_dns_lookup(name: str, server: str = "") -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("A hostname to resolve is required.")
    q_name = shlex.quote(name)
    server = (server or "").strip()
    q_server = shlex.quote(server) if server else ""

    return (
        f"if command -v dig >/dev/null 2>&1; then dig {q_name} {('@' + q_server) if server else ''} 2>&1; "
        f"elif command -v host >/dev/null 2>&1; then host {q_name} {q_server} 2>&1; "
        f"elif command -v nslookup >/dev/null 2>&1; then nslookup {q_name} {q_server} 2>&1; "
        f"else getent hosts {q_name} 2>&1 || echo 'No DNS lookup tool (dig/host/nslookup/getent) found on this host.'; fi"
    )


def cmd_monitor_ports() -> str:
    """All active sockets - listening AND established - TCP+UDP."""
    return (
        "if command -v ss >/dev/null 2>&1; then ss -tunap 2>&1; "
        "elif command -v netstat >/dev/null 2>&1; then netstat -tunap 2>&1; "
        "else echo 'Neither ss nor netstat found on this host.' >&2; exit 1; fi"
    )


def cmd_listening_services() -> str:
    """Just the LISTEN-state sockets - the more direct answer to "what
    services are exposed on this host"."""
    return (
        "if command -v ss >/dev/null 2>&1; then ss -tulpn 2>&1; "
        "elif command -v netstat >/dev/null 2>&1; then netstat -tulpn 2>&1; "
        "else echo 'Neither ss nor netstat found on this host.' >&2; exit 1; fi"
    )


def cmd_tcpdump_capture(iface: str = "", count=50, timeout_s=10, filter_expr: str = "") -> str:
    """Text-mode capture, capped on both packet count and wall-clock
    time so a checked fleet of hosts can't be left tcpdump-ing
    indefinitely."""
    iface_value = (iface or "").strip()
    iface_part = f"-i {shlex.quote(_validate_iface(iface_value))}" if iface_value else "-i any"
    count = _validate_int_range(count, 1, 500, "Packet count")
    timeout_s = _validate_int_range(timeout_s, 1, 60, "Timeout (seconds)")
    filter_value = (filter_expr or "").strip()
    q_filter = shlex.quote(filter_value) if filter_value else ""
    return (
        "if ! command -v tcpdump >/dev/null 2>&1; then "
        "echo 'tcpdump is not installed on this host.' >&2; exit 1; fi; "
        f"timeout {timeout_s} tcpdump {iface_part} -nn -c {count} {q_filter} 2>&1"
    )


# --- addressing / DHCP / MTU (nmcli) -------------------------------------

def cmd_list_connections() -> str:
    return f"{_require_nmcli_fragment()}nmcli connection show 2>&1"


def cmd_list_devices() -> str:
    return f"{_require_nmcli_fragment()}nmcli device status 2>&1"


def cmd_show_ip_config(iface: str = "") -> str:
    iface_value = (iface or "").strip()
    if iface_value:
        q = shlex.quote(_validate_iface(iface_value))
        return f"{_require_nmcli_fragment()}nmcli device show {q} 2>&1"
    return f"{_require_nmcli_fragment()}nmcli device show 2>&1"


def cmd_configure_static_ip(connection: str, ip_cidr: str, gateway: str = "", dns: str = "") -> str:
    """Covers both "Configure IP addresses" and "Configure static
    networking" - on nmcli they're the same operation."""
    conn = _validate_connection_name(connection)
    ip_cidr = (ip_cidr or "").strip()
    if not ip_cidr:
        raise ValueError("IP address (CIDR form, e.g. 192.168.1.50/24) is required.")
    q_conn = shlex.quote(conn)
    q_ip = shlex.quote(ip_cidr)

    extra = ""
    gateway = (gateway or "").strip()
    if gateway:
        extra += f" ipv4.gateway {shlex.quote(gateway)}"
    dns = (dns or "").strip()
    if dns:
        extra += f" ipv4.dns {shlex.quote(dns)}"

    return (
        f"{_require_nmcli_fragment()}"
        f"nmcli connection modify {q_conn} ipv4.method manual ipv4.addresses {q_ip}{extra} "
        f"&& nmcli connection up {q_conn} 2>&1"
    )


def cmd_configure_dhcp(connection: str) -> str:
    conn = _validate_connection_name(connection)
    q_conn = shlex.quote(conn)
    return (
        f"{_require_nmcli_fragment()}"
        f'nmcli connection modify {q_conn} ipv4.method auto ipv4.addresses "" ipv4.gateway "" '
        f"&& nmcli connection up {q_conn} 2>&1"
    )


def cmd_set_mtu(connection: str, mtu) -> str:
    conn = _validate_connection_name(connection)
    mtu = _validate_int_range(mtu, 68, 9000, "MTU")
    q_conn = shlex.quote(conn)
    return (
        f"{_require_nmcli_fragment()}"
        f"nmcli connection modify {q_conn} 802-3-ethernet.mtu {mtu} "
        f"&& nmcli connection up {q_conn} 2>&1"
    )


# --- DNS / hostname -------------------------------------------------------

def cmd_set_dns(connection: str, dns_servers: str) -> str:
    conn = _validate_connection_name(connection)
    dns_servers = (dns_servers or "").strip()
    if not dns_servers:
        raise ValueError("At least one DNS server is required (space-separated for more than one).")
    q_conn = shlex.quote(conn)
    q_dns = shlex.quote(dns_servers)
    return (
        f"{_require_nmcli_fragment()}"
        f"nmcli connection modify {q_conn} ipv4.dns {q_dns} ipv4.ignore-auto-dns yes "
        f"&& nmcli connection up {q_conn} 2>&1"
    )


def cmd_show_hostname() -> str:
    return "hostnamectl status 2>/dev/null || hostname 2>&1"


def cmd_set_hostname(new_hostname: str) -> str:
    name = _validate_hostname(new_hostname)
    q = shlex.quote(name)
    return (
        "if command -v hostnamectl >/dev/null 2>&1; then hostnamectl set-hostname "
        f"{q} 2>&1; else hostname {q} && echo {q} > /etc/hostname; fi"
    )


# --- gateway / routing ------------------------------------------------------

def cmd_set_gateway(connection: str, gateway: str) -> str:
    conn = _validate_connection_name(connection)
    gateway = (gateway or "").strip()
    if not gateway:
        raise ValueError("Gateway IP address is required.")
    q_conn = shlex.quote(conn)
    q_gw = shlex.quote(gateway)
    return (
        f"{_require_nmcli_fragment()}"
        f"nmcli connection modify {q_conn} ipv4.gateway {q_gw} "
        f"&& nmcli connection up {q_conn} 2>&1"
    )


def cmd_show_routes() -> str:
    """Plain `ip route` - deliberately NOT behind the nmcli guard,
    since the routing table itself is kernel state any Linux host has
    regardless of which tool manages its network config."""
    return (
        "if command -v ip >/dev/null 2>&1; then ip route show 2>&1; "
        "else netstat -rn 2>&1; fi"
    )


def cmd_add_static_route(connection: str, destination_cidr: str, via_gateway: str) -> str:
    conn = _validate_connection_name(connection)
    destination_cidr = (destination_cidr or "").strip()
    via_gateway = (via_gateway or "").strip()
    if not destination_cidr:
        raise ValueError("Destination network (CIDR form, e.g. 10.0.5.0/24) is required.")
    if not via_gateway:
        raise ValueError("Via gateway IP is required.")
    q_conn = shlex.quote(conn)
    q_route = shlex.quote(f"{destination_cidr} {via_gateway}")
    return (
        f"{_require_nmcli_fragment()}"
        f"nmcli connection modify {q_conn} +ipv4.routes {q_route} "
        f"&& nmcli connection up {q_conn} 2>&1"
    )


# --- bonding / teaming / VLANs / bridges ------------------------------------

def _nmcli_add_slaves_fragment(master: str, slave_ifaces: list, master_type: str) -> str:
    parts = []
    for s in slave_ifaces:
        q_slave_name = shlex.quote(f"{s}-{master_type}-slave")
        q_slave_dev = shlex.quote(s)
        q_master = shlex.quote(master)
        parts.append(
            f"nmcli connection add type ethernet con-name {q_slave_name} "
            f"ifname {q_slave_dev} master {q_master} && "
            f"nmcli connection up {q_slave_name}"
        )
    return " && ".join(parts)


_VALID_BOND_MODES = {
    "balance-rr", "active-backup", "balance-xor", "broadcast",
    "802.3ad", "balance-tlb", "balance-alb",
}


def cmd_configure_bonding(bond_name: str, mode: str, slave_ifaces: str) -> str:
    bond_name = _validate_iface(bond_name, "Bond name")
    mode = (mode or "").strip() or "active-backup"
    if mode not in _VALID_BOND_MODES:
        raise ValueError(f"Unknown bond mode '{mode}'.")
    slaves = _ifaces_list(slave_ifaces, "Slave interface(s)")
    q_bond = shlex.quote(bond_name)
    slaves_fragment = _nmcli_add_slaves_fragment(bond_name, slaves, "bond")

    return (
        f"{_require_nmcli_fragment()}"
        f'nmcli connection add type bond con-name {q_bond} ifname {q_bond} '
        f'bond.options "mode={mode}" && '
        f"{slaves_fragment} && "
        f"nmcli connection up {q_bond} 2>&1"
    )


_VALID_TEAM_RUNNERS = {"roundrobin", "activebackup", "loadbalance", "lacp", "broadcast"}


def cmd_configure_teaming(team_name: str, runner: str, slave_ifaces: str) -> str:
    team_name = _validate_iface(team_name, "Team name")
    runner = (runner or "").strip() or "roundrobin"
    if runner not in _VALID_TEAM_RUNNERS:
        raise ValueError(f"Unknown team runner '{runner}'.")
    slaves = _ifaces_list(slave_ifaces, "Slave interface(s)")
    q_team = shlex.quote(team_name)

    team_config = json.dumps({"runner": {"name": runner}})
    q_config = shlex.quote(team_config)

    slaves_fragment = _nmcli_add_slaves_fragment(team_name, slaves, "team")

    return (
        f"{_require_nmcli_fragment()}"
        f"nmcli connection add type team con-name {q_team} ifname {q_team} "
        f"team.config {q_config} && "
        f"{slaves_fragment} && "
        f"nmcli connection up {q_team} 2>&1"
    )


def cmd_configure_vlan(parent_iface: str, vlan_id, vlan_name: str = "") -> str:
    parent = _validate_iface(parent_iface, "Parent interface")
    vlan_id = _validate_int_range(vlan_id, 1, 4094, "VLAN ID")
    vlan_name = (vlan_name or "").strip() or f"{parent}.{vlan_id}"
    vlan_name = _validate_iface(vlan_name, "VLAN interface name")
    q_name = shlex.quote(vlan_name)
    q_parent = shlex.quote(parent)
    return (
        f"{_require_nmcli_fragment()}"
        f"nmcli connection add type vlan con-name {q_name} ifname {q_name} "
        f"dev {q_parent} id {vlan_id} && "
        f"nmcli connection up {q_name} 2>&1"
    )


def cmd_configure_bridge(bridge_name: str, slave_ifaces: str) -> str:
    bridge_name = _validate_iface(bridge_name, "Bridge name")
    slaves = _ifaces_list(slave_ifaces, "Slave interface(s)")
    q_bridge = shlex.quote(bridge_name)
    slaves_fragment = _nmcli_add_slaves_fragment(bridge_name, slaves, "bridge")

    return (
        f"{_require_nmcli_fragment()}"
        f"nmcli connection add type bridge con-name {q_bridge} ifname {q_bridge} && "
        f"{slaves_fragment} && "
        f"nmcli connection up {q_bridge} 2>&1"
    )
