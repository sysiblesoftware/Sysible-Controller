"""
The action registry: the single bridge between the React UI and the
desktop client's cmd_* shell-command builders.

Each Action ties together:
  * name    - stable id used in the URL (/api/tool/<name>) and by the SPA
  * tool    - which dashboard tool/tile it belongs to (groups the UI)
  * label   - human label for the button/form
  * kind    - dispatch kind passed through to run_on_entry (mostly
              "command"; some result-heavy reads use a distinct kind so
              the controller can cache/route them, matching the desktop)
  * params  - ordered list of Param (name/label/type/default/required)
              the SPA renders a form from, and the server validates
  * build   - callable(params: dict) -> str : returns the exact shell
              string by delegating to the matching cmd_* builder

To extend toward full desktop parity you ADD Action entries here that
point at cmd_* functions that already exist in client/_api_*.py. You do
not write any new dispatch or shell logic - that already exists and is
shared with the desktop app, so the two stay in lockstep.

This file intentionally seeds only a representative slice across three
tools (fleet run-command, service management, user & group). It proves
the pattern end-to-end; the remaining tiles are the same shape.
"""
from dataclasses import dataclass, field
from typing import Callable

from client import api  # cmd_* builders are re-exported on client.api
from client import _api_users  # for the one cmd_* name that collides on `api`


@dataclass
class Param:
    name: str
    label: str
    type: str = "text"          # text | password | number | select | checkbox
    default: object = ""
    required: bool = True
    options: list = field(default_factory=list)   # for type == "select"
    help: str = ""


@dataclass
class Action:
    name: str
    tool: str
    label: str
    build: Callable[[dict], str]
    kind: str = "command"
    params: list = field(default_factory=list)
    description: str = ""
    danger: bool = False        # UI confirms before running (delete, etc.)
    tab: str = ""               # desktop tab this action lives under
    group: str = ""             # titled section (QGroupBox) within the tab


# ----------------------------------------------------------------------
# Small helpers so build= callables stay one-liners and coerce types the
# HTML form hands us (everything arrives as strings/None).
# ----------------------------------------------------------------------
def _s(params, key, default=""):
    v = params.get(key, default)
    return default if v is None else str(v)


def _i(params, key, default=0):
    v = params.get(key, default)
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _b(params, key, default=False):
    v = params.get(key, default)
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("1", "true", "yes", "on")


# ----------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------
_ACTIONS: dict[str, Action] = {}


def _register(action: Action):
    _ACTIONS[action.name] = action


# ---- Tool: Run Command (the fleet primitive) -------------------------
_register(Action(
    name="run_command",
    tool="Run Command",
    label="Run shell command",
    description="Run an arbitrary shell command on every selected host.",
    params=[Param("command", "Command", help="e.g. uname -a")],
    build=lambda p: _s(p, "command"),
))

# ---- Tool: Service Management ----------------------------------------
_register(Action(
    name="svc_list",
    tool="Service Management",
    label="List services",
    description="List all systemd services and their state.",
    params=[],
    build=lambda p: api.cmd_list_services(),
))
_register(Action(
    name="svc_list_running",
    tool="Service Management",
    label="List running services",
    params=[],
    build=lambda p: api.cmd_list_running_services(),
))
_register(Action(
    name="svc_status",
    tool="Service Management",
    label="Service status",
    params=[Param("name", "Service name", help="e.g. sshd")],
    build=lambda p: api.cmd_service_status(_s(p, "name")),
))
_register(Action(
    name="svc_start",
    tool="Service Management",
    label="Start service",
    params=[Param("name", "Service name")],
    build=lambda p: api.cmd_service_start(_s(p, "name")),
))
_register(Action(
    name="svc_stop",
    tool="Service Management",
    label="Stop service",
    danger=True,
    params=[Param("name", "Service name")],
    build=lambda p: api.cmd_service_stop(_s(p, "name")),
))
_register(Action(
    name="svc_restart",
    tool="Service Management",
    label="Restart service",
    params=[Param("name", "Service name")],
    build=lambda p: api.cmd_service_restart(_s(p, "name")),
))

# ---- Tool: User & Group Administration -------------------------------
_register(Action(
    name="user_create",
    tool="User & Group Administration",
    label="Create user",
    params=[
        Param("username", "Username"),
        Param("password", "Password", type="password", required=False),
        Param("shell", "Shell", default="/bin/bash", required=False),
    ],
    build=lambda p: api.cmd_create_user(_s(p, "username"), _s(p, "password"),
                                        _s(p, "shell", "/bin/bash") or "/bin/bash"),
))
_register(Action(
    name="user_delete",
    tool="User & Group Administration",
    label="Delete user",
    danger=True,
    params=[Param("username", "Username")],
    build=lambda p: api.cmd_delete_user(_s(p, "username")),
))
_register(Action(
    name="user_lock",
    tool="User & Group Administration",
    label="Lock user",
    params=[Param("username", "Username")],
    build=lambda p: api.cmd_lock_user(_s(p, "username")),
))
_register(Action(
    name="user_unlock",
    tool="User & Group Administration",
    label="Unlock user",
    params=[Param("username", "Username")],
    build=lambda p: api.cmd_unlock_user(_s(p, "username")),
))
_register(Action(
    name="user_set_shell",
    tool="User & Group Administration",
    label="Set shell",
    params=[Param("username", "Username"), Param("shell", "Shell", default="/bin/bash")],
    build=lambda p: api.cmd_set_user_shell(_s(p, "username"), _s(p, "shell", "/bin/bash")),
))
_register(Action(
    name="group_members",
    tool="User & Group Administration",
    label="List groups & members",
    params=[],
    build=lambda p: api.cmd_list_groups_with_members(),
))


# ---- Tool: Host Software Management ----------------------------------
_register(Action(
    name="pkg_list_installed", tool="Host Software Management",
    label="List installed packages", params=[],
    build=lambda p: api.cmd_list_installed_packages()))
_register(Action(
    name="pkg_search", tool="Host Software Management", label="Search packages",
    params=[Param("term", "Search term")],
    build=lambda p: api.cmd_search_packages(_s(p, "term"))))
_register(Action(
    name="pkg_query", tool="Host Software Management", label="Package info",
    params=[Param("name", "Package name")],
    build=lambda p: api.cmd_query_package(_s(p, "name"))))
_register(Action(
    name="pkg_install", tool="Host Software Management", label="Install packages",
    params=[Param("names", "Package name(s)", help="space-separated")],
    build=lambda p: api.cmd_install_packages(_s(p, "names"))))
_register(Action(
    name="pkg_update", tool="Host Software Management",
    label="Update / upgrade packages",
    params=[Param("names", "Package name(s)", required=False,
                  help="leave blank to update everything")],
    build=lambda p: api.cmd_update_packages(_s(p, "names"))))
_register(Action(
    name="pkg_remove", tool="Host Software Management", label="Remove packages",
    danger=True, params=[Param("names", "Package name(s)")],
    build=lambda p: api.cmd_remove_packages(_s(p, "names"))))
_register(Action(
    name="pkg_clean_cache", tool="Host Software Management",
    label="Clean package cache", params=[],
    build=lambda p: api.cmd_clean_package_cache()))

# ---- Tool: Repository Management --------------------------------------
_register(Action(
    name="repo_list", tool="Repository Management", label="List repositories",
    params=[], build=lambda p: api.cmd_list_repositories()))
_register(Action(
    name="repo_add", tool="Repository Management", label="Add repository",
    params=[Param("url", "Repo URL / file"), Param("alias", "Alias", required=False)],
    build=lambda p: api.cmd_add_repository(_s(p, "url"), _s(p, "alias"))))
_register(Action(
    name="repo_enable", tool="Repository Management", label="Enable repository",
    params=[Param("alias", "Alias / id")],
    build=lambda p: api.cmd_enable_repository(_s(p, "alias"))))
_register(Action(
    name="repo_disable", tool="Repository Management", label="Disable repository",
    params=[Param("alias", "Alias / id")],
    build=lambda p: api.cmd_disable_repository(_s(p, "alias"))))
_register(Action(
    name="repo_remove", tool="Repository Management", label="Remove repository",
    danger=True, params=[Param("alias", "Alias / id")],
    build=lambda p: api.cmd_remove_repository(_s(p, "alias"))))

# ---- Tool: Cron & Systemd Timers -------------------------------------
_register(Action(
    name="cron_list", tool="Cron & Systemd Timers", label="List cron jobs",
    params=[], build=lambda p: api.cmd_list_cron_jobs()))
_register(Action(
    name="cron_add", tool="Cron & Systemd Timers", label="Add cron job",
    params=[Param("schedule", "Schedule", help="e.g. */10 * * * *"),
            Param("command", "Command"),
            Param("comment", "Comment", required=False)],
    build=lambda p: api.cmd_add_cron_job(_s(p, "schedule"), _s(p, "command"),
                                         _s(p, "comment"))))
_register(Action(
    name="cron_remove", tool="Cron & Systemd Timers", label="Remove cron job",
    danger=True, params=[Param("match_text", "Match text",
                               help="text identifying the job line")],
    build=lambda p: api.cmd_remove_cron_job(_s(p, "match_text"))))
_register(Action(
    name="timer_list", tool="Cron & Systemd Timers", label="List timers",
    params=[], build=lambda p: api.cmd_list_timers()))
_register(Action(
    name="timer_status", tool="Cron & Systemd Timers", label="Timer status",
    params=[Param("name", "Timer name")],
    build=lambda p: api.cmd_timer_status(_s(p, "name"))))
for _tn, _fn in [("timer_start", "cmd_timer_start"), ("timer_stop", "cmd_timer_stop"),
                 ("timer_enable", "cmd_timer_enable"), ("timer_disable", "cmd_timer_disable")]:
    _register(Action(
        name=_tn, tool="Cron & Systemd Timers",
        label=_tn.replace("timer_", "").capitalize() + " timer",
        params=[Param("name", "Timer name")],
        build=(lambda fn: (lambda p: getattr(api, fn)(_s(p, "name"))))(_fn)))

# ---- Tool: Network Management -----------------------------------------
_register(Action(name="net_ip", tool="Network Management", label="Show IP config",
    params=[Param("iface", "Interface", required=False)],
    build=lambda p: api.cmd_show_ip_config(_s(p, "iface"))))
_register(Action(name="net_devices", tool="Network Management", label="List devices",
    params=[], build=lambda p: api.cmd_list_devices()))
_register(Action(name="net_routes", tool="Network Management", label="Show routes",
    params=[], build=lambda p: api.cmd_show_routes()))
_register(Action(name="net_connections", tool="Network Management",
    label="List connections", params=[], build=lambda p: api.cmd_list_connections()))
_register(Action(name="net_listening", tool="Network Management",
    label="Listening services", params=[], build=lambda p: api.cmd_listening_services()))
_register(Action(name="net_hostname", tool="Network Management", label="Show hostname",
    params=[], build=lambda p: api.cmd_show_hostname()))
_register(Action(name="net_ping", tool="Network Management", label="Ping",
    params=[Param("target", "Target", help="host or IP"),
            Param("count", "Count", type="number", default="4", required=False)],
    build=lambda p: api.cmd_ping(_s(p, "target"), _i(p, "count", 4))))
_register(Action(name="net_traceroute", tool="Network Management", label="Traceroute",
    params=[Param("target", "Target")],
    build=lambda p: api.cmd_traceroute(_s(p, "target"))))
_register(Action(name="net_dns", tool="Network Management", label="DNS lookup",
    params=[Param("name", "Name"), Param("server", "DNS server", required=False)],
    build=lambda p: api.cmd_dns_lookup(_s(p, "name"), _s(p, "server"))))
_register(Action(name="net_set_mtu", tool="Network Management", label="Set MTU",
    params=[Param("connection", "Connection"), Param("mtu", "MTU", type="number", default="1500")],
    build=lambda p: api.cmd_set_mtu(_s(p, "connection"), _i(p, "mtu", 1500))))
_register(Action(name="net_add_route", tool="Network Management", label="Add static route",
    params=[Param("connection", "Connection"), Param("destination_cidr", "Destination CIDR"),
            Param("via_gateway", "Via gateway")],
    build=lambda p: api.cmd_add_static_route(_s(p, "connection"),
                                             _s(p, "destination_cidr"), _s(p, "via_gateway"))))

# ---- Tool: Storage Administration ------------------------------------
_register(Action(name="stor_list_disks", tool="Storage Administration",
    label="List disks", params=[], build=lambda p: api.cmd_list_disks()))
_register(Action(name="stor_rescan", tool="Storage Administration",
    label="Rescan disks", params=[], build=lambda p: api.cmd_rescan_disks()))
_register(Action(name="stor_list_parts", tool="Storage Administration",
    label="List partitions", params=[Param("device", "Device", required=False)],
    build=lambda p: api.cmd_list_partitions(_s(p, "device"))))
_register(Action(name="stor_smart", tool="Storage Administration",
    label="SMART status", params=[Param("device", "Device", help="e.g. /dev/sda")],
    build=lambda p: api.cmd_check_smart_status(_s(p, "device"))))
