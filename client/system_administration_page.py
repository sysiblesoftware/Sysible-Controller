from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QGridLayout, QLabel

from client.dashboard_card import DashboardCard
from client.branding import make_page_header
from client import theme
from client.user_group_administration_page import UserGroupAdministrationPage
from client.system_health_logs_page import SystemHealthLogsPage
from client.service_management_page import ServiceManagementPage
from client.environmental_policies_page import EnvironmentalPoliciesPage
from client.cron_systemd_timers_page import CronSystemdTimersPage
from client.host_software_management_page import HostSoftwareManagementPage
from client.repository_management_page import RepositoryManagementPage
from client.network_management_page import NetworkManagementPage
from client.file_system_management_page import FileSystemManagementPage
from client.storage_administration_page import StorageAdministrationPage
from client.firewall_administration_page import FirewallAdministrationPage
from client.security_administration_page import SecurityAdministrationPage


class SystemAdministrationPage(QWidget):
    """
    System Administration menu: a small sub-dashboard with one tile per
    System Administration tool, opening each as its own focused window.

    Previously this page held User & Group Administration and System
    Health & Logs side by side as two tabs of one big window. That made
    every control - host checklist, sync button, user panel, health
    actions - fight for space in a single cluttered view. Splitting them
    into their own pages (client/user_group_administration_page.py and
    client/system_health_logs_page.py) keeps each tool's host list and
    controls focused on just that tool.
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("System Administration")
        self.resize(560, 760)

        self.user_group_window = None
        self.health_window = None
        self.service_window = None
        self.environmental_policies_window = None
        self.cron_timers_window = None
        self.software_mgmt_window = None
        self.repo_mgmt_window = None
        self.network_mgmt_window = None
        self.filesystem_mgmt_window = None
        self.storage_admin_window = None
        self.firewall_admin_window = None
        self.security_admin_window = None

        main = QVBoxLayout()
        self.setLayout(main)

        main.addLayout(make_page_header("System Administration"))

        self.subtitle_label = QLabel("Select a tool below.")
        self.subtitle_label.setAlignment(Qt.AlignCenter)
        main.addWidget(self.subtitle_label)

        self._apply_subtitle_theme()
        theme.add_theme_listener(self._apply_subtitle_theme)

        main.addSpacing(12)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(16)
        for col in range(2):
            grid.setColumnStretch(col, 1)

        cards = [
            ("User & Group Administration",
             "Create, lock, and manage user accounts, passwords, sudo access, and groups across agent and SSH hosts.",
             self.open_user_group_admin, "fa5s.users", "slate"),
            ("System Health & Logs",
             "Disk usage, memory/CPU snapshots, failed services, large files, and log search across agent and SSH hosts.",
             self.open_health_logs, "fa5s.heartbeat", "green"),
            ("Service Management",
             "Start, stop, restart, enable/disable, and troubleshoot systemd services, or create and configure new ones.",
             self.open_service_management, "fa5s.cogs", "purple"),
            ("Environmental Policies",
             "Set the baseline password, lockout, sudo, and umask policy for accounts on managed hosts, and push it out.",
             self.open_environmental_policies, "fa5s.shield-alt", "coral"),
            ("Cron & Systemd Timers",
             "View, add, and remove cron jobs, and view, create, start/stop, enable/disable, and delete systemd timers.",
             self.open_cron_timers, "fa5s.clock", "amber"),
            ("Host Software Management",
             "Detect each host's package manager, then install, remove, update, query, verify, and clean packages "
             "across dnf/yum, zypper, and apt hosts alike.",
             self.open_software_mgmt, "fa5s.box", "teal"),
            ("Repository Management",
             "List, add, enable, disable, and remove software repositories across dnf/yum, zypper, and apt hosts.",
             self.open_repo_mgmt, "fa5s.code-branch", "rose"),
            ("Network Management",
             "Diagnose connectivity and DNS, inspect ports and capture packets, and configure IP/DHCP/DNS/gateway/"
             "routing/hostname/bonding/teaming/VLANs/bridges/MTU across managed hosts.",
             self.open_network_mgmt, "fa5s.network-wired", "sky"),
            ("File System Management",
             "Create/remove directories, copy/move/rename files, manage ownership/permissions/ACLs and "
             "links, mount/unmount/resize/repair filesystems, configure /etc/fstab and quotas, and "
             "archive/compress files across managed hosts.",
             self.open_filesystem_mgmt, "fa5s.hdd", "indigo"),
            ("Storage Administration",
             "Partition, format, and monitor disks, manage LVM physical volumes/volume groups/logical "
             "volumes, configure RAID and replace failed disks, and set up swap space across managed hosts.",
             self.open_storage_admin, "fa5s.database", "copper"),
            ("Firewall Administration",
             "Configure firewalld zones, ports, and rich rules, and manage the underlying "
             "nftables and iptables rule sets across managed hosts.",
             self.open_firewall_admin, "fa5s.fire", "crimson"),
            ("Security Administration",
             "Configure and troubleshoot SELinux, harden SSH access and rotate keys, review "
             "audit logs and failed logins, install security updates, set password policy, "
             "harden systems, and run vulnerability scans across managed hosts.",
             self.open_security_admin, "fa5s.lock", "graphite"),
        ]

        for index, (card_title, description, handler, icon, color) in enumerate(cards):
            row, col = divmod(index, 2)
            grid.addWidget(
                DashboardCard(card_title, description, handler, icon, color),
                row, col,
            )

        main.addLayout(grid)
        main.addStretch()

    def _apply_subtitle_theme(self):
        color = "#6B7280" if theme.get_theme_mode() == "light" else "#9aa5b1"
        self.subtitle_label.setStyleSheet(f"font-size: 11px; color: {color};")

    def open_user_group_admin(self):
        if self.user_group_window is None:
            self.user_group_window = UserGroupAdministrationPage()
        self.user_group_window.show()
        self.user_group_window.raise_()
        return self.user_group_window

    def open_health_logs(self):
        if self.health_window is None:
            self.health_window = SystemHealthLogsPage()
        self.health_window.show()
        self.health_window.raise_()
        return self.health_window

    def open_service_management(self):
        if self.service_window is None:
            self.service_window = ServiceManagementPage()
        self.service_window.show()
        self.service_window.raise_()
        return self.service_window

    def open_environmental_policies(self):
        if self.environmental_policies_window is None:
            self.environmental_policies_window = EnvironmentalPoliciesPage()
        self.environmental_policies_window.show()
        self.environmental_policies_window.raise_()
        return self.environmental_policies_window

    def open_cron_timers(self):
        if self.cron_timers_window is None:
            self.cron_timers_window = CronSystemdTimersPage()
        self.cron_timers_window.show()
        self.cron_timers_window.raise_()
        return self.cron_timers_window

    def open_software_mgmt(self):
        if self.software_mgmt_window is None:
            self.software_mgmt_window = HostSoftwareManagementPage()
        self.software_mgmt_window.show()
        self.software_mgmt_window.raise_()
        return self.software_mgmt_window

    def open_repo_mgmt(self):
        if self.repo_mgmt_window is None:
            self.repo_mgmt_window = RepositoryManagementPage()
        self.repo_mgmt_window.show()
        self.repo_mgmt_window.raise_()
        return self.repo_mgmt_window

    def open_network_mgmt(self):
        if self.network_mgmt_window is None:
            self.network_mgmt_window = NetworkManagementPage()
        self.network_mgmt_window.show()
        self.network_mgmt_window.raise_()
        return self.network_mgmt_window

    def open_filesystem_mgmt(self):
        if self.filesystem_mgmt_window is None:
            self.filesystem_mgmt_window = FileSystemManagementPage()
        self.filesystem_mgmt_window.show()
        self.filesystem_mgmt_window.raise_()
        return self.filesystem_mgmt_window

    def open_storage_admin(self):
        if self.storage_admin_window is None:
            self.storage_admin_window = StorageAdministrationPage()
        self.storage_admin_window.show()
        self.storage_admin_window.raise_()
        return self.storage_admin_window

    def open_firewall_admin(self):
        if self.firewall_admin_window is None:
            self.firewall_admin_window = FirewallAdministrationPage()
        self.firewall_admin_window.show()
        self.firewall_admin_window.raise_()
        return self.firewall_admin_window

    def open_security_admin(self):
        if self.security_admin_window is None:
            self.security_admin_window = SecurityAdministrationPage()
        self.security_admin_window.show()
        self.security_admin_window.raise_()
        return self.security_admin_window
