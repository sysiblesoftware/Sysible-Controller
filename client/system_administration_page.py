from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QGridLayout, QLabel, QLineEdit

from client.dashboard_card import DashboardCard
from client.branding import make_page_header
from client import theme
from client.user_group_administration_page import UserGroupAdministrationPage
from client.system_health_recovery_page import SystemHealthRecoveryPage
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
from client.backup_recovery_page import BackupRecoveryPage
from client.time_synchronization_page import TimeSynchronizationPage
from client.certificate_management_page import CertificateManagementPage
from client.containers_vms_page import ContainersVMsPage
from client.directory_services_page import DirectoryServicesPage
from client.subscription_management_page import SubscriptionManagementPage


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
        self.resize(840, 720)

        self.user_group_window = None
        self.diagnostics_window = None
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
        self.backup_recovery_window = None
        self.timesync_window = None
        self.cert_mgmt_window = None
        self.containers_vms_window = None
        self.directory_window = None
        self.subscription_window = None

        main = QVBoxLayout()
        self.setLayout(main)

        main.addLayout(make_page_header("System Administration"))

        self.subtitle_label = QLabel("Select a tool below.")
        self.subtitle_label.setAlignment(Qt.AlignCenter)
        main.addWidget(self.subtitle_label)

        self._apply_subtitle_theme()
        theme.add_theme_listener(self._apply_subtitle_theme)

        # Search box - filters the tiles below as you type, mirroring the
        # main dashboard's search. Matches the typed words against each
        # tool's title and description, so "raid", "firewall", or "open a
        # port" narrows the grid to the relevant tools.
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(
            "Search tools… e.g. \"users\", \"firewall\", \"raid\", \"certificates\""
        )
        self.search_input.textChanged.connect(self._filter_tiles)
        main.addWidget(self.search_input)

        main.addSpacing(12)

        self._grid = QGridLayout()
        self._grid.setHorizontalSpacing(16)
        self._grid.setVerticalSpacing(16)
        for col in range(3):
            self._grid.setColumnStretch(col, 1)
        grid = self._grid

        cards = [
            ("User & Group Administration",
             "Create, lock, and manage user accounts, passwords, sudo access, and groups across agent and SSH hosts.",
             self.open_user_group_admin, "fa5s.users", "slate"),
            ("System Health, Logs & Recovery",
             "Disk usage, memory/CPU, failed services, logs, and process tools, plus boot/GRUB "
             "and kernel recovery — across agent and SSH hosts.",
             self.open_system_diagnostics, "fa5s.heartbeat", "green"),
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
            ("Backup & Recovery",
             "Back up and restore files, verify backup integrity, schedule backups, create and "
             "restore LVM snapshots, guide deleted-file recovery, and run disaster-recovery drills.",
             self.open_backup_recovery, "fa5s.save", "teal"),
            ("Time Synchronization",
             "Configure NTP/chrony, verify synchronization, troubleshoot clock drift, and set the "
             "system time zone across managed hosts.",
             self.open_timesync, "fa5s.clock", "sky"),
            ("Certificate Management",
             "Generate CSRs, install/renew/replace certificates, verify certificate chains, and "
             "troubleshoot TLS endpoints across managed hosts.",
             self.open_cert_mgmt, "fa5s.certificate", "rose"),
            ("Containers & VMs",
             "List and start/stop/restart Docker or Podman containers, view container logs and images, "
             "and manage libvirt virtual machines across managed hosts.",
             self.open_containers_vms, "fa5s.cube", "indigo"),
            ("Directory Services (Active Directory / LDAP)",
             "Join hosts to Active Directory (realmd/SSSD), manage realm status and login permits, "
             "enable home-dir creation, and configure/test LDAP and LDAPS.",
             self.open_directory, "fa5s.users-cog", "sky"),
            ("Subscription & Licensing",
             "Register and manage commercial-distro subscriptions: Red Hat (subscription-manager), "
             "Ubuntu Pro, and SUSE (SUSEConnect) — status, attach/enable, and repositories.",
             self.open_subscriptions, "fa5s.id-card", "amber"),
        ]

        # Build every card once and keep it with a lowercase haystack
        # (title + description) for the search filter; the grid is then
        # (re)populated by _relayout_tiles so hiding a tile leaves no gap.
        self._tiles = []
        for card_title, description, handler, icon, color in cards:
            card = DashboardCard(card_title, description, handler, icon, color)
            self._tiles.append((card, f"{card_title} {description}".lower()))

        main.addLayout(grid)
        main.addStretch()

        self._no_results = QLabel("No tools match your search.")
        self._no_results.setAlignment(Qt.AlignCenter)
        theme.style_hint_label(self._no_results)
        self._no_results.setVisible(False)
        main.addWidget(self._no_results)

        self._relayout_tiles(self._tiles)

    def _relayout_tiles(self, tiles):
        """Clear the grid and lay out `tiles` (a list of (card, haystack))
        in three columns, with no gaps for filtered-out cards."""
        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        for index, (card, _haystack) in enumerate(tiles):
            row, col = divmod(index, 3)
            self._grid.addWidget(card, row, col)
            card.setVisible(True)
        self._no_results.setVisible(not tiles)

    def _filter_tiles(self, text):
        terms = text.strip().lower().split()
        if not terms:
            matches = self._tiles
        else:
            matches = [t for t in self._tiles if all(term in t[1] for term in terms)]
        self._relayout_tiles(matches)

    def _apply_subtitle_theme(self):
        color = "#6B7280" if theme.get_theme_mode() == "light" else "#9aa5b1"
        self.subtitle_label.setStyleSheet(f"font-size: 11px; color: {color};")

    def open_user_group_admin(self):
        if self.user_group_window is None:
            self.user_group_window = UserGroupAdministrationPage()
        self.user_group_window.show()
        self.user_group_window.raise_()
        return self.user_group_window

    def open_system_diagnostics(self):
        if self.diagnostics_window is None:
            self.diagnostics_window = SystemHealthRecoveryPage()
        self.diagnostics_window.show()
        self.diagnostics_window.raise_()
        return self.diagnostics_window

    def open_health_logs(self):
        # Kept so the dashboard feature search ("disk usage", "tail logs", ...)
        # still works: opens the combined window on its Health & Logs tab.
        return self.open_system_diagnostics().show_health()

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

    def open_backup_recovery(self):
        if self.backup_recovery_window is None:
            self.backup_recovery_window = BackupRecoveryPage()
        self.backup_recovery_window.show()
        self.backup_recovery_window.raise_()
        return self.backup_recovery_window

    def open_boot_recovery(self):
        # Feature search ("rebuild grub", "remove old kernels", ...) opens the
        # combined window on its Boot & Recovery tab.
        return self.open_system_diagnostics().show_boot()

    def open_timesync(self):
        if self.timesync_window is None:
            self.timesync_window = TimeSynchronizationPage()
        self.timesync_window.show()
        self.timesync_window.raise_()
        return self.timesync_window

    def open_cert_mgmt(self):
        if self.cert_mgmt_window is None:
            self.cert_mgmt_window = CertificateManagementPage()
        self.cert_mgmt_window.show()
        self.cert_mgmt_window.raise_()
        return self.cert_mgmt_window

    def open_containers_vms(self):
        if self.containers_vms_window is None:
            self.containers_vms_window = ContainersVMsPage()
        self.containers_vms_window.show()
        self.containers_vms_window.raise_()
        return self.containers_vms_window

    def open_directory(self):
        if self.directory_window is None:
            self.directory_window = DirectoryServicesPage()
        self.directory_window.show()
        self.directory_window.raise_()
        return self.directory_window

    def open_subscriptions(self):
        if self.subscription_window is None:
            self.subscription_window = SubscriptionManagementPage()
        self.subscription_window.show()
        self.subscription_window.raise_()
        return self.subscription_window