_register(Action(name="stor_install_smart", tool="Storage Administration",
    label="Install smartmontools", params=[], build=lambda p: api.cmd_install_smartmontools()))
_register(Action(name="stor_install_lvm", tool="Storage Administration",
    label="Install LVM tools", params=[], build=lambda p: api.cmd_install_lvm_tools()))
_register(Action(name="stor_install_mdadm", tool="Storage Administration",
    label="Install mdadm", params=[], build=lambda p: api.cmd_install_mdadm()))
_register(Action(name="stor_format", tool="Storage Administration",
    label="Format filesystem", danger=True,
    params=[Param("device", "Device / partition"),
            Param("fs_type", "Filesystem", type="select",
                  options=["ext4", "xfs", "btrfs", "vfat", "ext3"], default="ext4"),
            Param("label", "Label", required=False)],
    build=lambda p: api.cmd_format_filesystem(_s(p, "device"), _s(p, "fs_type", "ext4"),
                                              _s(p, "label"))))
_register(Action(name="stor_pv_list", tool="Storage Administration",
    label="List physical volumes", params=[], build=lambda p: api.cmd_list_physical_volumes()))
_register(Action(name="stor_vg_list", tool="Storage Administration",
    label="List volume groups", params=[], build=lambda p: api.cmd_list_volume_groups()))
_register(Action(name="stor_lv_list", tool="Storage Administration",
    label="List logical volumes", params=[], build=lambda p: api.cmd_list_logical_volumes()))
_register(Action(name="stor_lv_create", tool="Storage Administration",
    label="Create logical volume",
    params=[Param("vg_name", "Volume group"), Param("lv_name", "LV name"),
            Param("size", "Size", help="e.g. 5G")],
    build=lambda p: api.cmd_create_logical_volume(_s(p, "vg_name"), _s(p, "lv_name"), _s(p, "size"))))
_register(Action(name="stor_raid_list", tool="Storage Administration",
    label="List RAID arrays", params=[], build=lambda p: api.cmd_list_raid_arrays()))
_register(Action(name="stor_swap_list", tool="Storage Administration",
    label="List swap", params=[], build=lambda p: api.cmd_list_swap()))

# ---- Tool: Firewall Administration -----------------------------------
_register(Action(name="fw_status", tool="Firewall Administration",
    label="firewalld status", params=[], build=lambda p: api.cmd_firewalld_status()))
_register(Action(name="fw_list_all_ports", tool="Firewall Administration",
    label="List ALL listening ports", params=[], build=lambda p: api.cmd_list_listening_ports()))
_register(Action(name="fw_list_ports", tool="Firewall Administration",
    label="List open ports (zone)", params=[Param("zone", "Zone", required=False)],
    build=lambda p: api.cmd_list_ports(_s(p, "zone"))))
_register(Action(name="fw_list_zones", tool="Firewall Administration",
    label="List zones", params=[], build=lambda p: api.cmd_list_zones()))
_register(Action(name="fw_open_port", tool="Firewall Administration", label="Open port",
    params=[Param("port", "Port", help="e.g. 8080"),
            Param("protocol", "Protocol", type="select", options=["tcp", "udp"], default="tcp"),
            Param("zone", "Zone", required=False)],
    build=lambda p: api.cmd_open_port(_s(p, "port"), _s(p, "protocol", "tcp"), _s(p, "zone"))))
_register(Action(name="fw_close_port", tool="Firewall Administration", label="Close port",
    danger=True,
    params=[Param("port", "Port"),
            Param("protocol", "Protocol", type="select", options=["tcp", "udp"], default="tcp"),
            Param("zone", "Zone", required=False)],
    build=lambda p: api.cmd_close_port(_s(p, "port"), _s(p, "protocol", "tcp"), _s(p, "zone"))))
_register(Action(name="fw_reload", tool="Firewall Administration", label="Reload firewalld",
    params=[], build=lambda p: api.cmd_reload_firewalld()))
_register(Action(name="fw_install_firewalld", tool="Firewall Administration",
    label="Install firewalld", params=[], build=lambda p: api.cmd_install_firewalld()))
_register(Action(name="fw_install_ufw", tool="Firewall Administration",
    label="Install ufw", params=[], build=lambda p: api.cmd_install_ufw()))
_register(Action(name="fw_nft_ruleset", tool="Firewall Administration",
    label="nftables ruleset", params=[], build=lambda p: api.cmd_nft_list_ruleset()))
_register(Action(name="fw_iptables_list", tool="Firewall Administration",
    label="iptables list", params=[Param("table", "Table", default="filter", required=False)],
    build=lambda p: api.cmd_iptables_list(_s(p, "table", "filter") or "filter")))

# ---- Tool: Security Administration -----------------------------------
_register(Action(name="sec_selinux_status", tool="Security Administration",
    label="SELinux status", params=[], build=lambda p: api.cmd_selinux_status()))
_register(Action(name="sec_install_selinux", tool="Security Administration",
    label="Install SELinux tools", params=[], build=lambda p: api.cmd_install_selinux_tools()))
_register(Action(name="sec_set_selinux_mode", tool="Security Administration",
    label="Set SELinux mode",
    params=[Param("mode", "Mode", type="select", options=["enforcing", "permissive"], default="enforcing")],
    build=lambda p: api.cmd_set_selinux_mode(_s(p, "mode", "enforcing"))))
_register(Action(name="sec_sshd_status", tool="Security Administration",
    label="SSH daemon status", params=[], build=lambda p: api.cmd_sshd_status()))
_register(Action(name="sec_sshd_set", tool="Security Administration", label="Set sshd option",
    params=[Param("key", "Option"), Param("value", "Value")],
    build=lambda p: api.cmd_sshd_set_option(_s(p, "key"), _s(p, "value"))))
_register(Action(name="sec_failed_logins", tool="Security Administration",
    label="List failed logins",
    params=[Param("lines", "Lines", type="number", default="50", required=False)],
    build=lambda p: api.cmd_list_failed_logins(_i(p, "lines", 50))))
_register(Action(name="sec_locked_accounts", tool="Security Administration",
    label="List locked accounts", params=[], build=lambda p: api.cmd_list_locked_accounts()))
_register(Action(name="sec_check_updates", tool="Security Administration",
    label="Check security updates", params=[], build=lambda p: api.cmd_check_security_updates()))
_register(Action(name="sec_install_updates", tool="Security Administration",
    label="Install security updates", params=[], build=lambda p: api.cmd_install_security_updates()))
_register(Action(name="sec_hardening", tool="Security Administration",
    label="Hardening overview", params=[], build=lambda p: api.cmd_get_hardening_overview()))
_register(Action(name="sec_install_rkhunter", tool="Security Administration",
    label="Install rkhunter", params=[], build=lambda p: api.cmd_install_rkhunter()))
_register(Action(name="sec_install_lynis", tool="Security Administration",
    label="Install Lynis", params=[], build=lambda p: api.cmd_install_lynis()))

# ---- Tool: File System Management ------------------------------------
_register(Action(name="fs_list_dir", tool="File System Management", label="List directory",
    params=[Param("path", "Path", default="/", help="e.g. /opt")],
    build=lambda p: api.cmd_list_directory(_s(p, "path", "/") or "/")))
_register(Action(name="fs_view", tool="File System Management", label="View file",
    params=[Param("path", "Path")], build=lambda p: api.cmd_view_file(_s(p, "path"))))
# Cross-host file comparison: check several hosts, enter a path, and see which
# hosts have a different version (grouped by content hash). The web console
# handles this specially (POST /api/files/compare aggregates the per-host
# fingerprints); the build below is the per-host read-only fingerprint command.
_register(Action(name="fs_compare", tool="File System Management",
    group="Compare a file across hosts", label="Compare across selected hosts",
    params=[Param("path", "Path", help="e.g. /etc/ssh/sshd_config")],
    build=lambda p: api.cmd_file_fingerprint(_s(p, "path"))))
_register(Action(name="fs_mkdir", tool="File System Management", label="Create directory",
    params=[Param("path", "Path")], build=lambda p: api.cmd_create_directory(_s(p, "path"))))
_register(Action(name="fs_rmdir", tool="File System Management", label="Remove directory",
    danger=True, params=[Param("path", "Path"),
                         Param("recursive", "Recursive", type="checkbox", default=False, required=False)],
    build=lambda p: api.cmd_remove_directory(_s(p, "path"), _b(p, "recursive"),
                                             allow_critical=_b(p, "allow_critical", False))))
_register(Action(name="fs_copy", tool="File System Management", label="Copy",
    params=[Param("source", "Source"), Param("destination", "Destination")],
    build=lambda p: api.cmd_copy_file(_s(p, "source"), _s(p, "destination"))))
_register(Action(name="fs_move", tool="File System Management", label="Move",
    params=[Param("source", "Source"), Param("destination", "Destination")],
    build=lambda p: api.cmd_move_file(_s(p, "source"), _s(p, "destination"))))
_register(Action(name="fs_chmod", tool="File System Management", label="Change permissions",
    params=[Param("path", "Path"), Param("mode", "Mode", help="e.g. 0644"),
            Param("recursive", "Recursive", type="checkbox", default=False, required=False)],
    build=lambda p: api.cmd_change_permissions(_s(p, "path"), _s(p, "mode"), _b(p, "recursive"))))
_register(Action(name="fs_chown", tool="File System Management", label="Change ownership",
    params=[Param("path", "Path"), Param("owner", "Owner", required=False),
            Param("group", "Group", required=False),
            Param("recursive", "Recursive", type="checkbox", default=False, required=False)],
    build=lambda p: api.cmd_change_ownership(_s(p, "path"), _s(p, "owner"),
                                             _s(p, "group"), _b(p, "recursive"))))
_register(Action(name="fs_archive", tool="File System Management", label="Create archive",
    params=[Param("source_path", "Source"), Param("archive_path", "Archive path"),
            Param("compression", "Compression", type="select",
                  options=["gzip", "bzip2", "xz", "none"], default="gzip")],
    build=lambda p: api.cmd_create_archive(_s(p, "source_path"), _s(p, "archive_path"),
                                           _s(p, "compression", "gzip"))))
_register(Action(name="fs_show_fstab", tool="File System Management", label="Show fstab",
    params=[], build=lambda p: api.cmd_show_fstab()))
_register(Action(name="fs_mount_nfs", tool="File System Management", label="Mount NFS",
    params=[Param("server", "Server"), Param("export_path", "Export path"),
            Param("mount_point", "Mount point")],
    build=lambda p: api.cmd_mount_nfs(_s(p, "server"), _s(p, "export_path"), _s(p, "mount_point"))))
_register(Action(name="fs_mount_cifs", tool="File System Management", label="Mount CIFS/SMB",
    params=[Param("server", "Server"), Param("share", "Share"),
            Param("mount_point", "Mount point"),
            Param("username", "Username", required=False),
            Param("password", "Password", type="password", required=False)],
    build=lambda p: api.cmd_mount_cifs(_s(p, "server"), _s(p, "share"), _s(p, "mount_point"),
                                       _s(p, "username"), _s(p, "password"))))

# ---- Tool: System Health, Logs & Recovery ----------------------------
_register(Action(name="health_check", tool="System Health, Logs & Recovery",
    label="Health check", params=[], build=lambda p: api.cmd_health_check()))
_register(Action(name="health_disk_usage", tool="System Health, Logs & Recovery",
    label="Disk usage", params=[], build=lambda p: api.cmd_disk_usage()))
_register(Action(name="health_mem_cpu", tool="System Health, Logs & Recovery",
    label="Memory / CPU snapshot", params=[], build=lambda p: api.cmd_memory_cpu_snapshot()))
_register(Action(name="health_uptime", tool="System Health, Logs & Recovery",
    label="Uptime", params=[], build=lambda p: api.cmd_uptime()))
_register(Action(name="health_failed_services", tool="System Health, Logs & Recovery",
    label="Failed services", params=[], build=lambda p: api.cmd_failed_services()))
_register(Action(name="health_processes", tool="System Health, Logs & Recovery",
    label="Top processes",
    params=[Param("sort_by", "Sort by", type="select", options=["cpu", "mem"], default="cpu"),
            Param("top_n", "Count", type="number", default="30", required=False)],
    build=lambda p: api.cmd_list_processes(_s(p, "sort_by", "cpu"), _i(p, "top_n", 30))))
_register(Action(name="health_review_logs", tool="System Health, Logs & Recovery",
    label="Review system logs",
    params=[Param("lines", "Lines", type="number", default="200", required=False)],
    build=lambda p: api.cmd_review_system_logs(_i(p, "lines", 200))))
_register(Action(name="health_search_log", tool="System Health, Logs & Recovery",
    label="Search logs",
    params=[Param("pattern", "Pattern"),
            Param("lines", "Lines", type="number", default="200", required=False)],
    build=lambda p: api.cmd_search_log(_s(p, "pattern"), _i(p, "lines", 200))))
