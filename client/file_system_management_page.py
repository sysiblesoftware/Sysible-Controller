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

HOST_REFRESH_MS = 10000
FS_POLL_MS = 2000


def _entry_key(entry):
    """Hashable identity for a merged-host entry (api.list_merged_hosts()) -
    agent host_ids and SSH host names live in separate namespaces, so the
    kind has to be part of the key."""
    return (entry["kind"], entry["id"])


class FileSystemManagementPage(QWidget):
    """
    File System Management against a *merged* host list (agent-enrolled
    hosts AND SSH-enrolled hosts) - directory/file operations, ownership/
    permissions/ACLs, links, mount/unmount/resize/repair, /etc/fstab,
    quotas, and archive/compress, all dispatched to whichever hosts are
    checked, same as every other System Administration tool.

    File/directory-level operations (create/remove dirs, copy/move/
    rename, chown/chmod/ACLs, links, archive/compress) are universal
    coreutils/tar/gzip-family commands with no filesystem-type
    assumption - see client/_api_filesystem.py.

    Mount/resize/repair are filesystem-type-aware (ext2/3/4 vs xfs vs
    btrfs) and auto-detect which tool to call - see client/
    _api_filesystem_mount.py. Check Disk Usage and Find Large Files
    reuse the existing System Health & Logs command builders
    (api.cmd_disk_usage() / api.cmd_find_large_files()) rather than
    duplicating them.
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("File System Management")
        self.resize(1150, 860)

        self.fs_results = {}   # entry_key -> {label, stdout, stderr, code, pending}
        self.fs_pending = {}   # entry_key -> (entry, task_id)
        self.last_command_label = None
        self._collapsed_envs = set()

        main = QVBoxLayout()
        self.setLayout(main)

        main.addLayout(make_page_header("File System Management"))

        # =========================================================
        # TARGET HOSTS (agent + SSH, merged)
        # =========================================================
        hosts_box = QVBoxLayout()

        self.host_list = QListWidget()
        self.host_list.setFixedHeight(70)
        connect_group_toggle(self.host_list)

        hosts_header = QHBoxLayout()
        hosts_title = QLabel("Target Hosts (agent + SSH)")
        hosts_title.setStyleSheet("font-weight: bold;")
        hosts_header.addWidget(hosts_title)
        hosts_header.addStretch()

        btn_refresh_hosts = QPushButton("Refresh Hosts")
        btn_refresh_hosts.clicked.connect(self.load_hosts)

        btn_select_all = QPushButton("Select All")
        btn_select_all.clicked.connect(self.select_all_hosts)

        btn_deselect_all = QPushButton("Deselect All")
        btn_deselect_all.clicked.connect(self.deselect_all_hosts)

        btn_collapse_all, btn_expand_all = add_collapse_expand_buttons(self.host_list)

        hosts_header.addWidget(btn_refresh_hosts)
        hosts_header.addWidget(btn_select_all)
        hosts_header.addWidget(btn_deselect_all)
        hosts_header.addWidget(btn_collapse_all)
        hosts_header.addWidget(btn_expand_all)

        hosts_box.addLayout(hosts_header)
        hosts_box.addWidget(self.host_list)

        main.addLayout(hosts_box)

        # =========================================================
        # ACTIONS (tabbed - 19 distinct features grouped logically)
        # =========================================================
        action_tabs = QTabWidget()
        action_tabs.addTab(self._build_dirs_files_tab(), "Directories and Files")
        action_tabs.addTab(self._build_permissions_links_tab(), "Permissions, Ownership and Links")
        action_tabs.addTab(self._build_mount_tab(), "Mount and Filesystem")
        action_tabs.addTab(self._build_fstab_quota_tab(), "fstab and Quotas")
        action_tabs.addTab(self._build_archive_tab(), "Archive and Compress")
        main.addWidget(action_tabs)

        # =========================================================
        # RESULTS (stretchy - see service_management_page.py for why)
        # =========================================================
        main.addWidget(self._build_results_panel(), 1)

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

        self.fs_poll_timer = QTimer()
        self.fs_poll_timer.timeout.connect(self._poll_fs)

        bus.host_removed.connect(self.load_hosts)

    # =========================================================
    # ACTION PANEL BUILDERS
    # =========================================================
    def _build_dirs_files_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        dir_title = QLabel("Directories")
        dir_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(dir_title)

        create_row = QHBoxLayout()
        create_row.addWidget(QLabel("Path:"))
        self.create_dir_path_input = QLineEdit()
        self.create_dir_path_input.setPlaceholderText("e.g. /data/new-folder")
        create_row.addWidget(self.create_dir_path_input, 1)
        create_row.addWidget(QLabel("Mode (optional):"))
        self.create_dir_mode_input = QLineEdit()
        self.create_dir_mode_input.setPlaceholderText("e.g. 755")
        self.create_dir_mode_input.setMaximumWidth(70)
        create_row.addWidget(self.create_dir_mode_input)
        btn_create_dir = QPushButton("Create Directory")
        btn_create_dir.clicked.connect(self.run_create_directory)
        create_row.addWidget(btn_create_dir)
        layout.addLayout(create_row)

        remove_row = QHBoxLayout()
        remove_row.addWidget(QLabel("Path:"))
        self.remove_dir_path_input = QLineEdit()
        self.remove_dir_path_input.setPlaceholderText("e.g. /data/old-folder")
        remove_row.addWidget(self.remove_dir_path_input, 1)
        self.remove_dir_recursive_check = QCheckBox("Recursive (rm -rf)")
        remove_row.addWidget(self.remove_dir_recursive_check)
        btn_remove_dir = QPushButton("Remove Directory")
        btn_remove_dir.clicked.connect(self.run_remove_directory)
        remove_row.addWidget(btn_remove_dir)
        layout.addLayout(remove_row)

        layout.addSpacing(14)

        files_title = QLabel("Files")
        files_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(files_title)

        hint = QLabel(
            "Copy/Move apply to a file or, with Copy's Recursive box checked, a whole "
            "directory tree. Rename only changes the filename - use Move to relocate "
            "to a different directory."
        )
        theme.style_hint_label(hint)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        copy_row = QHBoxLayout()
        copy_row.addWidget(QLabel("Source:"))
        self.copy_source_input = QLineEdit()
        copy_row.addWidget(self.copy_source_input, 1)
        copy_row.addWidget(QLabel("Destination:"))
        self.copy_dest_input = QLineEdit()
        copy_row.addWidget(self.copy_dest_input, 1)
        self.copy_recursive_check = QCheckBox("Recursive")
        self.copy_recursive_check.setChecked(True)
        copy_row.addWidget(self.copy_recursive_check)
        btn_copy = QPushButton("Copy")
        btn_copy.clicked.connect(self.run_copy_file)
        copy_row.addWidget(btn_copy)
        layout.addLayout(copy_row)

        move_row = QHBoxLayout()
        move_row.addWidget(QLabel("Source:"))
        self.move_source_input = QLineEdit()
        move_row.addWidget(self.move_source_input, 1)
        move_row.addWidget(QLabel("Destination:"))
        self.move_dest_input = QLineEdit()
        move_row.addWidget(self.move_dest_input, 1)
        btn_move = QPushButton("Move")
        btn_move.clicked.connect(self.run_move_file)
        move_row.addWidget(btn_move)
        layout.addLayout(move_row)

        rename_row = QHBoxLayout()
        rename_row.addWidget(QLabel("Path:"))
        self.rename_path_input = QLineEdit()
        self.rename_path_input.setPlaceholderText("e.g. /data/report.txt")
        rename_row.addWidget(self.rename_path_input, 1)
        rename_row.addWidget(QLabel("New name:"))
        self.rename_new_name_input = QLineEdit()
        self.rename_new_name_input.setPlaceholderText("e.g. report-final.txt")
        rename_row.addWidget(self.rename_new_name_input, 1)
        btn_rename = QPushButton("Rename")
        btn_rename.clicked.connect(self.run_rename_file)
        rename_row.addWidget(btn_rename)
        layout.addLayout(rename_row)

        layout.addStretch()
        return panel

    def _build_permissions_links_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        own_title = QLabel("Ownership and Permissions")
        own_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(own_title)

        chown_row = QHBoxLayout()
        chown_row.addWidget(QLabel("Path:"))
        self.chown_path_input = QLineEdit()
        chown_row.addWidget(self.chown_path_input, 1)
        chown_row.addWidget(QLabel("Owner:"))
        self.chown_owner_input = QLineEdit()
        self.chown_owner_input.setMaximumWidth(100)
        chown_row.addWidget(self.chown_owner_input)
        chown_row.addWidget(QLabel("Group:"))
        self.chown_group_input = QLineEdit()
        self.chown_group_input.setMaximumWidth(100)
        chown_row.addWidget(self.chown_group_input)
        self.chown_recursive_check = QCheckBox("Recursive")
        chown_row.addWidget(self.chown_recursive_check)
        btn_chown = QPushButton("Change Ownership")
        btn_chown.clicked.connect(self.run_change_ownership)
        chown_row.addWidget(btn_chown)
        layout.addLayout(chown_row)

        chmod_row = QHBoxLayout()
        chmod_row.addWidget(QLabel("Path:"))
        self.chmod_path_input = QLineEdit()
        chmod_row.addWidget(self.chmod_path_input, 1)
        chmod_row.addWidget(QLabel("Mode:"))
        self.chmod_mode_input = QLineEdit()
        self.chmod_mode_input.setPlaceholderText("e.g. 755 or u+x")
        self.chmod_mode_input.setMaximumWidth(90)
        chmod_row.addWidget(self.chmod_mode_input)
        self.chmod_recursive_check = QCheckBox("Recursive")
        chmod_row.addWidget(self.chmod_recursive_check)
        btn_chmod = QPushButton("Change Permissions")
        btn_chmod.clicked.connect(self.run_change_permissions)
        chmod_row.addWidget(btn_chmod)
        layout.addLayout(chmod_row)

        layout.addSpacing(14)

        acl_title = QLabel("ACLs")
        acl_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(acl_title)

        acl_hint = QLabel("Entries format: u:alice:rwx,g:devs:rx (comma-separated, no spaces).")
        theme.style_hint_label(acl_hint)
        layout.addWidget(acl_hint)

        acl_row = QHBoxLayout()
        acl_row.addWidget(QLabel("Path:"))
        self.acl_path_input = QLineEdit()
        acl_row.addWidget(self.acl_path_input, 1)
        acl_row.addWidget(QLabel("ACL entries:"))
        self.acl_entries_input = QLineEdit()
        self.acl_entries_input.setPlaceholderText("e.g. u:alice:rwx,g:devs:rx")
        acl_row.addWidget(self.acl_entries_input, 1)
        self.acl_recursive_check = QCheckBox("Recursive")
        acl_row.addWidget(self.acl_recursive_check)
        btn_set_acl = QPushButton("Set ACL")
        btn_set_acl.clicked.connect(self.run_set_acl)
        acl_row.addWidget(btn_set_acl)
        btn_show_acl = QPushButton("Show ACL")
        btn_show_acl.clicked.connect(self.run_show_acl)
        acl_row.addWidget(btn_show_acl)
        layout.addLayout(acl_row)

        layout.addSpacing(14)

        links_title = QLabel("Links")
        links_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(links_title)

        symlink_row = QHBoxLayout()
        symlink_row.addWidget(QLabel("Target:"))
        self.symlink_target_input = QLineEdit()
        symlink_row.addWidget(self.symlink_target_input, 1)
        symlink_row.addWidget(QLabel("Link path:"))
        self.symlink_link_input = QLineEdit()
        symlink_row.addWidget(self.symlink_link_input, 1)
        btn_symlink = QPushButton("Create Symbolic Link")
        btn_symlink.clicked.connect(self.run_create_symlink)
        symlink_row.addWidget(btn_symlink)
        layout.addLayout(symlink_row)

        hardlink_row = QHBoxLayout()
        hardlink_row.addWidget(QLabel("Target:"))
        self.hardlink_target_input = QLineEdit()
        hardlink_row.addWidget(self.hardlink_target_input, 1)
        hardlink_row.addWidget(QLabel("Link path:"))
        self.hardlink_link_input = QLineEdit()
        hardlink_row.addWidget(self.hardlink_link_input, 1)
        btn_hardlink = QPushButton("Create Hard Link")
        btn_hardlink.clicked.connect(self.run_create_hardlink)
        hardlink_row.addWidget(btn_hardlink)
        layout.addLayout(hardlink_row)

        hardlink_hint = QLabel("Hard links require Target and Link path to be on the same filesystem.")
        theme.style_hint_label(hardlink_hint)
        layout.addWidget(hardlink_hint)

        layout.addStretch()
        return panel

    def _build_mount_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        mount_title = QLabel("Mount / Unmount")
        mount_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(mount_title)

        mount_row = QHBoxLayout()
        mount_row.addWidget(QLabel("Device:"))
        self.mount_device_input = QLineEdit()
        self.mount_device_input.setPlaceholderText("e.g. /dev/sdb1")
        mount_row.addWidget(self.mount_device_input, 1)
        mount_row.addWidget(QLabel("Mount point:"))
        self.mount_point_input = QLineEdit()
        self.mount_point_input.setPlaceholderText("e.g. /data")
        mount_row.addWidget(self.mount_point_input, 1)
        btn_mount = QPushButton("Mount")
        btn_mount.clicked.connect(self.run_mount_filesystem)
        mount_row.addWidget(btn_mount)
        layout.addLayout(mount_row)

        mount_opts_row = QHBoxLayout()
        mount_opts_row.addWidget(QLabel("Filesystem type (optional):"))
        self.mount_fstype_input = QLineEdit()
        self.mount_fstype_input.setPlaceholderText("e.g. ext4, xfs, nfs")
        self.mount_fstype_input.setMaximumWidth(110)
        mount_opts_row.addWidget(self.mount_fstype_input)
        mount_opts_row.addWidget(QLabel("Options (optional):"))
        self.mount_options_input = QLineEdit()
        self.mount_options_input.setPlaceholderText("e.g. ro,noatime")
        mount_opts_row.addWidget(self.mount_options_input, 1)
        layout.addLayout(mount_opts_row)

        unmount_row = QHBoxLayout()
        unmount_row.addWidget(QLabel("Mount point or device:"))
        self.unmount_target_input = QLineEdit()
        unmount_row.addWidget(self.unmount_target_input, 1)
        self.unmount_force_check = QCheckBox("Force")
        unmount_row.addWidget(self.unmount_force_check)
        btn_unmount = QPushButton("Unmount")
        btn_unmount.clicked.connect(self.run_unmount_filesystem)
        unmount_row.addWidget(btn_unmount)
        layout.addLayout(unmount_row)

        layout.addSpacing(14)

        resize_title = QLabel("Resize and Repair")
        resize_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(resize_title)

        resize_hint = QLabel(
            "Filesystem type is auto-detected (ext2/3/4, xfs, btrfs) and the matching "
            "tool is called automatically - xfs and btrfs can only grow, and only while "
            "mounted; for those, target the mount point rather than the device."
        )
        theme.style_hint_label(resize_hint)
        resize_hint.setWordWrap(True)
        layout.addWidget(resize_hint)

        resize_row = QHBoxLayout()
        resize_row.addWidget(QLabel("Device or mount point:"))
        self.resize_target_input = QLineEdit()
        resize_row.addWidget(self.resize_target_input, 1)
        resize_row.addWidget(QLabel("New size (optional):"))
        self.resize_size_input = QLineEdit()
        self.resize_size_input.setPlaceholderText("e.g. 10G, +5G, blank = fill device")
        resize_row.addWidget(self.resize_size_input, 1)
        btn_resize = QPushButton("Resize Filesystem")
        btn_resize.clicked.connect(self.run_resize_filesystem)
        resize_row.addWidget(btn_resize)
        layout.addLayout(resize_row)

        repair_row = QHBoxLayout()
        repair_row.addWidget(QLabel("Device:"))
        self.repair_device_input = QLineEdit()
        self.repair_device_input.setPlaceholderText("e.g. /dev/sdb1")
        repair_row.addWidget(self.repair_device_input, 1)
        self.repair_auto_yes_check = QCheckBox("Auto-confirm repairs (-y)")
        self.repair_auto_yes_check.setChecked(True)
        repair_row.addWidget(self.repair_auto_yes_check)
        btn_repair = QPushButton("Repair Filesystem")
        btn_repair.clicked.connect(self.run_repair_filesystem)
        repair_row.addWidget(btn_repair)
        layout.addLayout(repair_row)

        repair_hint = QLabel("Refuses to run if the device is currently mounted - unmount it first.")
        theme.style_hint_label(repair_hint)
        layout.addWidget(repair_hint)

        layout.addSpacing(14)

        usage_title = QLabel("Disk Usage")
        usage_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(usage_title)

        usage_row = QHBoxLayout()
        btn_disk_usage = QPushButton("Check Disk Usage")
        btn_disk_usage.clicked.connect(self.run_disk_usage)
        usage_row.addWidget(btn_disk_usage)
        usage_row.addWidget(QLabel("Path:"))
        self.large_files_path_input = QLineEdit("/")
        usage_row.addWidget(self.large_files_path_input, 1)
        usage_row.addWidget(QLabel("Top N:"))
        self.large_files_top_n_input = QLineEdit("20")
        self.large_files_top_n_input.setMaximumWidth(50)
        usage_row.addWidget(self.large_files_top_n_input)
        btn_large_files = QPushButton("Find Large Files")
        btn_large_files.clicked.connect(self.run_find_large_files)
        usage_row.addWidget(btn_large_files)
        layout.addLayout(usage_row)

        layout.addStretch()
        return panel

    def _build_fstab_quota_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        fstab_title = QLabel("/etc/fstab")
        fstab_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(fstab_title)

        fstab_show_row = QHBoxLayout()
        btn_show_fstab = QPushButton("Show /etc/fstab")
        btn_show_fstab.clicked.connect(self.run_show_fstab)
        fstab_show_row.addWidget(btn_show_fstab)
        fstab_show_row.addStretch()
        layout.addLayout(fstab_show_row)

        fstab_hint = QLabel("Add/Remove both back up /etc/fstab first (timestamped copy alongside it).")
        theme.style_hint_label(fstab_hint)
        layout.addWidget(fstab_hint)

        fstab_add_row1 = QHBoxLayout()
        fstab_add_row1.addWidget(QLabel("Device:"))
        self.fstab_device_input = QLineEdit()
        self.fstab_device_input.setPlaceholderText("e.g. /dev/sdb1 or UUID=...")
        fstab_add_row1.addWidget(self.fstab_device_input, 1)
        fstab_add_row1.addWidget(QLabel("Mount point:"))
        self.fstab_mount_point_input = QLineEdit()
        fstab_add_row1.addWidget(self.fstab_mount_point_input, 1)
        fstab_add_row1.addWidget(QLabel("Type:"))
        self.fstab_fstype_input = QLineEdit()
        self.fstab_fstype_input.setPlaceholderText("e.g. ext4")
        self.fstab_fstype_input.setMaximumWidth(80)
        fstab_add_row1.addWidget(self.fstab_fstype_input)
        layout.addLayout(fstab_add_row1)

        fstab_add_row2 = QHBoxLayout()
        fstab_add_row2.addWidget(QLabel("Options:"))
        self.fstab_options_input = QLineEdit("defaults")
        fstab_add_row2.addWidget(self.fstab_options_input, 1)
        fstab_add_row2.addWidget(QLabel("Dump:"))
        self.fstab_dump_input = QLineEdit("0")
        self.fstab_dump_input.setMaximumWidth(40)
        fstab_add_row2.addWidget(self.fstab_dump_input)
        fstab_add_row2.addWidget(QLabel("Pass:"))
        self.fstab_pass_input = QLineEdit("0")
        self.fstab_pass_input.setMaximumWidth(40)
        fstab_add_row2.addWidget(self.fstab_pass_input)
        btn_add_fstab = QPushButton("Add fstab Entry")
        btn_add_fstab.clicked.connect(self.run_add_fstab_entry)
        fstab_add_row2.addWidget(btn_add_fstab)
        layout.addLayout(fstab_add_row2)

        fstab_remove_row = QHBoxLayout()
        fstab_remove_row.addWidget(QLabel("Mount point to remove:"))
        self.fstab_remove_mount_point_input = QLineEdit()
        fstab_remove_row.addWidget(self.fstab_remove_mount_point_input, 1)
        btn_remove_fstab = QPushButton("Remove fstab Entry")
        btn_remove_fstab.clicked.connect(self.run_remove_fstab_entry)
        fstab_remove_row.addWidget(btn_remove_fstab)
        layout.addLayout(fstab_remove_row)

        layout.addSpacing(14)

        quota_title = QLabel("Quotas")
        quota_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(quota_title)

        quota_enable_row = QHBoxLayout()
        quota_enable_row.addWidget(QLabel("Mount point:"))
        self.quota_enable_mount_point_input = QLineEdit()
        quota_enable_row.addWidget(self.quota_enable_mount_point_input, 1)
        btn_enable_quotas = QPushButton("Enable Quotas")
        btn_enable_quotas.clicked.connect(self.run_enable_quotas)
        quota_enable_row.addWidget(btn_enable_quotas)
        quota_enable_row.addWidget(QLabel("Mount point (blank = all):"))
        self.quota_show_mount_point_input = QLineEdit()
        quota_enable_row.addWidget(self.quota_show_mount_point_input, 1)
        btn_show_quotas = QPushButton("Show Quotas")
        btn_show_quotas.clicked.connect(self.run_show_quotas)
        quota_enable_row.addWidget(btn_show_quotas)
        layout.addLayout(quota_enable_row)

        quota_hint = QLabel(
            "Enable Quotas requires the filesystem already mounted with usrquota/grpquota "
            "in /etc/fstab and remounted."
        )
        theme.style_hint_label(quota_hint)
        quota_hint.setWordWrap(True)
        layout.addWidget(quota_hint)

        quota_set_row1 = QHBoxLayout()
        quota_set_row1.addWidget(QLabel("Username:"))
        self.quota_username_input = QLineEdit()
        quota_set_row1.addWidget(self.quota_username_input, 1)
        quota_set_row1.addWidget(QLabel("Mount point:"))
        self.quota_set_mount_point_input = QLineEdit()
        quota_set_row1.addWidget(self.quota_set_mount_point_input, 1)
        layout.addLayout(quota_set_row1)

        quota_set_row2 = QHBoxLayout()
        quota_set_row2.addWidget(QLabel("Block soft (1K):"))
        self.quota_block_soft_input = QLineEdit("0")
        quota_set_row2.addWidget(self.quota_block_soft_input)
        quota_set_row2.addWidget(QLabel("Block hard (1K):"))
        self.quota_block_hard_input = QLineEdit("0")
        quota_set_row2.addWidget(self.quota_block_hard_input)
        quota_set_row2.addWidget(QLabel("Inode soft:"))
        self.quota_inode_soft_input = QLineEdit("0")
        quota_set_row2.addWidget(self.quota_inode_soft_input)
        quota_set_row2.addWidget(QLabel("Inode hard:"))
        self.quota_inode_hard_input = QLineEdit("0")
        quota_set_row2.addWidget(self.quota_inode_hard_input)
        btn_set_quota = QPushButton("Set User Quota")
        btn_set_quota.clicked.connect(self.run_set_user_quota)
        quota_set_row2.addWidget(btn_set_quota)
        layout.addLayout(quota_set_row2)

        quota_set_hint = QLabel("0 means unlimited for a block or inode soft/hard pair.")
        theme.style_hint_label(quota_set_hint)
        layout.addWidget(quota_set_hint)

        layout.addStretch()
        return panel

    def _build_archive_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        archive_title = QLabel("Archive (tar)")
        archive_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(archive_title)

        create_archive_row = QHBoxLayout()
        create_archive_row.addWidget(QLabel("Source path:"))
        self.archive_source_input = QLineEdit()
        self.archive_source_input.setPlaceholderText("file or directory")
        create_archive_row.addWidget(self.archive_source_input, 1)
        create_archive_row.addWidget(QLabel("Archive path:"))
        self.archive_path_input = QLineEdit()
        self.archive_path_input.setPlaceholderText("e.g. /backups/data.tar.gz")
        create_archive_row.addWidget(self.archive_path_input, 1)
        create_archive_row.addWidget(QLabel("Compression:"))
        self.archive_compression_combo = QComboBox()
        self.archive_compression_combo.addItems(["gzip", "bzip2", "xz", "none"])
        create_archive_row.addWidget(self.archive_compression_combo)
        btn_create_archive = QPushButton("Create Archive")
        btn_create_archive.clicked.connect(self.run_create_archive)
        create_archive_row.addWidget(btn_create_archive)
        layout.addLayout(create_archive_row)

        extract_row = QHBoxLayout()
        extract_row.addWidget(QLabel("Archive path:"))
        self.extract_archive_input = QLineEdit()
        extract_row.addWidget(self.extract_archive_input, 1)
        extract_row.addWidget(QLabel("Destination directory:"))
        self.extract_dest_input = QLineEdit()
        extract_row.addWidget(self.extract_dest_input, 1)
        btn_extract = QPushButton("Extract Archive")
        btn_extract.clicked.connect(self.run_extract_archive)
        extract_row.addWidget(btn_extract)
        layout.addLayout(extract_row)

        extract_hint = QLabel("Extract auto-detects compression from the archive itself (.tar/.tar.gz/.tar.bz2/.tar.xz).")
        theme.style_hint_label(extract_hint)
        layout.addWidget(extract_hint)

        layout.addSpacing(14)

        compress_title = QLabel("Compress (single file)")
        compress_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(compress_title)

        compress_hint = QLabel(
            "Distinct from Archive above, which bundles many files/a directory into one "
            ".tar first - this compresses one file in place."
        )
        theme.style_hint_label(compress_hint)
        compress_hint.setWordWrap(True)
        layout.addWidget(compress_hint)

        compress_row = QHBoxLayout()
        compress_row.addWidget(QLabel("File path:"))
        self.compress_path_input = QLineEdit()
        compress_row.addWidget(self.compress_path_input, 1)
        compress_row.addWidget(QLabel("Method:"))
        self.compress_method_combo = QComboBox()
        self.compress_method_combo.addItems(["gzip", "bzip2", "xz", "zip"])
        compress_row.addWidget(self.compress_method_combo)
        self.compress_keep_original_check = QCheckBox("Keep original")
        self.compress_keep_original_check.setChecked(True)
        compress_row.addWidget(self.compress_keep_original_check)
        btn_compress = QPushButton("Compress")
        btn_compress.clicked.connect(self.run_compress_file)
        compress_row.addWidget(btn_compress)
        layout.addLayout(compress_row)

        decompress_row = QHBoxLayout()
        decompress_row.addWidget(QLabel("File path:"))
        self.decompress_path_input = QLineEdit()
        self.decompress_path_input.setPlaceholderText("e.g. /data/report.txt.gz")
        decompress_row.addWidget(self.decompress_path_input, 1)
        self.decompress_keep_original_check = QCheckBox("Keep original")
        self.decompress_keep_original_check.setChecked(True)
        decompress_row.addWidget(self.decompress_keep_original_check)
        btn_decompress = QPushButton("Decompress")
        btn_decompress.clicked.connect(self.run_decompress_file)
        decompress_row.addWidget(btn_decompress)
        layout.addLayout(decompress_row)

        decompress_hint = QLabel("Method is auto-detected from the filename extension (.gz/.bz2/.xz/.zip).")
        theme.style_hint_label(decompress_hint)
        layout.addWidget(decompress_hint)

        layout.addStretch()
        return panel

    def _build_results_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        self.fs_status = QLabel("Pick an action above to run it on all checked hosts.")
        self.fs_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        layout.addWidget(self.fs_status)

        self.fs_tabs = QTabWidget()
        self.fs_tabs.setTabsClosable(True)
        self.fs_tabs.tabCloseRequested.connect(self._close_fs_tab)
        layout.addWidget(self.fs_tabs)
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
            self.fs_status.setStyleSheet(f"color: {STATUS_ERROR_COLOR};")
            self.fs_status.setText(f"Could not load hosts: {e}")
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
        visible = sum(
            1 for i in range(self.host_list.count())
            if not self.host_list.item(i).isHidden()
        )
        row_h = self.host_list.sizeHintForRow(0) if visible else 22
        if row_h <= 0:
            row_h = 22
        height = row_h * min(visible, 6) + 2 * self.host_list.frameWidth() + 6
        self.host_list.setFixedHeight(max(48, min(height, 160)))

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
    # DIRECTORIES & FILES ACTIONS
    # =========================================================
    def run_create_directory(self):
        try:
            cmd = api.cmd_create_directory(
                self.create_dir_path_input.text(), self.create_dir_mode_input.text(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_fs_command(cmd, "Create Directory")

    def run_remove_directory(self):
        path = self.remove_dir_path_input.text().strip()
        recursive = self.remove_dir_recursive_check.isChecked()
        if recursive:
            confirm = QMessageBox.question(
                self, "Confirm recursive removal",
                f"Recursively remove '{path}' and everything inside it on all checked hosts?\n"
                "This cannot be undone.",
                QMessageBox.Yes | QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                return
        try:
            cmd = api.cmd_remove_directory(path, recursive)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_fs_command(cmd, "Remove Directory")

    def run_copy_file(self):
        try:
            cmd = api.cmd_copy_file(
                self.copy_source_input.text(), self.copy_dest_input.text(),
                self.copy_recursive_check.isChecked(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_fs_command(cmd, "Copy")

    def run_move_file(self):
        try:
            cmd = api.cmd_move_file(self.move_source_input.text(), self.move_dest_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_fs_command(cmd, "Move")

    def run_rename_file(self):
        try:
            cmd = api.cmd_rename_file(self.rename_path_input.text(), self.rename_new_name_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_fs_command(cmd, "Rename")

    # =========================================================
    # PERMISSIONS / OWNERSHIP / LINKS ACTIONS
    # =========================================================
    def run_change_ownership(self):
        path = self.chown_path_input.text().strip()
        recursive = self.chown_recursive_check.isChecked()
        if recursive:
            confirm = QMessageBox.question(
                self, "Confirm recursive ownership change",
                f"Recursively change ownership of '{path}' on all checked hosts?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                return
        try:
            cmd = api.cmd_change_ownership(
                path, self.chown_owner_input.text(), self.chown_group_input.text(), recursive,
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_fs_command(cmd, "Change Ownership")

    def run_change_permissions(self):
        path = self.chmod_path_input.text().strip()
        recursive = self.chmod_recursive_check.isChecked()
        if recursive:
            confirm = QMessageBox.question(
                self, "Confirm recursive permission change",
                f"Recursively change permissions of '{path}' on all checked hosts?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                return
        try:
            cmd = api.cmd_change_permissions(path, self.chmod_mode_input.text(), recursive)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_fs_command(cmd, "Change Permissions")

    def run_set_acl(self):
        try:
            cmd = api.cmd_set_acl(
                self.acl_path_input.text(), self.acl_entries_input.text(),
                self.acl_recursive_check.isChecked(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_fs_command(cmd, "Set ACL")

    def run_show_acl(self):
        try:
            cmd = api.cmd_show_acl(self.acl_path_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_fs_command(cmd, "Show ACL")

    def run_create_symlink(self):
        try:
            cmd = api.cmd_create_symlink(self.symlink_target_input.text(), self.symlink_link_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_fs_command(cmd, "Create Symbolic Link")

    def run_create_hardlink(self):
        try:
            cmd = api.cmd_create_hardlink(self.hardlink_target_input.text(), self.hardlink_link_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_fs_command(cmd, "Create Hard Link")

    # =========================================================
    # MOUNT / FILESYSTEM ACTIONS
    # =========================================================
    def run_mount_filesystem(self):
        try:
            cmd = api.cmd_mount_filesystem(
                self.mount_device_input.text(), self.mount_point_input.text(),
                self.mount_fstype_input.text(), self.mount_options_input.text(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_fs_command(cmd, "Mount Filesystem")

    def run_unmount_filesystem(self):
        target = self.unmount_target_input.text().strip()
        force = self.unmount_force_check.isChecked()
        if force:
            confirm = QMessageBox.question(
                self, "Confirm forced unmount",
                f"Force-unmount '{target}' on all checked hosts even if it's busy?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                return
        try:
            cmd = api.cmd_unmount_filesystem(target, force)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_fs_command(cmd, "Unmount Filesystem")

    def run_resize_filesystem(self):
        try:
            cmd = api.cmd_resize_filesystem(self.resize_target_input.text(), self.resize_size_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_fs_command(cmd, "Resize Filesystem")

    def run_repair_filesystem(self):
        device = self.repair_device_input.text().strip()
        confirm = QMessageBox.question(
            self, "Confirm filesystem repair",
            f"Run fsck against '{device}' on all checked hosts? It must be unmounted first.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            cmd = api.cmd_repair_filesystem(device, self.repair_auto_yes_check.isChecked())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_fs_command(cmd, "Repair Filesystem")

    def run_disk_usage(self):
        self._run_fs_command(api.cmd_disk_usage(), "Check Disk Usage")

    def run_find_large_files(self):
        path = self.large_files_path_input.text().strip() or "/"
        top_n = self.large_files_top_n_input.text().strip() or "20"
        try:
            top_n = int(top_n)
        except ValueError:
            QMessageBox.warning(self, "Invalid input", "Top N must be a whole number.")
            return
        cmd = api.cmd_find_large_files(path, top_n)
        self._run_fs_command(cmd, "Find Large Files")

    # =========================================================
    # FSTAB / QUOTAS ACTIONS
    # =========================================================
    def run_show_fstab(self):
        self._run_fs_command(api.cmd_show_fstab(), "Show /etc/fstab")

    def run_add_fstab_entry(self):
        try:
            dump = int(self.fstab_dump_input.text().strip() or "0")
            pass_num = int(self.fstab_pass_input.text().strip() or "0")
        except ValueError:
            QMessageBox.warning(self, "Invalid input", "Dump and Pass must be whole numbers.")
            return
        try:
            cmd = api.cmd_add_fstab_entry(
                self.fstab_device_input.text(), self.fstab_mount_point_input.text(),
                self.fstab_fstype_input.text(), self.fstab_options_input.text(),
                dump, pass_num,
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_fs_command(cmd, "Add fstab Entry")

    def run_remove_fstab_entry(self):
        mount_point = self.fstab_remove_mount_point_input.text().strip()
        confirm = QMessageBox.question(
            self, "Confirm fstab entry removal",
            f"Remove the /etc/fstab entry for '{mount_point}' on all checked hosts?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            cmd = api.cmd_remove_fstab_entry(mount_point)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_fs_command(cmd, "Remove fstab Entry")

    def run_enable_quotas(self):
        try:
            cmd = api.cmd_enable_quotas(self.quota_enable_mount_point_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_fs_command(cmd, "Enable Quotas")

    def run_show_quotas(self):
        self._run_fs_command(api.cmd_show_quotas(self.quota_show_mount_point_input.text()), "Show Quotas")

    def run_set_user_quota(self):
        try:
            block_soft = int(self.quota_block_soft_input.text().strip() or "0")
            block_hard = int(self.quota_block_hard_input.text().strip() or "0")
            inode_soft = int(self.quota_inode_soft_input.text().strip() or "0")
            inode_hard = int(self.quota_inode_hard_input.text().strip() or "0")
        except ValueError:
            QMessageBox.warning(self, "Invalid input", "Quota limits must be whole numbers.")
            return
        try:
            cmd = api.cmd_set_user_quota(
                self.quota_username_input.text(), self.quota_set_mount_point_input.text(),
                block_soft, block_hard, inode_soft, inode_hard,
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_fs_command(cmd, "Set User Quota")

    # =========================================================
    # ARCHIVE / COMPRESS ACTIONS
    # =========================================================
    def run_create_archive(self):
        try:
            cmd = api.cmd_create_archive(
                self.archive_source_input.text(), self.archive_path_input.text(),
                self.archive_compression_combo.currentText(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_fs_command(cmd, "Create Archive")

    def run_extract_archive(self):
        try:
            cmd = api.cmd_extract_archive(self.extract_archive_input.text(), self.extract_dest_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_fs_command(cmd, "Extract Archive")

    def run_compress_file(self):
        try:
            cmd = api.cmd_compress_file(
                self.compress_path_input.text(), self.compress_method_combo.currentText(),
                self.compress_keep_original_check.isChecked(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_fs_command(cmd, "Compress")

    def run_decompress_file(self):
        try:
            cmd = api.cmd_decompress_file(
                self.decompress_path_input.text(), self.decompress_keep_original_check.isChecked(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_fs_command(cmd, "Decompress")

    # =========================================================
    # DISPATCH + RESULTS (same pattern as Network Management /
    # Host Software Management / Service Management)
    # =========================================================
    def _run_fs_command(self, command, label):
        entries = self.checked_entries()

        if not entries:
            QMessageBox.information(self, "No hosts checked", "Check one or more target hosts first.")
            return

        self.last_command_label = label
        self.fs_results = {}
        self.fs_pending = {}
        self.fs_tabs.clear()

        for entry in entries:
            key = _entry_key(entry)
            result = api.run_on_entry(entry, command)

            if result["sync"]:
                self.fs_results[key] = {
                    "label": entry["label"],
                    "stdout": result["stdout"],
                    "stderr": result["stderr"] or result["error"] or "",
                    "code": result["code"],
                    "pending": False,
                }
            elif result["error"]:
                self.fs_results[key] = {
                    "label": entry["label"], "stdout": "", "stderr": result["error"],
                    "code": None, "pending": False,
                }
            else:
                self.fs_results[key] = {
                    "label": entry["label"], "stdout": "", "stderr": "",
                    "code": None, "pending": True,
                }
                self.fs_pending[key] = (entry, result["task_id"])

            self._add_fs_tab(key)

        self.fs_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        self.fs_status.setText(f"Running '{label}' on {len(entries)} host(s)...")

        if self.fs_tabs.count() > 0:
            self.fs_tabs.setCurrentIndex(0)

        if self.fs_pending:
            self.fs_poll_timer.start(FS_POLL_MS)
        else:
            self.fs_status.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
            self.fs_status.setText(f"'{label}' complete.")

    def _status_text(self, data):
        if data["pending"]:
            return "pending..."
        return "ok" if not data["stderr"] else "error"

    def _render_fs_result(self, text_edit, data):
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

    def _close_fs_tab(self, index):
        bar = self.fs_tabs.tabBar()
        key = bar.tabData(index)
        self.fs_tabs.removeTab(index)
        self.fs_results.pop(key, None)
        self.fs_pending.pop(key, None)

    def _add_fs_tab(self, key):
        data = self.fs_results.get(key)
        if not data:
            return
        status = self._status_text(data)

        text_edit = QTextEdit()
        text_edit.setReadOnly(True)
        text_edit.setStyleSheet("font-family: monospace;")
        self._render_fs_result(text_edit, data)

        idx = self.fs_tabs.addTab(text_edit, f"{data['label']}  [{status}]")
        self.fs_tabs.tabBar().setTabData(idx, key)

    def _refresh_fs_tab(self, key):
        bar = self.fs_tabs.tabBar()
        for i in range(self.fs_tabs.count()):
            if bar.tabData(i) != key:
                continue
            data = self.fs_results.get(key)
            if data:
                status = self._status_text(data)
                self.fs_tabs.setTabText(i, f"{data['label']}  [{status}]")
                self._render_fs_result(self.fs_tabs.widget(i), data)
            return

    def _poll_fs(self):
        if not self.fs_pending:
            self.fs_poll_timer.stop()
            return

        done = []

        for key, (entry, task_id) in list(self.fs_pending.items()):
            result = api.poll_entry_result(entry, task_id)
            if result is None:
                continue

            self.fs_results[key] = {
                "label": entry["label"],
                "stdout": result["stdout"],
                "stderr": result["stderr"] or result["error"] or "",
                "code": result["code"],
                "pending": False,
            }
            self._refresh_fs_tab(key)
            done.append(key)

        for key in done:
            del self.fs_pending[key]

        if not self.fs_pending:
            self.fs_poll_timer.stop()
            self.fs_status.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
            self.fs_status.setText("All hosts reported back.")
