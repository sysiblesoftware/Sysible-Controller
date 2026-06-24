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
_register(Action(name="fs_mkdir", tool="File System Management", label="Create directory",
    params=[Param("path", "Path")], build=lambda p: api.cmd_create_directory(_s(p, "path"))))
_register(Action(name="fs_rmdir", tool="File System Management", label="Remove directory",
    danger=True, params=[Param("path", "Path"),
                         Param("recursive", "Recursive", type="checkbox", default=False, required=False)],
    build=lambda p: api.cmd_remove_directory(_s(p, "path"), _b(p, "recursive"))))
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
                  options=["start", "stop", "restart", "pause", "unpause", "kill"], default="start"),
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


# ----------------------------------------------------------------------
# Public API used by server.py
# ----------------------------------------------------------------------
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
            "params": [
                {
                    "name": pr.name, "label": pr.label, "type": pr.type,
                    "default": pr.default, "required": pr.required,
                    "options": pr.options, "help": pr.help,
                }
                for pr in a.params
            ],
        })
    return [{"tool": tool, "actions": acts} for tool, acts in by_tool.items()]