_register(Action(name="health_kernel_msgs", tool="System Health, Logs & Recovery",
    label="Kernel messages",
    params=[Param("lines", "Lines", type="number", default="200", required=False)],
    build=lambda p: api.cmd_monitor_kernel_messages(_i(p, "lines", 200))))
_register(Action(name="health_boot_failures", tool="System Health, Logs & Recovery",
    label="Investigate boot failures", params=[],
    build=lambda p: api.cmd_investigate_boot_failures()))
_register(Action(name="health_list_kernels", tool="System Health, Logs & Recovery",
    label="List kernels", params=[], build=lambda p: api.cmd_list_kernels()))
_register(Action(name="health_grub", tool="System Health, Logs & Recovery",
    label="Show GRUB config", params=[], build=lambda p: api.cmd_show_grub_config()))
_register(Action(name="health_support_info", tool="System Health, Logs & Recovery",
    label="Collect support info", params=[], build=lambda p: api.cmd_collect_support_info()))

# ---- Tool: Time Synchronization --------------------------------------
_register(Action(name="time_status", tool="Time Synchronization", label="Time sync status",
    params=[], build=lambda p: api.cmd_timesync_status()))
_register(Action(name="time_verify", tool="Time Synchronization", label="Verify sync",
    params=[], build=lambda p: api.cmd_verify_sync()))
_register(Action(name="time_set_ntp", tool="Time Synchronization", label="Set NTP servers",
    params=[Param("servers", "Servers", help="space-separated")],
    build=lambda p: api.cmd_set_ntp_servers(_s(p, "servers"))))
_register(Action(name="time_set_tz", tool="Time Synchronization", label="Set timezone",
    params=[Param("tz", "Timezone", help="e.g. America/New_York")],
    build=lambda p: api.cmd_set_timezone(_s(p, "tz"))))
_register(Action(name="time_list_tz", tool="Time Synchronization", label="List timezones",
    params=[Param("filter_text", "Filter", required=False)],
    build=lambda p: api.cmd_list_timezones(_s(p, "filter_text"))))

# ---- Tool: Certificate Management ------------------------------------
_register(Action(name="cert_generate_csr", tool="Certificate Management", label="Generate CSR",
    params=[Param("common_name", "Common name"), Param("org", "Organization", required=False)],
    build=lambda p: api.cmd_generate_csr(_s(p, "common_name"), _s(p, "org"))))
_register(Action(name="cert_check", tool="Certificate Management", label="Inspect certificate",
    params=[Param("cert_path", "Certificate path")],
    build=lambda p: api.cmd_check_certificate(_s(p, "cert_path"))))
_register(Action(name="cert_troubleshoot_tls", tool="Certificate Management",
    label="Troubleshoot TLS",
    params=[Param("host", "Host"), Param("port", "Port", default="443", required=False)],
    build=lambda p: api.cmd_troubleshoot_tls(_s(p, "host"), _s(p, "port", "443") or "443")))

# ---- Tool: Containers & VMs ------------------------------------------
_register(Action(name="cont_runtime", tool="Containers & VMs", label="Container runtime",
    params=[], build=lambda p: api.cmd_container_runtime()))
_register(Action(name="cont_list", tool="Containers & VMs", label="List containers",
    params=[], build=lambda p: api.cmd_list_containers()))
_register(Action(name="cont_images", tool="Containers & VMs", label="List images",
    params=[], build=lambda p: api.cmd_list_images()))
_register(Action(name="cont_action", tool="Containers & VMs", label="Container action",
    params=[Param("action", "Action", type="select",
                  options=["start", "stop", "restart", "rm", "pause", "unpause"], default="start"),
            Param("name", "Container")],
    build=lambda p: api.cmd_container_action(_s(p, "action", "start"), _s(p, "name"))))
_register(Action(name="cont_logs", tool="Containers & VMs", label="Container logs",
    params=[Param("name", "Container"),
            Param("lines", "Lines", type="number", default="200", required=False)],
    build=lambda p: api.cmd_container_logs(_s(p, "name"), _i(p, "lines", 200))))
_register(Action(name="cont_prune", tool="Containers & VMs", label="Prune", danger=True,
    params=[], build=lambda p: api.cmd_container_prune()))
_register(Action(name="vm_list", tool="Containers & VMs", label="List VMs",
    params=[], build=lambda p: api.cmd_list_vms()))

# ---- Tool: Directory Services (Active Directory / LDAP) --------------
_register(Action(name="dir_install_ad", tool="Directory Services (Active Directory / LDAP)",
    label="Install AD dependencies", params=[], build=lambda p: api.cmd_install_ad_dependencies()))
_register(Action(name="dir_prepare_ad", tool="Directory Services (Active Directory / LDAP)",
    label="Prepare host for AD join",
    description="One-click prep: install the AD client packages and configure the host, ready to join.",
    params=[Param("domain", "Domain", required=False, help="e.g. corp.example.com")],
    build=lambda p: api.cmd_prepare_ad_join(_s(p, "domain"))))
_register(Action(name="dir_realm_status", tool="Directory Services (Active Directory / LDAP)",
    label="Realm status", params=[], build=lambda p: api.cmd_realm_status()))
_register(Action(name="dir_krb_status", tool="Directory Services (Active Directory / LDAP)",
    label="Kerberos status", params=[], build=lambda p: api.cmd_kerberos_status()))
_register(Action(name="dir_join_ad", tool="Directory Services (Active Directory / LDAP)",
    label="Join AD domain",
    params=[Param("domain", "Domain"), Param("admin_user", "Admin user"),
            Param("password", "Password", type="password"),
            Param("computer_ou", "Computer OU", required=False)],
    build=lambda p: api.cmd_join_ad(_s(p, "domain"), _s(p, "admin_user"),
                                    _s(p, "password"), _s(p, "computer_ou"))))
_register(Action(name="dir_leave_ad", tool="Directory Services (Active Directory / LDAP)",
    label="Leave AD domain", danger=True, params=[Param("domain", "Domain")],
    build=lambda p: api.cmd_leave_ad(_s(p, "domain"))))

# ---- Tool: Backup & Recovery -----------------------------------------
_register(Action(name="backup_files", tool="Backup & Recovery", label="Back up files",
    params=[Param("source", "Source path"), Param("dest_dir", "Destination dir")],
    build=lambda p: api.cmd_backup_files(_s(p, "source"), _s(p, "dest_dir"))))
_register(Action(name="backup_restore", tool="Backup & Recovery", label="Restore files",
    danger=True, params=[Param("archive", "Archive"), Param("dest_dir", "Destination dir")],
    build=lambda p: api.cmd_restore_files(_s(p, "archive"), _s(p, "dest_dir"))))
_register(Action(name="backup_verify", tool="Backup & Recovery", label="Verify backup",
    params=[Param("archive", "Archive")],
    build=lambda p: api.cmd_verify_backup(_s(p, "archive"))))

# ---- Tool: Distro Subscription & Licensing ---------------------------
_register(Action(name="sub_detect", tool="Distro Subscription & Licensing",
    label="Detect subscription system", params=[], build=lambda p: api.cmd_subscription_detect()))
_register(Action(name="sub_register_all", tool="Distro Subscription & Licensing",
    label="Register ALL selected hosts (auto-detect vendor)",
    description="Registers every selected host with its own subscription system "
                "(RHSM / Ubuntu Pro / SUSE) using whichever credentials you provide. "
                "Ideal for registering a fleet of RHEL servers in one click; safe "
                "across mixed distros (hosts you didn't supply creds for are skipped).",
    params=[Param("org", "RHSM organization", required=False),
            Param("activationkey", "RHSM activation key", required=False),
            Param("username", "RHSM username", required=False),
            Param("password", "RHSM password", type="password", required=False),
            Param("auto_attach", "RHSM auto-attach", type="checkbox", default=True, required=False),
            Param("pro_token", "Ubuntu Pro token", type="password", required=False),
            Param("suse_regcode", "SUSE registration code", type="password", required=False),
            Param("suse_email", "SUSE email", required=False)],
    build=lambda p: api.cmd_subscription_register_all(
        _s(p, "org"), _s(p, "activationkey"), _s(p, "username"), _s(p, "password"),
        _b(p, "auto_attach", True), _s(p, "pro_token"), _s(p, "suse_regcode"), _s(p, "suse_email"))))
_register(Action(name="sub_rhsm_status", tool="Distro Subscription & Licensing",
    label="Red Hat (RHSM) status", params=[], build=lambda p: api.cmd_rhsm_status()))
_register(Action(name="sub_rhsm_register", tool="Distro Subscription & Licensing",
    label="RHSM register (org + key)",
    params=[Param("org", "Organization"), Param("activationkey", "Activation key")],
    build=lambda p: api.cmd_rhsm_register(_s(p, "org"), _s(p, "activationkey"))))
_register(Action(name="sub_pro_status", tool="Distro Subscription & Licensing",
    label="Ubuntu Pro status", params=[], build=lambda p: api.cmd_pro_status()))
_register(Action(name="sub_pro_attach", tool="Distro Subscription & Licensing",
    label="Ubuntu Pro attach", params=[Param("token", "Token", type="password")],
    build=lambda p: api.cmd_pro_attach(_s(p, "token"))))
_register(Action(name="sub_suse_status", tool="Distro Subscription & Licensing",
    label="SUSE (SCC) status", params=[], build=lambda p: api.cmd_suse_status()))
_register(Action(name="sub_suse_register", tool="Distro Subscription & Licensing",
    label="SUSE register",
    params=[Param("regcode", "Reg code", type="password"), Param("email", "Email", required=False)],
    build=lambda p: api.cmd_suse_register(_s(p, "regcode"), _s(p, "email"))))


def _io(params, key):
    """Optional int: blank/None -> None (so the builder keeps its default),
    otherwise the int. Used by the policy setters whose args default to
    None meaning 'leave this field unchanged'."""
    v = params.get(key, "")
    if v is None or str(v).strip() == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ======================================================================
# FULL-PARITY ACTIONS - the remaining cmd_* builders, one Action each.
# ======================================================================

# ---- User & Group Administration (advanced) --------------------------
_register(Action(name="user_set_password", tool="User & Group Administration",
    label="Set password", params=[Param("username", "Username"),
                                  Param("password", "Password", type="password")],
    build=lambda p: api.cmd_set_password(_s(p, "username"), _s(p, "password"))))
_register(Action(name="user_set_sudo", tool="User & Group Administration",
    label="Set sudo access", params=[Param("username", "Username"),
        Param("enable", "Grant sudo", type="checkbox", default=True, required=False)],
    build=lambda p: api.cmd_set_sudo(_s(p, "username"), _b(p, "enable", True))))
_register(Action(name="user_set_comment", tool="User & Group Administration",
    label="Set full name (GECOS)", params=[Param("username", "Username"),
                                           Param("comment", "Full name / comment")],
    build=lambda p: api.cmd_set_user_comment(_s(p, "username"), _s(p, "comment"))))
_register(Action(name="user_force_reset", tool="User & Group Administration",
    label="Force password reset", params=[Param("username", "Username")],
    build=lambda p: api.cmd_force_password_reset(_s(p, "username"))))
_register(Action(name="user_kill_sessions", tool="User & Group Administration",
    label="Kill user sessions", danger=True, params=[Param("username", "Username")],
    build=lambda p: api.cmd_kill_user_sessions(_s(p, "username"))))
_register(Action(name="user_set_expiration", tool="User & Group Administration",
    label="Set account expiration", params=[Param("username", "Username"),
        Param("expire_date", "Expire date", required=False, help="YYYY-MM-DD, blank to clear")],
    build=lambda p: api.cmd_set_account_expiration(_s(p, "username"), _s(p, "expire_date"))))
_register(Action(name="user_set_aging", tool="User & Group Administration",
    label="Set password aging", params=[Param("username", "Username"),
        Param("max_days", "Max days", type="number", required=False),
        Param("min_days", "Min days", type="number", required=False),
        Param("warn_days", "Warn days", type="number", required=False)],
    build=lambda p: _api_users.cmd_set_password_aging(_s(p, "username"), _io(p, "max_days"),
                                                      _io(p, "min_days"), _io(p, "warn_days"))))
_register(Action(name="group_create", tool="User & Group Administration",
    label="Create group", params=[Param("name", "Group name")],
    build=lambda p: api.cmd_create_group(_s(p, "name"))))
_register(Action(name="group_delete", tool="User & Group Administration",
    label="Delete group", danger=True, params=[Param("name", "Group name")],
    build=lambda p: api.cmd_delete_group(_s(p, "name"))))
_register(Action(name="group_add_user", tool="User & Group Administration",
    label="Add user to group", params=[Param("group", "Group"), Param("username", "Username")],
    build=lambda p: api.cmd_add_user_to_group(_s(p, "group"), _s(p, "username"))))
_register(Action(name="group_remove_user", tool="User & Group Administration",
    label="Remove user from group", params=[Param("group", "Group"), Param("username", "Username")],
    build=lambda p: api.cmd_remove_user_from_group(_s(p, "group"), _s(p, "username"))))
_register(Action(name="user_audit_privileged", tool="User & Group Administration",
    label="Audit privileged users", params=[], build=lambda p: api.cmd_audit_privileged_users()))
_register(Action(name="user_list_usernames", tool="User & Group Administration",
    label="List usernames", params=[], build=lambda p: api.cmd_list_usernames()))
_register(Action(name="policy_pwquality", tool="User & Group Administration",
    label="Set password quality policy", params=[
        Param("minlen", "Min length", type="number", required=False),
        Param("retry", "Retries", type="number", required=False),
        Param("dcredit", "Digit credit", type="number", required=False),
        Param("ucredit", "Upper credit", type="number", required=False),
        Param("lcredit", "Lower credit", type="number", required=False),
        Param("ocredit", "Other credit", type="number", required=False)],
    build=lambda p: api.cmd_set_password_quality_policy(_io(p, "minlen"), _io(p, "retry"),
        _io(p, "dcredit"), _io(p, "ucredit"), _io(p, "lcredit"), _io(p, "ocredit"))))
_register(Action(name="policy_lockout", tool="User & Group Administration",
    label="Set account lockout policy", params=[
        Param("deny", "Failed attempts", type="number", required=False),
        Param("unlock_time", "Unlock seconds", type="number", required=False)],
    build=lambda p: api.cmd_set_account_lockout_policy(_io(p, "deny"), _io(p, "unlock_time"))))
_register(Action(name="policy_umask", tool="User & Group Administration",
    label="Set umask policy", params=[Param("value", "umask", default="027")],
    build=lambda p: api.cmd_set_umask_policy(_s(p, "value", "027"))))
_register(Action(name="policy_sudo", tool="User & Group Administration",
    label="Set sudo policy", params=[
        Param("timestamp_timeout", "Timestamp timeout (min)", type="number", required=False),
        Param("require_password", "Password requirement", type="select",
              options=["unchanged", "require", "nopasswd"], default="unchanged"),
        Param("group", "Sudo group", default="sudo", required=False)],
    build=lambda p: api.cmd_set_sudo_policy(_io(p, "timestamp_timeout"),
        (None if _s(p, "require_password", "unchanged") == "unchanged"
         else _s(p, "require_password") == "require"),
        _s(p, "group", "sudo") or "sudo")))

# ---- Service Management (advanced + process control) -----------------
for _tn, _fn, _lbl in [
        ("svc_reload", "cmd_service_reload", "Reload service"),
        ("svc_enable", "cmd_service_enable", "Enable service"),
        ("svc_disable", "cmd_service_disable", "Disable service"),
        ("svc_troubleshoot", "cmd_troubleshoot_service", "Troubleshoot service"),
        ("svc_dependencies", "cmd_service_dependencies", "Service dependencies")]:
    _register(Action(name=_tn, tool="Service Management", label=_lbl,
        params=[Param("name", "Service name")],
        build=(lambda fn: (lambda p: getattr(api, fn)(_s(p, "name"))))(_fn)))
_register(Action(name="svc_logs", tool="Service Management", label="Service logs",
    params=[Param("name", "Service name"),
            Param("lines", "Lines", type="number", default="200", required=False)],
    build=lambda p: api.cmd_service_logs(_s(p, "name"), _i(p, "lines", 200))))
_register(Action(name="svc_set_deps", tool="Service Management", label="Set service dependencies",
    params=[Param("name", "Service"), Param("after", "After", required=False),
            Param("requires", "Requires", required=False), Param("wants", "Wants", required=False)],
    build=lambda p: api.cmd_set_service_dependencies(_s(p, "name"), _s(p, "after"),
                                                     _s(p, "requires"), _s(p, "wants"))))
_register(Action(name="svc_create", tool="Service Management", label="Create systemd service",
    params=[Param("name", "Unit name"), Param("description", "Description", required=False),
            Param("exec_start", "ExecStart"),
            Param("working_directory", "Working dir", required=False),
            Param("run_as_user", "Run as user", default="root", required=False),
            Param("restart_policy", "Restart", type="select",
                  options=["on-failure", "always", "no", "on-abnormal"], default="on-failure"),
            Param("after", "After", default="network.target", required=False),
            Param("enable_now", "Enable now", type="checkbox", default=True, required=False)],
    build=lambda p: api.cmd_create_systemd_service(_s(p, "name"), _s(p, "description"),
        _s(p, "exec_start"), _s(p, "working_directory"), _s(p, "run_as_user", "root") or "root",
        _s(p, "restart_policy", "on-failure"), _s(p, "after", "network.target") or "network.target",
        _b(p, "enable_now", True))))
_register(Action(name="proc_high_load", tool="Service Management", label="Investigate high load",
    params=[], build=lambda p: api.cmd_investigate_high_load()))
_register(Action(name="proc_zombies", tool="Service Management", label="Zombie processes",
    params=[], build=lambda p: api.cmd_zombie_processes()))
_register(Action(name="proc_kill", tool="Service Management", label="Kill process", danger=True,
    params=[Param("pid", "PID", type="number"),
            Param("signal", "Signal", type="select", options=["TERM", "KILL", "HUP", "INT"], default="TERM")],
    build=lambda p: api.cmd_kill_process(_i(p, "pid"), _s(p, "signal", "TERM"))))
_register(Action(name="proc_renice", tool="Service Management", label="Renice process",
    params=[Param("pid", "PID", type="number"), Param("niceness", "Niceness", type="number", default="0")],
    build=lambda p: api.cmd_renice_process(_i(p, "pid"), _i(p, "niceness", 0))))
_register(Action(name="proc_restart", tool="Service Management", label="Restart process (by PID)",
    params=[Param("pid", "PID", type="number")],
    build=lambda p: api.cmd_restart_process(_i(p, "pid"))))

# ---- Host Software Management (advanced) ------------------------------
_register(Action(name="pkg_verify", tool="Host Software Management", label="Verify package",
    params=[Param("name", "Package name")], build=lambda p: api.cmd_verify_package(_s(p, "name"))))
_register(Action(name="pkg_install_local", tool="Host Software Management",
    label="Install local package (path on host)",
    params=[Param("remote_path", "Path on host", help="e.g. /tmp/foo.rpm")],
    build=lambda p: api.cmd_install_local_package(_s(p, "remote_path"))))
_register(Action(name="pkg_detect_env", tool="Host Software Management",
    label="Detect host package environment", params=[],
    build=lambda p: api.cmd_detect_host_environment()))

# ---- Repository Management (advanced) ---------------------------------
_register(Action(name="repo_create", tool="Repository Management", label="Create repository (full)",
    params=[Param("alias", "Alias / id"), Param("baseurl", "Base URL"),
            Param("name", "Display name", required=False),
            Param("gpgcheck", "GPG check", type="checkbox", default=True, required=False),
            Param("gpgkey", "GPG key URL", required=False),
            Param("distribution", "Distribution (deb)", required=False),
            Param("components", "Components (deb)", required=False)],
    build=lambda p: api.cmd_create_repository(_s(p, "alias"), _s(p, "baseurl"), _s(p, "name"),
        _b(p, "gpgcheck", True), _s(p, "gpgkey"), _s(p, "distribution"), _s(p, "components"))))

# ---- Cron & Systemd Timers (advanced) --------------------------------
_register(Action(name="timer_create", tool="Cron & Systemd Timers", label="Create systemd timer",
    params=[Param("name", "Timer name"), Param("exec_start", "ExecStart command"),
            Param("on_calendar", "OnCalendar", required=False, help="e.g. *-*-* 02:00:00"),
            Param("on_boot_sec", "OnBootSec", required=False),
            Param("on_unit_active_sec", "OnUnitActiveSec", required=False),
            Param("description", "Description", required=False),
            Param("run_as_user", "Run as user", default="root", required=False),
            Param("enable_now", "Enable now", type="checkbox", default=True, required=False)],
    build=lambda p: api.cmd_create_systemd_timer(_s(p, "name"), _s(p, "exec_start"),
        _s(p, "on_calendar"), _s(p, "on_boot_sec"), _s(p, "on_unit_active_sec"),
        _s(p, "description"), _s(p, "run_as_user", "root") or "root", _b(p, "enable_now", True))))
_register(Action(name="timer_delete", tool="Cron & Systemd Timers", label="Delete timer",
    danger=True, params=[Param("name", "Timer name"),
        Param("delete_service", "Also delete service unit", type="checkbox", default=True, required=False)],
    build=lambda p: api.cmd_delete_timer(_s(p, "name"), _b(p, "delete_service", True))))

# ---- Network Management (advanced) -----------------------------------
_register(Action(name="net_set_hostname", tool="Network Management", label="Set hostname",
    params=[Param("new_hostname", "Hostname")],
    build=lambda p: api.cmd_set_hostname(_s(p, "new_hostname"))))
_register(Action(name="net_set_gateway", tool="Network Management", label="Set gateway",
    params=[Param("connection", "Connection"), Param("gateway", "Gateway")],
    build=lambda p: api.cmd_set_gateway(_s(p, "connection"), _s(p, "gateway"))))
_register(Action(name="net_set_dns", tool="Network Management", label="Set DNS servers",
    params=[Param("connection", "Connection"), Param("dns_servers", "DNS servers", help="space-separated")],
    build=lambda p: api.cmd_set_dns(_s(p, "connection"), _s(p, "dns_servers"))))
_register(Action(name="net_static_ip", tool="Network Management", label="Configure static IP",
    params=[Param("connection", "Connection"), Param("ip_cidr", "IP/CIDR", help="e.g. 10.0.0.5/24"),
            Param("gateway", "Gateway", required=False), Param("dns", "DNS", required=False)],
    build=lambda p: api.cmd_configure_static_ip(_s(p, "connection"), _s(p, "ip_cidr"),
                                                _s(p, "gateway"), _s(p, "dns"))))
_register(Action(name="net_dhcp", tool="Network Management", label="Configure DHCP",
    params=[Param("connection", "Connection")],
    build=lambda p: api.cmd_configure_dhcp(_s(p, "connection"))))
_register(Action(name="net_bond", tool="Network Management", label="Configure bonding",
    params=[Param("bond_name", "Bond name"),
            Param("mode", "Mode", type="select",
                  options=["active-backup", "balance-rr", "802.3ad", "balance-xor"], default="active-backup"),
            Param("slave_ifaces", "Slave interfaces", help="space-separated")],
    build=lambda p: api.cmd_configure_bonding(_s(p, "bond_name"), _s(p, "mode", "active-backup"),
                                              _s(p, "slave_ifaces"))))
_register(Action(name="net_team", tool="Network Management", label="Configure teaming",
    params=[Param("team_name", "Team name"),
            Param("runner", "Runner", type="select",
                  options=["activebackup", "roundrobin", "lacp", "loadbalance"], default="activebackup"),
            Param("slave_ifaces", "Slave interfaces", help="space-separated")],
    build=lambda p: api.cmd_configure_teaming(_s(p, "team_name"), _s(p, "runner", "activebackup"),
                                              _s(p, "slave_ifaces"))))
_register(Action(name="net_vlan", tool="Network Management", label="Configure VLAN",
    params=[Param("parent_iface", "Parent interface"), Param("vlan_id", "VLAN ID", type="number"),
            Param("vlan_name", "VLAN name", required=False)],
    build=lambda p: api.cmd_configure_vlan(_s(p, "parent_iface"), _i(p, "vlan_id"), _s(p, "vlan_name"))))
_register(Action(name="net_bridge", tool="Network Management", label="Configure bridge",
    params=[Param("bridge_name", "Bridge name"),
            Param("slave_ifaces", "Slave interfaces", help="space-separated")],
    build=lambda p: api.cmd_configure_bridge(_s(p, "bridge_name"), _s(p, "slave_ifaces"))))
_register(Action(name="net_monitor_ports", tool="Network Management", label="Monitor ports",
    params=[], build=lambda p: api.cmd_monitor_ports()))
_register(Action(name="net_tcpdump", tool="Network Management", label="Packet capture (tcpdump)",
    params=[Param("iface", "Interface", required=False), Param("count", "Packets", type="number", default="50", required=False),
            Param("timeout_s", "Timeout (s)", type="number", default="10", required=False),
            Param("filter_expr", "Filter", required=False)],
    build=lambda p: api.cmd_tcpdump_capture(_s(p, "iface"), _i(p, "count", 50),
                                            _i(p, "timeout_s", 10), _s(p, "filter_expr"))))

# ---- Storage Administration (advanced) -------------------------------
_register(Action(name="stor_remove_disk", tool="Storage Administration", label="Remove disk",
    danger=True, params=[Param("device", "Device")],
    build=lambda p: api.cmd_remove_disk(_s(p, "device"))))
_register(Action(name="stor_disk_health", tool="Storage Administration", label="Monitor disk health",
    params=[], build=lambda p: api.cmd_monitor_disk_health()))
_register(Action(name="stor_part_table", tool="Storage Administration", label="Create partition table",
    danger=True, params=[Param("device", "Device"),
        Param("label_type", "Label", type="select", options=["gpt", "msdos"], default="gpt")],
    build=lambda p: api.cmd_create_partition_table(_s(p, "device"), _s(p, "label_type", "gpt"))))
_register(Action(name="stor_part_create", tool="Storage Administration", label="Create partition",
    params=[Param("device", "Device"),
            Param("fs_type", "Filesystem", type="select", options=["ext4", "xfs", "btrfs", "vfat"], default="ext4"),
            Param("start", "Start", default="0%", required=False), Param("end", "End", default="100%", required=False)],
    build=lambda p: api.cmd_create_partition(_s(p, "device"), _s(p, "fs_type", "ext4"),
                                             _s(p, "start", "0%") or "0%", _s(p, "end", "100%") or "100%")))
_register(Action(name="stor_part_delete", tool="Storage Administration", label="Delete partition",
    danger=True, params=[Param("device", "Device"), Param("part_number", "Partition #", type="number")],
    build=lambda p: api.cmd_delete_partition(_s(p, "device"), _i(p, "part_number"))))
_register(Action(name="stor_part_resize", tool="Storage Administration", label="Resize partition",
    params=[Param("device", "Device"), Param("part_number", "Partition #", type="number"),
            Param("end", "New end", help="e.g. 100%")],
    build=lambda p: api.cmd_resize_partition(_s(p, "device"), _i(p, "part_number"), _s(p, "end"))))
_register(Action(name="stor_pv_create", tool="Storage Administration", label="Create physical volume(s)",
    params=[Param("devices", "Devices", help="space-separated")],
    build=lambda p: api.cmd_create_physical_volume(_s(p, "devices"))))
_register(Action(name="stor_vg_create", tool="Storage Administration", label="Create volume group",
    params=[Param("vg_name", "VG name"), Param("devices", "Devices", help="space-separated")],
    build=lambda p: api.cmd_create_volume_group(_s(p, "vg_name"), _s(p, "devices"))))
_register(Action(name="stor_vg_extend", tool="Storage Administration", label="Extend volume group",
    params=[Param("vg_name", "VG name"), Param("devices", "Devices")],
    build=lambda p: api.cmd_extend_volume_group(_s(p, "vg_name"), _s(p, "devices"))))
_register(Action(name="stor_vg_reduce", tool="Storage Administration", label="Reduce volume group",
    danger=True, params=[Param("vg_name", "VG name"), Param("devices", "Devices")],
    build=lambda p: api.cmd_reduce_volume_group(_s(p, "vg_name"), _s(p, "devices"))))
_register(Action(name="stor_lv_extend", tool="Storage Administration", label="Extend logical volume",
    params=[Param("vg_name", "VG name"), Param("lv_name", "LV name"), Param("new_size", "New size"),
            Param("resize_fs", "Resize filesystem", type="checkbox", default=True, required=False)],
    build=lambda p: api.cmd_extend_logical_volume(_s(p, "vg_name"), _s(p, "lv_name"),
                                                  _s(p, "new_size"), _b(p, "resize_fs", True))))
_register(Action(name="stor_lv_reduce", tool="Storage Administration", label="Reduce logical volume",
    danger=True, params=[Param("vg_name", "VG name"), Param("lv_name", "LV name"), Param("new_size", "New size")],
    build=lambda p: api.cmd_reduce_logical_volume(_s(p, "vg_name"), _s(p, "lv_name"), _s(p, "new_size"))))
_register(Action(name="stor_raid_create", tool="Storage Administration", label="Create RAID array",
    params=[Param("raid_device", "md device", help="e.g. /dev/md0"),
            Param("level", "Level", type="select", options=["0", "1", "5", "6", "10"], default="1"),
            Param("devices", "Devices", help="space-separated")],
    build=lambda p: api.cmd_create_raid_array(_s(p, "raid_device"), _s(p, "level", "1"), _s(p, "devices"))))
_register(Action(name="stor_raid_status", tool="Storage Administration", label="RAID status",
    params=[Param("raid_device", "md device", required=False)],
    build=lambda p: api.cmd_raid_status(_s(p, "raid_device"))))
_register(Action(name="stor_raid_replace", tool="Storage Administration", label="Replace failed RAID disk",
    params=[Param("raid_device", "md device"), Param("failed_device", "Failed device"),
            Param("new_device", "New device")],
    build=lambda p: api.cmd_replace_failed_disk(_s(p, "raid_device"), _s(p, "failed_device"), _s(p, "new_device"))))
_register(Action(name="stor_swap_file", tool="Storage Administration", label="Create swap file",
    params=[Param("path", "Path", default="/swapfile"), Param("size_mb", "Size (MB)", type="number", default="1024"),
            Param("persist", "Persist in fstab", type="checkbox", default=True, required=False)],
    build=lambda p: api.cmd_create_swap_file(_s(p, "path", "/swapfile") or "/swapfile",
                                             _i(p, "size_mb", 1024), _b(p, "persist", True))))
_register(Action(name="stor_swap_resize", tool="Storage Administration", label="Resize swap file",
    params=[Param("path", "Path", default="/swapfile"), Param("size_mb", "Size (MB)", type="number", default="2048"),
            Param("persist", "Persist in fstab", type="checkbox", default=True, required=False)],
    build=lambda p: api.cmd_resize_swap_file(_s(p, "path", "/swapfile") or "/swapfile",
                                             _i(p, "size_mb", 2048), _b(p, "persist", True))))
_register(Action(name="stor_swap_partition", tool="Storage Administration", label="Create swap partition",
    params=[Param("device", "Device"), Param("persist", "Persist in fstab", type="checkbox", default=True, required=False)],
    build=lambda p: api.cmd_create_swap_partition(_s(p, "device"), _b(p, "persist", True))))
_register(Action(name="stor_swap_disable", tool="Storage Administration", label="Disable swap",
    params=[Param("target", "Target", help="path or device"),
            Param("remove_fstab", "Remove fstab entry", type="checkbox", default=False, required=False)],
    build=lambda p: api.cmd_disable_swap(_s(p, "target"), _b(p, "remove_fstab"))))

# ---- Firewall Administration (advanced) ------------------------------
_register(Action(name="fw_set_enabled", tool="Firewall Administration", label="Enable/disable firewalld",
    params=[Param("enabled", "Enabled", type="checkbox", default=True, required=False)],
    build=lambda p: api.cmd_set_firewalld_enabled(_b(p, "enabled", True))))
_register(Action(name="fw_set_default_zone", tool="Firewall Administration", label="Set default zone",
    params=[Param("zone", "Zone")], build=lambda p: api.cmd_set_default_zone(_s(p, "zone"))))
_register(Action(name="fw_create_zone", tool="Firewall Administration", label="Create zone",
    params=[Param("zone_name", "Zone name")], build=lambda p: api.cmd_create_zone(_s(p, "zone_name"))))
_register(Action(name="fw_delete_zone", tool="Firewall Administration", label="Delete zone",
    danger=True, params=[Param("zone_name", "Zone name")],
    build=lambda p: api.cmd_delete_zone(_s(p, "zone_name"))))
_register(Action(name="fw_list_rich", tool="Firewall Administration", label="List rich rules",
    params=[Param("zone", "Zone", required=False)], build=lambda p: api.cmd_list_rich_rules(_s(p, "zone"))))
_register(Action(name="fw_add_rich", tool="Firewall Administration", label="Add rich rule",
    params=[Param("rule", "Rich rule"), Param("zone", "Zone", required=False)],
    build=lambda p: api.cmd_add_rich_rule(_s(p, "rule"), _s(p, "zone"))))
_register(Action(name="fw_remove_rich", tool="Firewall Administration", label="Remove rich rule",
    danger=True, params=[Param("rule", "Rich rule"), Param("zone", "Zone", required=False)],
    build=lambda p: api.cmd_remove_rich_rule(_s(p, "rule"), _s(p, "zone"))))
_register(Action(name="fw_nft_add_table", tool="Firewall Administration", label="nft: add table",
    params=[Param("family", "Family", type="select", options=["inet", "ip", "ip6", "arp", "bridge"], default="inet"),
            Param("table", "Table")],
    build=lambda p: api.cmd_nft_add_table(_s(p, "family", "inet"), _s(p, "table"))))
_register(Action(name="fw_nft_add_chain", tool="Firewall Administration", label="nft: add chain",
    params=[Param("family", "Family", type="select", options=["inet", "ip", "ip6"], default="inet"),
            Param("table", "Table"), Param("chain", "Chain"),
            Param("hook", "Hook", required=False), Param("priority", "Priority", default="0", required=False),
            Param("policy", "Policy", type="select", options=["accept", "drop"], default="accept")],
    build=lambda p: api.cmd_nft_add_chain(_s(p, "family", "inet"), _s(p, "table"), _s(p, "chain"),
        _s(p, "hook"), _s(p, "priority", "0") or "0", _s(p, "policy", "accept"))))
_register(Action(name="fw_nft_add_rule", tool="Firewall Administration", label="nft: add rule",
    params=[Param("family", "Family", default="inet"), Param("table", "Table"),
            Param("chain", "Chain"), Param("rule_spec", "Rule spec")],
    build=lambda p: api.cmd_nft_add_rule(_s(p, "family", "inet"), _s(p, "table"),
                                         _s(p, "chain"), _s(p, "rule_spec"))))
_register(Action(name="fw_nft_delete_rule", tool="Firewall Administration", label="nft: delete rule",
    danger=True, params=[Param("family", "Family", default="inet"), Param("table", "Table"),
            Param("chain", "Chain"), Param("handle", "Handle")],
    build=lambda p: api.cmd_nft_delete_rule(_s(p, "family", "inet"), _s(p, "table"),
                                            _s(p, "chain"), _s(p, "handle"))))
_register(Action(name="fw_iptables_add", tool="Firewall Administration", label="iptables: add rule",
    params=[Param("table", "Table", default="filter"), Param("chain", "Chain"), Param("rule_spec", "Rule spec")],
    build=lambda p: api.cmd_iptables_add_rule(_s(p, "table", "filter") or "filter",
                                              _s(p, "chain"), _s(p, "rule_spec"))))
_register(Action(name="fw_iptables_delete", tool="Firewall Administration", label="iptables: delete rule",
    danger=True, params=[Param("table", "Table", default="filter"), Param("chain", "Chain"),
                         Param("rule_spec_or_number", "Rule spec or number")],
    build=lambda p: api.cmd_iptables_delete_rule(_s(p, "table", "filter") or "filter",
                                                 _s(p, "chain"), _s(p, "rule_spec_or_number"))))
_register(Action(name="fw_iptables_save", tool="Firewall Administration", label="iptables: save/persist",
    params=[], build=lambda p: api.cmd_iptables_save_persist()))
_register(Action(name="fw_iptables_flush", tool="Firewall Administration", label="iptables: flush",
    danger=True, params=[Param("table", "Table", default="filter", required=False),
                         Param("chain", "Chain", required=False, help="blank = whole table")],
    build=lambda p: api.cmd_iptables_flush(_s(p, "table", "filter") or "filter", _s(p, "chain"))))
_register(Action(name="fw_nft_flush", tool="Firewall Administration", label="nft: flush ruleset",
    danger=True, params=[], build=lambda p: api.cmd_nft_flush_ruleset()))

# ---- Security Administration (advanced) ------------------------------
_register(Action(name="sec_set_selinux_config", tool="Security Administration",
    label="Set SELinux config mode (persistent)",
    params=[Param("mode", "Mode", type="select", options=["enforcing", "permissive", "disabled"], default="enforcing")],
    build=lambda p: api.cmd_set_selinux_config_mode(_s(p, "mode", "enforcing"))))
_register(Action(name="sec_selinux_booleans", tool="Security Administration", label="List SELinux booleans",
    params=[Param("filter_text", "Filter", required=False)],
    build=lambda p: api.cmd_selinux_list_booleans(_s(p, "filter_text"))))
_register(Action(name="sec_selinux_set_bool", tool="Security Administration", label="Set SELinux boolean",
    params=[Param("name", "Boolean"), Param("enabled", "On", type="checkbox", default=True, required=False),
            Param("permanent", "Permanent", type="checkbox", default=True, required=False)],
    build=lambda p: api.cmd_set_selinux_boolean(_s(p, "name"), _b(p, "enabled", True), _b(p, "permanent", True))))
_register(Action(name="sec_selinux_denials", tool="Security Administration", label="Recent SELinux denials",
    params=[Param("lines", "Lines", type="number", default="50", required=False)],
    build=lambda p: api.cmd_selinux_recent_denials(_i(p, "lines", 50))))
_register(Action(name="sec_selinux_explain", tool="Security Administration", label="Explain SELinux denials",
    params=[Param("lines", "Lines", type="number", default="50", required=False)],
    build=lambda p: api.cmd_selinux_explain_denials(_i(p, "lines", 50))))
_register(Action(name="sec_selinux_journal", tool="Security Administration", label="SELinux journal denials",
    params=[Param("lines", "Lines", type="number", default="50", required=False)],
    build=lambda p: api.cmd_selinux_journal_denials(_i(p, "lines", 50))))
_register(Action(name="sec_selinux_getctx", tool="Security Administration", label="Get SELinux context",
    params=[Param("path", "Path")], build=lambda p: api.cmd_selinux_get_context(_s(p, "path"))))
_register(Action(name="sec_selinux_restorectx", tool="Security Administration", label="Restore SELinux context",
    params=[Param("path", "Path"), Param("recursive", "Recursive", type="checkbox", default=False, required=False)],
    build=lambda p: api.cmd_selinux_restore_context(_s(p, "path"), _b(p, "recursive"))))
_register(Action(name="sec_selinux_list_fctx", tool="Security Administration", label="List file contexts",
    params=[Param("pattern", "Pattern", required=False)],
    build=lambda p: api.cmd_selinux_list_fcontext(_s(p, "pattern"))))
_register(Action(name="sec_selinux_add_fctx", tool="Security Administration", label="Add file context",
    params=[Param("path_regex", "Path regex"), Param("file_type", "Type", help="e.g. httpd_sys_content_t")],
    build=lambda p: api.cmd_selinux_add_fcontext(_s(p, "path_regex"), _s(p, "file_type"))))
_register(Action(name="sec_selinux_rm_fctx", tool="Security Administration", label="Remove file context",
    danger=True, params=[Param("path_regex", "Path regex"), Param("file_type", "Type")],
    build=lambda p: api.cmd_selinux_remove_fcontext(_s(p, "path_regex"), _s(p, "file_type"))))
_register(Action(name="sec_selinux_gen_policy", tool="Security Administration", label="Generate policy from denials",
    params=[Param("module_name", "Module name")],
    build=lambda p: api.cmd_selinux_generate_policy_from_denials(_s(p, "module_name"))))
_register(Action(name="sec_sshd_get", tool="Security Administration", label="Get effective sshd config",
    params=[Param("key", "Option", required=False)],
    build=lambda p: api.cmd_sshd_get_effective_config(_s(p, "key"))))
_register(Action(name="sec_sshd_reload", tool="Security Administration", label="Reload sshd",
    params=[], build=lambda p: api.cmd_sshd_reload()))
_register(Action(name="sec_set_root_login", tool="Security Administration", label="Set root SSH login",
    params=[Param("allow", "Allow root login", type="checkbox", default=False, required=False)],
    build=lambda p: api.cmd_set_root_login(_b(p, "allow"))))
_register(Action(name="sec_set_pubkey_auth", tool="Security Administration", label="Set pubkey auth",
    params=[Param("enabled", "Enabled", type="checkbox", default=True, required=False)],
    build=lambda p: api.cmd_set_pubkey_auth(_b(p, "enabled", True))))
_register(Action(name="sec_set_password_auth", tool="Security Administration", label="Set password auth",
    params=[Param("enabled", "Enabled", type="checkbox", default=False, required=False)],
    build=lambda p: api.cmd_set_password_auth(_b(p, "enabled"))))
_register(Action(name="sec_list_authkeys", tool="Security Administration", label="List authorized keys",
    params=[Param("user", "User")], build=lambda p: api.cmd_list_authorized_keys(_s(p, "user"))))
_register(Action(name="sec_install_authkey", tool="Security Administration", label="Install authorized key",
    params=[Param("user", "User"), Param("public_key", "Public key")],
    build=lambda p: api.cmd_install_authorized_key(_s(p, "user"), _s(p, "public_key"))))
_register(Action(name="sec_remove_authkey", tool="Security Administration", label="Remove authorized key",
    danger=True, params=[Param("user", "User"), Param("match_text", "Match text")],
    build=lambda p: api.cmd_remove_authorized_key(_s(p, "user"), _s(p, "match_text"))))
_register(Action(name="sec_rotate_hostkeys", tool="Security Administration", label="Rotate SSH host keys",
    danger=True, params=[], build=lambda p: api.cmd_rotate_host_keys()))
_register(Action(name="sec_auditd_status", tool="Security Administration", label="auditd status",
    params=[], build=lambda p: api.cmd_auditd_status()))
_register(Action(name="sec_audit_tail", tool="Security Administration", label="Tail audit log",
    params=[Param("lines", "Lines", type="number", default="200", required=False)],
    build=lambda p: api.cmd_tail_audit_log(_i(p, "lines", 200))))
_register(Action(name="sec_audit_search", tool="Security Administration", label="Search audit log",
    params=[Param("query", "Query"), Param("lines", "Lines", type="number", default="200", required=False)],
    build=lambda p: api.cmd_search_audit_log(_s(p, "query"), _i(p, "lines", 200))))
_register(Action(name="sec_failed_summary", tool="Security Administration", label="Failed login summary",
    params=[Param("top_n", "Top N", type="number", default="20", required=False)],
    build=lambda p: api.cmd_failed_login_summary(_i(p, "top_n", 20))))
_register(Action(name="sec_pw_policy", tool="Security Administration", label="Show password policy",
    params=[], build=lambda p: api.cmd_get_password_policy()))
_register(Action(name="sec_set_pwquality", tool="Security Administration", label="Set pwquality option",
    params=[Param("key", "Option"), Param("value", "Value")],
    build=lambda p: api.cmd_set_pwquality_option(_s(p, "key"), _s(p, "value"))))
_register(Action(name="sec_set_aging", tool="Security Administration", label="Set default password aging",
    params=[Param("max_days", "Max days", type="number", required=False),
            Param("min_days", "Min days", type="number", required=False),
            Param("warn_days", "Warn days", type="number", required=False)],
    build=lambda p: api.cmd_set_password_aging(_io(p, "max_days"), _io(p, "min_days"), _io(p, "warn_days"))))
_register(Action(name="sec_set_lockout", tool="Security Administration", label="Set account lockout",
    params=[Param("attempts", "Attempts", type="number", default="5"),
            Param("unlock_seconds", "Unlock seconds", type="number", default="900")],
    build=lambda p: api.cmd_set_account_lockout(_i(p, "attempts", 5), _i(p, "unlock_seconds", 900))))
_register(Action(name="sec_sysctl_harden", tool="Security Administration", label="Apply sysctl hardening",
    params=[], build=lambda p: api.cmd_apply_sysctl_hardening()))
_register(Action(name="sec_disable_coredumps", tool="Security Administration", label="Disable core dumps",
    params=[], build=lambda p: api.cmd_disable_core_dumps()))
_register(Action(name="sec_world_writable", tool="Security Administration", label="List world-writable files",
    params=[Param("path", "Path", default="/etc", required=False)],
    build=lambda p: api.cmd_list_world_writable_files(_s(p, "path", "/etc") or "/etc")))
_register(Action(name="sec_suid", tool="Security Administration", label="List SUID binaries",
    params=[Param("path", "Path", default="/", required=False)],
    build=lambda p: api.cmd_list_suid_binaries(_s(p, "path", "/") or "/")))
_register(Action(name="sec_lynis_status", tool="Security Administration", label="Lynis status",
    params=[], build=lambda p: api.cmd_lynis_status()))
_register(Action(name="sec_run_lynis", tool="Security Administration", label="Run Lynis scan",
    params=[], build=lambda p: api.cmd_run_lynis_scan()))
_register(Action(name="sec_run_rkhunter", tool="Security Administration", label="Run rkhunter scan",
    params=[], build=lambda p: api.cmd_run_rkhunter_scan()))

# ---- File System Management (advanced) -------------------------------
_register(Action(name="fs_rename", tool="File System Management", label="Rename",
    params=[Param("path", "Path"), Param("new_name", "New name")],
    build=lambda p: api.cmd_rename_file(_s(p, "path"), _s(p, "new_name"))))
_register(Action(name="fs_symlink", tool="File System Management", label="Create symlink",
    params=[Param("target", "Target"), Param("link_path", "Link path")],
    build=lambda p: api.cmd_create_symlink(_s(p, "target"), _s(p, "link_path"))))
_register(Action(name="fs_hardlink", tool="File System Management", label="Create hardlink",
    params=[Param("target", "Target"), Param("link_path", "Link path")],
    build=lambda p: api.cmd_create_hardlink(_s(p, "target"), _s(p, "link_path"))))
_register(Action(name="fs_show_acl", tool="File System Management", label="Show ACL",
    params=[Param("path", "Path")], build=lambda p: api.cmd_show_acl(_s(p, "path"))))
_register(Action(name="fs_set_acl", tool="File System Management", label="Set ACL",
    params=[Param("path", "Path"), Param("acl_entries", "ACL entries", help="e.g. u:bob:rwx"),
            Param("recursive", "Recursive", type="checkbox", default=False, required=False)],
    build=lambda p: api.cmd_set_acl(_s(p, "path"), _s(p, "acl_entries"), _b(p, "recursive"))))
_register(Action(name="fs_extract", tool="File System Management", label="Extract archive",
    params=[Param("archive_path", "Archive"), Param("destination_dir", "Destination dir")],
    build=lambda p: api.cmd_extract_archive(_s(p, "archive_path"), _s(p, "destination_dir"))))
_register(Action(name="fs_compress", tool="File System Management", label="Compress file",
    params=[Param("path", "Path"),
            Param("method", "Method", type="select", options=["gzip", "bzip2", "xz"], default="gzip"),
            Param("keep_original", "Keep original", type="checkbox", default=True, required=False)],
    build=lambda p: api.cmd_compress_file(_s(p, "path"), _s(p, "method", "gzip"), _b(p, "keep_original", True))))
_register(Action(name="fs_decompress", tool="File System Management", label="Decompress file",
    params=[Param("path", "Path"), Param("keep_original", "Keep original", type="checkbox", default=True, required=False)],
    build=lambda p: api.cmd_decompress_file(_s(p, "path"), _b(p, "keep_original", True))))
_register(Action(name="fs_mount", tool="File System Management", label="Mount filesystem",
    params=[Param("device", "Device"), Param("mount_point", "Mount point"),
            Param("fstype", "FS type", required=False), Param("options", "Options", required=False)],
    build=lambda p: api.cmd_mount_filesystem(_s(p, "device"), _s(p, "mount_point"),
                                             _s(p, "fstype"), _s(p, "options"))))
_register(Action(name="fs_unmount", tool="File System Management", label="Unmount",
    danger=True,
    params=[Param("target", "Target"), Param("force", "Force", type="checkbox", default=False, required=False)],
    build=lambda p: api.cmd_unmount_filesystem(_s(p, "target"), _b(p, "force"),
                                               allow_critical=_b(p, "allow_critical", False))))
_register(Action(name="fs_add_fstab", tool="File System Management", label="Add fstab entry",
    params=[Param("device", "Device"), Param("mount_point", "Mount point"), Param("fstype", "FS type"),
            Param("options", "Options", default="defaults", required=False),
            Param("dump", "dump", type="number", default="0", required=False),
            Param("pass_num", "pass", type="number", default="0", required=False)],
    build=lambda p: api.cmd_add_fstab_entry(_s(p, "device"), _s(p, "mount_point"), _s(p, "fstype"),
        _s(p, "options", "defaults") or "defaults", _i(p, "dump", 0), _i(p, "pass_num", 0))))
_register(Action(name="fs_remove_fstab", tool="File System Management", label="Remove fstab entry",
    danger=True, params=[Param("mount_point", "Mount point")],
    build=lambda p: api.cmd_remove_fstab_entry(_s(p, "mount_point"),
                                               allow_critical=_b(p, "allow_critical", False))))
_register(Action(name="fs_resize", tool="File System Management", label="Resize filesystem",
    params=[Param("target", "Target"), Param("new_size", "New size", required=False, help="blank = grow to max")],
    build=lambda p: api.cmd_resize_filesystem(_s(p, "target"), _s(p, "new_size"))))
_register(Action(name="fs_repair", tool="File System Management", label="Repair filesystem",
    danger=True, params=[Param("device", "Device")],
    build=lambda p: api.cmd_repair_filesystem(_s(p, "device"))))
_register(Action(name="fs_show_quotas", tool="File System Management", label="Show quotas",
    params=[Param("mount_point", "Mount point", required=False)],
    build=lambda p: api.cmd_show_quotas(_s(p, "mount_point"))))
_register(Action(name="fs_enable_quotas", tool="File System Management", label="Enable quotas",
    params=[Param("mount_point", "Mount point")],
    build=lambda p: api.cmd_enable_quotas(_s(p, "mount_point"))))
_register(Action(name="fs_set_quota", tool="File System Management", label="Set user quota",
    params=[Param("username", "User"), Param("mount_point", "Mount point"),
            Param("block_soft", "Block soft (KB)", type="number"), Param("block_hard", "Block hard (KB)", type="number"),
            Param("inode_soft", "Inode soft", type="number", default="0", required=False),
            Param("inode_hard", "Inode hard", type="number", default="0", required=False)],
    build=lambda p: api.cmd_set_user_quota(_s(p, "username"), _s(p, "mount_point"),
        _i(p, "block_soft"), _i(p, "block_hard"), _i(p, "inode_soft", 0), _i(p, "inode_hard", 0))))

# ---- System Health, Logs & Recovery (advanced) -----------------------
_register(Action(name="health_large_files", tool="System Health, Logs & Recovery", label="Find large files",
    params=[Param("path", "Path", default="/", required=False),
            Param("top_n", "Count", type="number", default="20", required=False)],
    build=lambda p: api.cmd_find_large_files(_s(p, "path", "/") or "/", _i(p, "top_n", 20))))
_register(Action(name="health_journal", tool="System Health, Logs & Recovery", label="Analyze journal logs",
    params=[Param("priority", "Priority", required=False, help="e.g. err"),
            Param("lines", "Lines", type="number", default="200", required=False)],
    build=lambda p: api.cmd_analyze_journal_logs(_s(p, "priority"), _i(p, "lines", 200))))
_register(Action(name="health_app_errors", tool="System Health, Logs & Recovery", label="Trace application errors",
    params=[Param("unit", "Unit", required=False), Param("lines", "Lines", type="number", default="200", required=False)],
    build=lambda p: api.cmd_trace_application_errors(_s(p, "unit"), _i(p, "lines", 200))))
_register(Action(name="health_crashes", tool="System Health, Logs & Recovery", label="Investigate crashes",
    params=[], build=lambda p: api.cmd_investigate_crashes()))
_register(Action(name="health_mem_issues", tool="System Health, Logs & Recovery", label="Troubleshoot memory issues",
    params=[], build=lambda p: api.cmd_troubleshoot_memory_issues()))
_register(Action(name="health_cpu_bottleneck", tool="System Health, Logs & Recovery", label="Analyze CPU bottlenecks",
    params=[], build=lambda p: api.cmd_analyze_cpu_bottlenecks()))
_register(Action(name="health_audit_logs", tool="System Health, Logs & Recovery", label="Review audit logs",
    params=[Param("lines", "Lines", type="number", default="200", required=False)],
    build=lambda p: api.cmd_review_audit_logs(_i(p, "lines", 200))))
_register(Action(name="health_sos_report", tool="System Health, Logs & Recovery", label="Generate sos report",
    params=[], build=lambda p: api.cmd_generate_sos_report()))
_register(Action(name="health_install_sos", tool="System Health, Logs & Recovery", label="Install sos",
    params=[], build=lambda p: api.cmd_install_sos()))
_register(Action(name="health_install_auditd", tool="System Health, Logs & Recovery", label="Install auditd",
    params=[], build=lambda p: api.cmd_install_auditd()))
# Boot & recovery
_register(Action(name="boot_analyze", tool="System Health, Logs & Recovery", label="Analyze boot failures",
    params=[], build=lambda p: api.cmd_analyze_boot_failures()))
_register(Action(name="boot_set_grub_default", tool="System Health, Logs & Recovery", label="Set GRUB default",
    params=[Param("entry", "Entry")], build=lambda p: api.cmd_set_grub_default(_s(p, "entry"))))
_register(Action(name="boot_set_grub_timeout", tool="System Health, Logs & Recovery", label="Set GRUB timeout",
    params=[Param("seconds", "Seconds", type="number", default="5")],
    build=lambda p: api.cmd_set_grub_timeout(_s(p, "seconds", "5"))))
_register(Action(name="boot_rebuild_grub", tool="System Health, Logs & Recovery", label="Rebuild GRUB",
    danger=True, params=[], build=lambda p: api.cmd_rebuild_grub()))
_register(Action(name="boot_set_target", tool="System Health, Logs & Recovery", label="Set boot target",
    params=[Param("target", "Target", type="select",
                  options=["multi-user", "graphical", "rescue", "emergency"], default="multi-user")],
    build=lambda p: api.cmd_set_boot_target(_s(p, "target", "multi-user"))))
_register(Action(name="boot_set_cmdline", tool="System Health, Logs & Recovery", label="Set kernel cmdline",
    params=[Param("params", "Parameters")], build=lambda p: api.cmd_set_kernel_cmdline(_s(p, "params"))))
_register(Action(name="boot_regen_initramfs", tool="System Health, Logs & Recovery", label="Regenerate initramfs",
    danger=True, params=[], build=lambda p: api.cmd_regenerate_initramfs()))
_register(Action(name="boot_remove_kernels", tool="System Health, Logs & Recovery", label="Remove old kernels",
    danger=True, params=[Param("keep", "Keep N", type="number", default="2", required=False)],
    build=lambda p: api.cmd_remove_old_kernels(_s(p, "keep", "2") or "2")))

# ---- Time Synchronization (advanced) ---------------------------------
_register(Action(name="time_configure_chrony", tool="Time Synchronization", label="Configure chrony",
    params=[], build=lambda p: api.cmd_configure_chrony()))
_register(Action(name="time_troubleshoot", tool="Time Synchronization", label="Troubleshoot drift",
    params=[], build=lambda p: api.cmd_troubleshoot_drift()))

# ---- Certificate Management (advanced) -------------------------------
_register(Action(name="cert_install", tool="Certificate Management", label="Install certificate",
    params=[Param("cert_src", "Cert path on host"), Param("key_src", "Key path on host")],
    build=lambda p: api.cmd_install_certificate(_s(p, "cert_src"), _s(p, "key_src"))))
_register(Action(name="cert_renew_certbot", tool="Certificate Management", label="Renew (certbot)",
    params=[Param("domain", "Domain", required=False)],
    build=lambda p: api.cmd_renew_certbot(_s(p, "domain"))))
_register(Action(name="cert_verify_chain", tool="Certificate Management", label="Verify chain",
    params=[Param("cert_path", "Cert path"), Param("chain_path", "Chain path", required=False)],
    build=lambda p: api.cmd_verify_chain(_s(p, "cert_path"), _s(p, "chain_path"))))

# ---- Containers & VMs (advanced) -------------------------------------
_register(Action(name="vm_action", tool="Containers & VMs", label="VM action",
    params=[Param("action", "Action", type="select",
                  options=["start", "shutdown", "destroy", "reboot", "suspend", "resume"], default="start"),
            Param("name", "VM name")],
    build=lambda p: api.cmd_vm_action(_s(p, "action", "start"), _s(p, "name"))))
_register(Action(name="vm_info", tool="Containers & VMs", label="VM info",
    params=[Param("name", "VM name")], build=lambda p: api.cmd_vm_info(_s(p, "name"))))

# ---- Directory Services (advanced) -----------------------------------
_register(Action(name="dir_install_ldap", tool="Directory Services (Active Directory / LDAP)",
    label="Install LDAP dependencies", params=[], build=lambda p: api.cmd_install_ldap_dependencies()))
_register(Action(name="dir_krb_config", tool="Directory Services (Active Directory / LDAP)",
    label="Configure Kerberos", params=[Param("realm", "Realm"), Param("kdc", "KDC"),
        Param("admin_server", "Admin server", required=False)],
    build=lambda p: api.cmd_kerberos_config(_s(p, "realm"), _s(p, "kdc"), _s(p, "admin_server"))))
_register(Action(name="dir_krb_kinit", tool="Directory Services (Active Directory / LDAP)",
    label="Kerberos kinit", params=[Param("principal", "Principal"), Param("password", "Password", type="password")],
    build=lambda p: api.cmd_kerberos_kinit(_s(p, "principal"), _s(p, "password"))))
_register(Action(name="dir_krb_destroy", tool="Directory Services (Active Directory / LDAP)",
    label="Kerberos destroy tickets", params=[], build=lambda p: api.cmd_kerberos_destroy()))
_register(Action(name="dir_realm_permit", tool="Directory Services (Active Directory / LDAP)",
    label="Realm permit", params=[Param("principal", "Principal"),
        Param("is_group", "Is group", type="checkbox", default=False, required=False)],
    build=lambda p: api.cmd_realm_permit(_s(p, "principal"), _b(p, "is_group"))))
_register(Action(name="dir_mkhomedir", tool="Directory Services (Active Directory / LDAP)",
    label="Enable mkhomedir", params=[], build=lambda p: api.cmd_enable_mkhomedir()))
_register(Action(name="dir_test_ldaps", tool="Directory Services (Active Directory / LDAP)",
    label="Test LDAPS", params=[Param("server", "Server"), Param("port", "Port", default="636", required=False),
        Param("base_dn", "Base DN", required=False)],
    build=lambda p: api.cmd_test_ldaps(_s(p, "server"), _s(p, "port", "636") or "636", _s(p, "base_dn"))))
_register(Action(name="dir_config_ldap_client", tool="Directory Services (Active Directory / LDAP)",
    label="Configure LDAP client", params=[Param("server", "Server"), Param("base_dn", "Base DN"),
        Param("use_ldaps", "Use LDAPS", type="checkbox", default=True, required=False)],
    build=lambda p: api.cmd_configure_ldap_client(_s(p, "server"), _s(p, "base_dn"), _b(p, "use_ldaps", True))))

# ---- Backup & Recovery (advanced) ------------------------------------
_register(Action(name="backup_schedule", tool="Backup & Recovery", label="Configure backup schedule",
    params=[Param("source", "Source"), Param("dest_dir", "Destination dir"),
            Param("cron_expr", "Cron schedule", help="e.g. 0 2 * * *")],
    build=lambda p: api.cmd_configure_backup_schedule(_s(p, "source"), _s(p, "dest_dir"), _s(p, "cron_expr"))))
_register(Action(name="backup_snapshot", tool="Backup & Recovery", label="Create LVM snapshot",
    params=[Param("vg", "Volume group"), Param("lv", "Logical volume"),
            Param("snap_name", "Snapshot name"), Param("size", "Size", help="e.g. 1G")],
    build=lambda p: api.cmd_create_snapshot(_s(p, "vg"), _s(p, "lv"), _s(p, "snap_name"), _s(p, "size"))))
_register(Action(name="backup_restore_snapshot", tool="Backup & Recovery", label="Restore LVM snapshot",
    danger=True, params=[Param("vg", "Volume group"), Param("snap_name", "Snapshot name")],
    build=lambda p: api.cmd_restore_snapshot(_s(p, "vg"), _s(p, "snap_name"))))
_register(Action(name="backup_recover_deleted", tool="Backup & Recovery", label="Recover deleted files",
    params=[Param("device", "Device")], build=lambda p: api.cmd_recover_deleted(_s(p, "device"))))
_register(Action(name="backup_test_dr", tool="Backup & Recovery", label="Test disaster recovery",
    params=[Param("dest_dir", "Destination dir")],
    build=lambda p: api.cmd_test_disaster_recovery(_s(p, "dest_dir"))))

# ---- Distro Subscription & Licensing (advanced) ----------------------
_register(Action(name="sub_rhsm_auto_attach", tool="Distro Subscription & Licensing",
    label="RHSM auto-attach", params=[], build=lambda p: api.cmd_rhsm_auto_attach()))
_register(Action(name="sub_rhsm_refresh", tool="Distro Subscription & Licensing",
    label="RHSM refresh", params=[], build=lambda p: api.cmd_rhsm_refresh()))
_register(Action(name="sub_rhsm_consumed", tool="Distro Subscription & Licensing",
    label="RHSM list consumed", params=[], build=lambda p: api.cmd_rhsm_list_consumed()))
_register(Action(name="sub_rhsm_available", tool="Distro Subscription & Licensing",
    label="RHSM list available", params=[], build=lambda p: api.cmd_rhsm_list_available()))
_register(Action(name="sub_rhsm_repos", tool="Distro Subscription & Licensing",
    label="RHSM repos", params=[], build=lambda p: api.cmd_rhsm_repos()))
_register(Action(name="sub_rhsm_unregister", tool="Distro Subscription & Licensing",
    label="RHSM unregister", danger=True, params=[], build=lambda p: api.cmd_rhsm_unregister()))
_register(Action(name="sub_pro_detach", tool="Distro Subscription & Licensing",
    label="Ubuntu Pro detach", danger=True, params=[], build=lambda p: api.cmd_pro_detach()))
_register(Action(name="sub_pro_enable", tool="Distro Subscription & Licensing",
    label="Ubuntu Pro enable service", params=[Param("service", "Service", help="e.g. esm-infra")],
    build=lambda p: api.cmd_pro_enable(_s(p, "service"))))
_register(Action(name="sub_pro_disable", tool="Distro Subscription & Licensing",
    label="Ubuntu Pro disable service", params=[Param("service", "Service")],
    build=lambda p: api.cmd_pro_disable(_s(p, "service"))))
_register(Action(name="sub_pro_refresh", tool="Distro Subscription & Licensing",
    label="Ubuntu Pro refresh", params=[], build=lambda p: api.cmd_pro_refresh()))
_register(Action(name="sub_suse_extensions", tool="Distro Subscription & Licensing",
    label="SUSE list extensions", params=[], build=lambda p: api.cmd_suse_list_extensions()))
_register(Action(name="sub_suse_deregister", tool="Distro Subscription & Licensing",
    label="SUSE deregister", danger=True, params=[], build=lambda p: api.cmd_suse_deregister()))


# ----------------------------------------------------------------------
# Public API used by server.py
# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# Desktop layout map: how each tool's actions are organized into tabs and
# titled group sections, matching the desktop *_page.py pages. Extend this
# per tool; any action not listed falls into a single default tab. Order
# here is the order shown in the UI.
#   tool -> [ (tab, group, [action_name, ...]), ... ]
# ----------------------------------------------------------------------
_LAYOUT: dict[str, list] = {
    "Firewall Administration": [
        ("Firewalld", "Install a Firewall", ["fw_install_firewalld", "fw_install_ufw"]),
        ("Firewalld", "Service State", ["fw_status", "fw_set_enabled", "fw_reload"]),
        ("Firewalld", "Default Zone", ["fw_set_default_zone"]),
        ("Ports", "List Ports", ["fw_list_ports", "fw_list_all_ports"]),
        ("Ports", "Open / Close Ports", ["fw_open_port", "fw_close_port"]),
        ("Zones", "Zones", ["fw_list_zones", "fw_create_zone", "fw_delete_zone"]),
        ("Rich Rules", "Rich Rules", ["fw_list_rich", "fw_add_rich", "fw_remove_rich"]),
        ("nftables", "Ruleset", ["fw_nft_ruleset", "fw_nft_flush"]),
        ("nftables", "Tables & Chains", ["fw_nft_add_table", "fw_nft_add_chain"]),
        ("nftables", "Rules", ["fw_nft_add_rule", "fw_nft_delete_rule"]),
        ("iptables", "Rules", ["fw_iptables_list", "fw_iptables_save",
                               "fw_iptables_add", "fw_iptables_delete", "fw_iptables_flush"]),
    ],
    "Network Management": [
        ("Diagnostics", "Show", ["net_ip", "net_devices", "net_routes", "net_connections", "net_listening"]),
        ("Diagnostics", "Test", ["net_ping", "net_traceroute", "net_monitor_ports", "net_tcpdump"]),
        ("Addressing and DHCP", "Addressing", ["net_set_mtu", "net_static_ip", "net_dhcp"]),
        ("DNS and Hostname", "DNS & Hostname", ["net_hostname", "net_set_hostname", "net_dns", "net_set_dns"]),
        ("Gateway and Routing", "Routing", ["net_add_route", "net_set_gateway"]),
        ("Bonding, Teaming, VLANs, Bridges", "Virtual Interfaces", ["net_bond", "net_team", "net_vlan", "net_bridge"]),
    ],
    "Storage Administration": [
        ("Disks", "Disks", ["stor_list_disks", "stor_rescan", "stor_disk_health", "stor_smart", "stor_remove_disk"]),
        ("Disks", "Install Tools", ["stor_install_smart", "stor_install_lvm", "stor_install_mdadm"]),
        ("Partitions", "Partitions", ["stor_list_parts", "stor_part_table", "stor_part_create", "stor_part_delete", "stor_part_resize"]),
        ("Format Filesystems", "Format", ["stor_format"]),
        ("LVM", "Physical & Volume Groups", ["stor_pv_list", "stor_vg_list", "stor_pv_create", "stor_vg_create", "stor_vg_extend", "stor_vg_reduce"]),
        ("LVM", "Logical Volumes", ["stor_lv_list", "stor_lv_create", "stor_lv_extend", "stor_lv_reduce"]),
        ("RAID", "RAID", ["stor_raid_list", "stor_raid_status", "stor_raid_create", "stor_raid_replace"]),
        ("Swap", "Swap", ["stor_swap_list", "stor_swap_file", "stor_swap_resize", "stor_swap_partition", "stor_swap_disable"]),
    ],
    "Security Administration": [
        ("SELinux", "Status & Mode", ["sec_selinux_status", "sec_install_selinux", "sec_set_selinux_mode", "sec_set_selinux_config"]),
        ("SELinux", "Booleans", ["sec_selinux_booleans", "sec_selinux_set_bool"]),
        ("SELinux", "Denials", ["sec_selinux_denials", "sec_selinux_explain", "sec_selinux_journal"]),
        ("SELinux", "File Contexts", ["sec_selinux_getctx", "sec_selinux_restorectx", "sec_selinux_list_fctx", "sec_selinux_add_fctx", "sec_selinux_rm_fctx", "sec_selinux_gen_policy"]),
        ("SSH", "Service", ["sec_sshd_status", "sec_sshd_get", "sec_sshd_set", "sec_sshd_reload"]),
        ("SSH", "Auth Policy", ["sec_set_root_login", "sec_set_pubkey_auth", "sec_set_password_auth"]),
        ("SSH", "Authorized Keys", ["sec_list_authkeys", "sec_install_authkey", "sec_remove_authkey", "sec_rotate_hostkeys"]),
        ("Audit & Logins", "Logins", ["sec_failed_logins", "sec_locked_accounts", "sec_failed_summary"]),
        ("Audit & Logins", "Auditd", ["sec_auditd_status", "sec_audit_tail", "sec_audit_search"]),
        ("Updates & Policy", "Updates", ["sec_check_updates", "sec_install_updates"]),
        ("Updates & Policy", "Password Policy", ["sec_pw_policy", "sec_set_pwquality", "sec_set_aging", "sec_set_lockout"]),
        ("Hardening & Scans", "Hardening", ["sec_hardening", "sec_sysctl_harden", "sec_disable_coredumps", "sec_world_writable", "sec_suid"]),
        ("Hardening & Scans", "Scanners", ["sec_install_rkhunter", "sec_install_lynis", "sec_lynis_status", "sec_run_lynis", "sec_run_rkhunter"]),
    ],
    "System Health, Logs & Recovery": [
        ("Overview & Disk", "Overview", ["health_check", "health_uptime", "health_mem_cpu", "health_disk_usage", "health_large_files"]),
        ("Processes", "Processes", ["health_processes", "health_failed_services", "health_cpu_bottleneck", "health_mem_issues"]),
        ("Logs", "Logs", ["health_review_logs", "health_search_log", "health_kernel_msgs", "health_journal", "health_app_errors", "health_audit_logs"]),
        ("Diagnostics", "Diagnostics", ["health_crashes", "health_boot_failures"]),
        ("Diagnostics", "Boot & GRUB", ["health_list_kernels", "health_grub", "boot_analyze", "boot_set_grub_default", "boot_set_grub_timeout", "boot_rebuild_grub", "boot_set_target", "boot_set_cmdline", "boot_regen_initramfs", "boot_remove_kernels"]),
        ("Support & Reports", "Support", ["health_support_info", "health_sos_report", "health_install_sos", "health_install_auditd"]),
    ],
    "File System Management": [
        ("Directories and Files", "Directories & Files", ["fs_list_dir", "fs_view", "fs_mkdir", "fs_rmdir", "fs_copy", "fs_move", "fs_rename"]),
        ("Permissions, Ownership and Links", "Permissions & Ownership", ["fs_chmod", "fs_chown", "fs_show_acl", "fs_set_acl"]),
        ("Permissions, Ownership and Links", "Links", ["fs_symlink", "fs_hardlink"]),
        ("Mount / Unmount", "Mount / Unmount", ["fs_mount", "fs_unmount"]),
        ("Network Mounts (NFS/CIFS)", "Network Mounts", ["fs_mount_nfs", "fs_mount_cifs"]),
        ("Resize & Repair", "Resize & Repair", ["fs_resize", "fs_repair"]),
        ("fstab and Quotas", "fstab", ["fs_show_fstab", "fs_add_fstab", "fs_remove_fstab"]),
        ("fstab and Quotas", "Quotas", ["fs_show_quotas", "fs_enable_quotas", "fs_set_quota"]),
        ("Archive and Compress", "Archive & Compress", ["fs_archive", "fs_extract", "fs_compress", "fs_decompress"]),
    ],
    "Distro Subscription & Licensing": [
        ("Overview", "Overview", ["sub_detect", "sub_register_all"]),
        ("Red Hat (RHSM)", "RHSM", ["sub_rhsm_status", "sub_rhsm_register", "sub_rhsm_auto_attach", "sub_rhsm_refresh", "sub_rhsm_consumed", "sub_rhsm_available", "sub_rhsm_repos", "sub_rhsm_unregister"]),
        ("Ubuntu Pro", "Ubuntu Pro", ["sub_pro_status", "sub_pro_attach", "sub_pro_detach", "sub_pro_enable", "sub_pro_disable", "sub_pro_refresh"]),
        ("SUSE (SCC)", "SUSE", ["sub_suse_status", "sub_suse_register", "sub_suse_extensions", "sub_suse_deregister"]),
    ],
    "Containers & VMs": [
        ("Containers", "Containers", ["cont_runtime", "cont_list", "cont_images", "cont_action", "cont_logs", "cont_prune"]),
        ("Virtual Machines", "Virtual Machines", ["vm_list", "vm_action", "vm_info"]),
    ],
    "Directory Services (Active Directory / LDAP)": [
        ("Active Directory", "Active Directory", ["dir_prepare_ad", "dir_install_ad", "dir_realm_status", "dir_join_ad", "dir_leave_ad", "dir_realm_permit", "dir_mkhomedir"]),
        ("LDAP / LDAPS", "LDAP", ["dir_install_ldap", "dir_test_ldaps", "dir_config_ldap_client"]),
        ("Kerberos", "Kerberos", ["dir_krb_status", "dir_krb_config", "dir_krb_kinit", "dir_krb_destroy"]),
    ],
    "Backup & Recovery": [
        ("Files", "Files", ["backup_files", "backup_restore", "backup_verify", "backup_recover_deleted"]),
        ("Snapshots", "LVM Snapshots", ["backup_snapshot", "backup_restore_snapshot"]),
        ("Schedule & DR", "Schedule & DR", ["backup_schedule", "backup_test_dr"]),
    ],
    "Certificate Management": [
        ("Certificates", "Certificates", ["cert_generate_csr", "cert_check", "cert_install", "cert_renew_certbot"]),
        ("Chain & TLS", "Chain & TLS", ["cert_verify_chain", "cert_troubleshoot_tls"]),
    ],
    # ---- single-pane tools: no tab bar, just titled group sections ----
    "Service Management": [
        # One shared "Service name" field, then grouped button rows — matches the
        # desktop's single-field + Service control / Boot & status / Diagnostics.
        ("", "Service (enter a name, then act)", ["svc_status", "svc_start", "svc_stop", "svc_restart",
                                                 "svc_reload", "svc_enable", "svc_disable",
                                                 "svc_troubleshoot", "svc_dependencies", "svc_logs"]),
        ("", "Lists", ["svc_list_running", "svc_list"]),
        ("", "Custom Systemd Service", ["svc_set_deps", "svc_create"]),
        ("", "Processes", ["proc_high_load", "proc_zombies", "proc_kill", "proc_renice", "proc_restart"]),
    ],
    "Host Software Management": [
        ("", "Query", ["pkg_list_installed", "pkg_search", "pkg_query", "pkg_verify", "pkg_detect_env"]),
        ("", "Install / Update / Remove", ["pkg_install", "pkg_update", "pkg_remove", "pkg_install_local"]),
        ("", "Maintenance", ["pkg_clean_cache"]),
    ],
    "Repository Management": [
        ("", "Repositories", ["repo_list", "repo_add", "repo_enable", "repo_disable", "repo_remove", "repo_create"]),
    ],
    "Cron & Systemd Timers": [
        ("", "Cron Jobs", ["cron_list", "cron_add", "cron_remove"]),
        ("", "Systemd Timers", ["timer_list", "timer_status", "timer_start", "timer_stop", "timer_enable", "timer_disable", "timer_create", "timer_delete"]),
    ],
    "Time Synchronization": [
        ("", "Status", ["time_status", "time_verify", "time_troubleshoot"]),
        ("", "Configure", ["time_set_ntp", "time_set_tz", "time_list_tz", "time_configure_chrony"]),
    ],
}

_ORDER: dict[str, int] = {}


def _apply_layout():
    for tool, sections in _LAYOUT.items():
        idx = 0
        for tab, group, names in sections:
            for nm in names:
                a = _ACTIONS.get(nm)
                if a is not None:
                    a.tab, a.group = tab, group
                    _ORDER[nm] = idx
                idx += 1


_apply_layout()


def get(name: str):
    return _ACTIONS.get(name)


def catalog():
    """Group actions by tool for the SPA, serializing Param to plain
    dicts. The build= callable is intentionally not serialized."""
    by_tool: dict[str, list] = {}
    for a in _ACTIONS.values():
        by_tool.setdefault(a.tool, []).append({
            "name": a.name,
            "label": a.label,
            "description": a.description,
            "danger": a.danger,
            "tab": a.tab,
            "group": a.group,
            "params": [
                {
                    "name": pr.name, "label": pr.label, "type": pr.type,
                    "default": pr.default, "required": pr.required,
                    "options": pr.options, "help": pr.help,
                }
                for pr in a.params
            ],
        })
    # Order each tool's actions by the desktop layout (unlisted actions keep
    # registration order, after the laid-out ones).
    for acts in by_tool.values():
        acts.sort(key=lambda a: _ORDER.get(a["name"], 10_000))
    return [{"tool": tool, "actions": acts} for tool, acts in by_tool.items()]
