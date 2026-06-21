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
STORAGE_POLL_MS = 2000


def _entry_key(entry):
    """Hashable identity for a merged-host entry (api.list_merged_hosts()) -
    agent host_ids and SSH host names live in separate namespaces, so the
    kind has to be part of the key."""
    return (entry["kind"], entry["id"])


class StorageAdministrationPage(QWidget):
    """
    Storage Administration against a *merged* host list (agent-enrolled
    hosts AND SSH-enrolled hosts) - partitions, raw mkfs formatting, LVM
    (physical volumes / volume groups / logical volumes), software RAID,
    swap, and whole-disk health/add/remove, all dispatched to whichever
    hosts are checked, same as every other System Administration tool.

    Everything below the filesystem layer that File System Management
    doesn't cover - see client/_api_storage.py for the command builders.
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Storage Administration")
        self.resize(1150, 860)

        self.storage_results = {}   # entry_key -> {label, stdout, stderr, code, pending}
        self.storage_pending = {}   # entry_key -> (entry, task_id)
        self.last_command_label = None
        self._collapsed_envs = set()

        main = QVBoxLayout()
        self.setLayout(main)

        main.addLayout(make_page_header("Storage Administration"))

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
        # ACTIONS (tabbed - 16 distinct features grouped logically)
        # =========================================================
        action_tabs = QTabWidget()
        action_tabs.addTab(self._build_disks_tab(), "Disks")
        action_tabs.addTab(self._build_partitions_tab(), "Partitions")
        action_tabs.addTab(self._build_format_tab(), "Format Filesystems")
        action_tabs.addTab(self._build_lvm_tab(), "LVM")
        action_tabs.addTab(self._build_raid_tab(), "RAID")
        action_tabs.addTab(self._build_swap_tab(), "Swap")
        main.addWidget(action_tabs)

        # =========================================================
        # RESULTS (stretchy - see file_system_management_page.py for why)
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

        self.storage_poll_timer = QTimer()
        self.storage_poll_timer.timeout.connect(self._poll_storage)

        bus.host_removed.connect(self.load_hosts)

    # =========================================================
    # ACTION PANEL BUILDERS (filled in below)
    # =========================================================
    def _build_disks_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        overview_title = QLabel("Overview and Health")
        overview_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(overview_title)

        overview_row = QHBoxLayout()
        btn_list_disks = QPushButton("List Disks")
        btn_list_disks.clicked.connect(self.run_list_disks)
        overview_row.addWidget(btn_list_disks)
        btn_monitor_health = QPushButton("Monitor Disk Health")
        btn_monitor_health.clicked.connect(self.run_monitor_disk_health)
        overview_row.addWidget(btn_monitor_health)
        overview_row.addStretch()
        layout.addLayout(overview_row)

        smart_row = QHBoxLayout()
        smart_row.addWidget(QLabel("Device:"))
        self.smart_device_input = QLineEdit()
        self.smart_device_input.setPlaceholderText("e.g. /dev/sda")
        smart_row.addWidget(self.smart_device_input, 1)
        btn_smart = QPushButton("Check SMART Status")
        btn_smart.clicked.connect(self.run_check_smart_status)
        smart_row.addWidget(btn_smart)
        layout.addLayout(smart_row)

        layout.addSpacing(14)

        addremove_title = QLabel("Add and Remove Disks")
        addremove_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(addremove_title)

        rescan_hint = QLabel(
            "After physically attaching a new SCSI/SATA/virtio disk, rescan to make it "
            "visible without rebooting. NVMe disks need no rescan - the kernel detects "
            "them automatically."
        )
        theme.style_hint_label(rescan_hint)
        rescan_hint.setWordWrap(True)
        layout.addWidget(rescan_hint)

        rescan_row = QHBoxLayout()
        btn_rescan = QPushButton("Rescan / Detect New Disk")
        btn_rescan.clicked.connect(self.run_rescan_disks)
        rescan_row.addWidget(btn_rescan)
        rescan_row.addStretch()
        layout.addLayout(rescan_row)

        remove_row = QHBoxLayout()
        remove_row.addWidget(QLabel("Device:"))
        self.remove_disk_device_input = QLineEdit()
        self.remove_disk_device_input.setPlaceholderText("e.g. /dev/sdb")
        remove_row.addWidget(self.remove_disk_device_input, 1)
        btn_remove_disk = QPushButton("Remove Disk")
        btn_remove_disk.clicked.connect(self.run_remove_disk)
        remove_row.addWidget(btn_remove_disk)
        layout.addLayout(remove_row)

        remove_hint = QLabel(
            "Refuses if the disk is mounted, an active LVM physical volume, or a RAID "
            "member - clear those first. Once it reports success, the disk is safe to "
            "physically detach."
        )
        theme.style_hint_label(remove_hint)
        remove_hint.setWordWrap(True)
        layout.addWidget(remove_hint)

        layout.addStretch()
        return panel

    def _build_partitions_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        list_title = QLabel("Partition Table")
        list_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(list_title)

        list_row = QHBoxLayout()
        list_row.addWidget(QLabel("Device (blank = overview of all disks):"))
        self.list_partitions_device_input = QLineEdit()
        self.list_partitions_device_input.setPlaceholderText("e.g. /dev/sdb")
        list_row.addWidget(self.list_partitions_device_input, 1)
        btn_list_partitions = QPushButton("List Partitions")
        btn_list_partitions.clicked.connect(self.run_list_partitions)
        list_row.addWidget(btn_list_partitions)
        layout.addLayout(list_row)

        layout.addSpacing(14)

        table_title = QLabel("Partition Table")
        table_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(table_title)

        table_hint = QLabel("DESTROYS any existing partition table (and the data it describes) on the device.")
        theme.style_hint_label(table_hint)
        layout.addWidget(table_hint)

        table_row = QHBoxLayout()
        table_row.addWidget(QLabel("Device:"))
        self.create_table_device_input = QLineEdit()
        self.create_table_device_input.setPlaceholderText("e.g. /dev/sdb")
        table_row.addWidget(self.create_table_device_input, 1)
        table_row.addWidget(QLabel("Label type:"))
        self.create_table_label_combo = QComboBox()
        self.create_table_label_combo.addItems(["gpt", "msdos"])
        table_row.addWidget(self.create_table_label_combo)
        btn_create_table = QPushButton("Create Partition Table")
        btn_create_table.clicked.connect(self.run_create_partition_table)
        table_row.addWidget(btn_create_table)
        layout.addLayout(table_row)

        layout.addSpacing(14)

        create_title = QLabel("Create Partition")
        create_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(create_title)

        create_row1 = QHBoxLayout()
        create_row1.addWidget(QLabel("Device:"))
        self.create_part_device_input = QLineEdit()
        self.create_part_device_input.setPlaceholderText("e.g. /dev/sdb")
        create_row1.addWidget(self.create_part_device_input, 1)
        create_row1.addWidget(QLabel("Filesystem hint:"))
        self.create_part_fstype_input = QLineEdit("ext4")
        self.create_part_fstype_input.setMaximumWidth(80)
        create_row1.addWidget(self.create_part_fstype_input)
        layout.addLayout(create_row1)

        create_row2 = QHBoxLayout()
        create_row2.addWidget(QLabel("Start:"))
        self.create_part_start_input = QLineEdit("0%")
        self.create_part_start_input.setMaximumWidth(80)
        create_row2.addWidget(self.create_part_start_input)
        create_row2.addWidget(QLabel("End:"))
        self.create_part_end_input = QLineEdit("100%")
        self.create_part_end_input.setMaximumWidth(80)
        create_row2.addWidget(self.create_part_end_input)
        btn_create_part = QPushButton("Create Partition")
        btn_create_part.clicked.connect(self.run_create_partition)
        create_row2.addWidget(btn_create_part)
        create_row2.addStretch()
        layout.addLayout(create_row2)

        create_hint = QLabel("Start/End accept parted's forms - percentages (0%, 100%) or absolute sizes (1MiB, 512GiB).")
        theme.style_hint_label(create_hint)
        layout.addWidget(create_hint)

        layout.addSpacing(14)

        delresize_title = QLabel("Delete and Resize Partition")
        delresize_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(delresize_title)

        delete_row = QHBoxLayout()
        delete_row.addWidget(QLabel("Device:"))
        self.delete_part_device_input = QLineEdit()
        self.delete_part_device_input.setPlaceholderText("e.g. /dev/sdb")
        delete_row.addWidget(self.delete_part_device_input, 1)
        delete_row.addWidget(QLabel("Partition #:"))
        self.delete_part_number_input = QLineEdit()
        self.delete_part_number_input.setMaximumWidth(50)
        delete_row.addWidget(self.delete_part_number_input)
        btn_delete_part = QPushButton("Delete Partition")
        btn_delete_part.clicked.connect(self.run_delete_partition)
        delete_row.addWidget(btn_delete_part)
        layout.addLayout(delete_row)

        resize_row = QHBoxLayout()
        resize_row.addWidget(QLabel("Device:"))
        self.resize_part_device_input = QLineEdit()
        self.resize_part_device_input.setPlaceholderText("e.g. /dev/sdb")
        resize_row.addWidget(self.resize_part_device_input, 1)
        resize_row.addWidget(QLabel("Partition #:"))
        self.resize_part_number_input = QLineEdit()
        self.resize_part_number_input.setMaximumWidth(50)
        resize_row.addWidget(self.resize_part_number_input)
        resize_row.addWidget(QLabel("New end:"))
        self.resize_part_end_input = QLineEdit()
        self.resize_part_end_input.setPlaceholderText("e.g. 100% or 50GiB")
        self.resize_part_end_input.setMaximumWidth(110)
        resize_row.addWidget(self.resize_part_end_input)
        btn_resize_part = QPushButton("Resize Partition")
        btn_resize_part.clicked.connect(self.run_resize_partition)
        resize_row.addWidget(btn_resize_part)
        layout.addLayout(resize_row)

        resize_hint = QLabel(
            "Resizes the partition table entry only - run Resize Filesystem (File System "
            "Management) afterward to grow/shrink the filesystem inside it to match."
        )
        theme.style_hint_label(resize_hint)
        resize_hint.setWordWrap(True)
        layout.addWidget(resize_hint)

        layout.addStretch()
        return panel

    def _build_format_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        title = QLabel("Format Filesystem")
        title.setStyleSheet("font-weight: bold;")
        layout.addWidget(title)

        hint = QLabel("Creates a brand-new filesystem on the device, destroying whatever was there before.")
        theme.style_hint_label(hint)
        layout.addWidget(hint)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Device:"))
        self.format_device_input = QLineEdit()
        self.format_device_input.setPlaceholderText("e.g. /dev/sdb1")
        row1.addWidget(self.format_device_input, 1)
        row1.addWidget(QLabel("Type:"))
        self.format_fstype_combo = QComboBox()
        self.format_fstype_combo.addItems(["ext4", "ext3", "ext2", "xfs", "btrfs", "vfat", "ntfs", "swap"])
        row1.addWidget(self.format_fstype_combo)
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Label (optional):"))
        self.format_label_input = QLineEdit()
        row2.addWidget(self.format_label_input, 1)
        self.format_force_check = QCheckBox("Force")
        self.format_force_check.setChecked(True)
        row2.addWidget(self.format_force_check)
        btn_format = QPushButton("Format Filesystem")
        btn_format.clicked.connect(self.run_format_filesystem)
        row2.addWidget(btn_format)
        layout.addLayout(row2)

        layout.addStretch()
        return panel

    def _build_lvm_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        pv_title = QLabel("Physical Volumes")
        pv_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(pv_title)

        pv_row = QHBoxLayout()
        pv_row.addWidget(QLabel("Device(s):"))
        self.create_pv_devices_input = QLineEdit()
        self.create_pv_devices_input.setPlaceholderText("e.g. /dev/sdb1 /dev/sdc1 (space-separated)")
        pv_row.addWidget(self.create_pv_devices_input, 1)
        btn_create_pv = QPushButton("Create Physical Volume")
        btn_create_pv.clicked.connect(self.run_create_physical_volume)
        pv_row.addWidget(btn_create_pv)
        btn_list_pv = QPushButton("List Physical Volumes")
        btn_list_pv.clicked.connect(self.run_list_physical_volumes)
        pv_row.addWidget(btn_list_pv)
        layout.addLayout(pv_row)

        layout.addSpacing(14)

        vg_title = QLabel("Volume Groups")
        vg_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(vg_title)

        vg_create_row = QHBoxLayout()
        vg_create_row.addWidget(QLabel("VG name:"))
        self.create_vg_name_input = QLineEdit()
        self.create_vg_name_input.setMaximumWidth(120)
        vg_create_row.addWidget(self.create_vg_name_input)
        vg_create_row.addWidget(QLabel("Device(s):"))
        self.create_vg_devices_input = QLineEdit()
        self.create_vg_devices_input.setPlaceholderText("e.g. /dev/sdb1 (space-separated)")
        vg_create_row.addWidget(self.create_vg_devices_input, 1)
        btn_create_vg = QPushButton("Create Volume Group")
        btn_create_vg.clicked.connect(self.run_create_volume_group)
        vg_create_row.addWidget(btn_create_vg)
        btn_list_vg = QPushButton("List Volume Groups")
        btn_list_vg.clicked.connect(self.run_list_volume_groups)
        vg_create_row.addWidget(btn_list_vg)
        layout.addLayout(vg_create_row)

        vg_resize_row = QHBoxLayout()
        vg_resize_row.addWidget(QLabel("VG name:"))
        self.resize_vg_name_input = QLineEdit()
        self.resize_vg_name_input.setMaximumWidth(120)
        vg_resize_row.addWidget(self.resize_vg_name_input)
        vg_resize_row.addWidget(QLabel("Device(s):"))
        self.resize_vg_devices_input = QLineEdit()
        self.resize_vg_devices_input.setPlaceholderText("space-separated")
        vg_resize_row.addWidget(self.resize_vg_devices_input, 1)
        btn_extend_vg = QPushButton("Extend Volume Group")
        btn_extend_vg.clicked.connect(self.run_extend_volume_group)
        vg_resize_row.addWidget(btn_extend_vg)
        btn_reduce_vg = QPushButton("Reduce Volume Group")
        btn_reduce_vg.clicked.connect(self.run_reduce_volume_group)
        vg_resize_row.addWidget(btn_reduce_vg)
        layout.addLayout(vg_resize_row)

        vg_hint = QLabel("Reduce requires each device already be empty of logical-volume data (pvmove it off first).")
        theme.style_hint_label(vg_hint)
        vg_hint.setWordWrap(True)
        layout.addWidget(vg_hint)

        layout.addSpacing(14)

        lv_title = QLabel("Logical Volumes")
        lv_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(lv_title)

        lv_create_row = QHBoxLayout()
        lv_create_row.addWidget(QLabel("VG name:"))
        self.create_lv_vg_input = QLineEdit()
        self.create_lv_vg_input.setMaximumWidth(110)
        lv_create_row.addWidget(self.create_lv_vg_input)
        lv_create_row.addWidget(QLabel("LV name:"))
        self.create_lv_name_input = QLineEdit()
        self.create_lv_name_input.setMaximumWidth(110)
        lv_create_row.addWidget(self.create_lv_name_input)
        lv_create_row.addWidget(QLabel("Size:"))
        self.create_lv_size_input = QLineEdit()
        self.create_lv_size_input.setPlaceholderText("e.g. 20G or 100%FREE")
        lv_create_row.addWidget(self.create_lv_size_input, 1)
        btn_create_lv = QPushButton("Create Logical Volume")
        btn_create_lv.clicked.connect(self.run_create_logical_volume)
        lv_create_row.addWidget(btn_create_lv)
        btn_list_lv = QPushButton("List Logical Volumes")
        btn_list_lv.clicked.connect(self.run_list_logical_volumes)
        lv_create_row.addWidget(btn_list_lv)
        layout.addLayout(lv_create_row)

        lv_extend_row = QHBoxLayout()
        lv_extend_row.addWidget(QLabel("VG name:"))
        self.extend_lv_vg_input = QLineEdit()
        self.extend_lv_vg_input.setMaximumWidth(110)
        lv_extend_row.addWidget(self.extend_lv_vg_input)
        lv_extend_row.addWidget(QLabel("LV name:"))
        self.extend_lv_name_input = QLineEdit()
        self.extend_lv_name_input.setMaximumWidth(110)
        lv_extend_row.addWidget(self.extend_lv_name_input)
        lv_extend_row.addWidget(QLabel("New size:"))
        self.extend_lv_size_input = QLineEdit()
        self.extend_lv_size_input.setPlaceholderText("e.g. 20G or +5G")
        lv_extend_row.addWidget(self.extend_lv_size_input, 1)
        self.extend_lv_resize_fs_check = QCheckBox("Resize filesystem too")
        self.extend_lv_resize_fs_check.setChecked(True)
        lv_extend_row.addWidget(self.extend_lv_resize_fs_check)
        btn_extend_lv = QPushButton("Extend Logical Volume")
        btn_extend_lv.clicked.connect(self.run_extend_logical_volume)
        lv_extend_row.addWidget(btn_extend_lv)
        layout.addLayout(lv_extend_row)

        lv_reduce_row = QHBoxLayout()
        lv_reduce_row.addWidget(QLabel("VG name:"))
        self.reduce_lv_vg_input = QLineEdit()
        self.reduce_lv_vg_input.setMaximumWidth(110)
        lv_reduce_row.addWidget(self.reduce_lv_vg_input)
        lv_reduce_row.addWidget(QLabel("LV name:"))
        self.reduce_lv_name_input = QLineEdit()
        self.reduce_lv_name_input.setMaximumWidth(110)
        lv_reduce_row.addWidget(self.reduce_lv_name_input)
        lv_reduce_row.addWidget(QLabel("New size:"))
        self.reduce_lv_size_input = QLineEdit()
        self.reduce_lv_size_input.setPlaceholderText("e.g. 5G")
        lv_reduce_row.addWidget(self.reduce_lv_size_input, 1)
        btn_reduce_lv = QPushButton("Reduce Logical Volume")
        btn_reduce_lv.clicked.connect(self.run_reduce_logical_volume)
        lv_reduce_row.addWidget(btn_reduce_lv)
        layout.addLayout(lv_reduce_row)

        lv_reduce_hint = QLabel(
            "ext2/3/4 is unmounted, fsck'd, and shrunk automatically. XFS cannot be shrunk "
            "(an XFS limitation) - back up, recreate a smaller LV, and restore instead."
        )
        theme.style_hint_label(lv_reduce_hint)
        lv_reduce_hint.setWordWrap(True)
        layout.addWidget(lv_reduce_hint)

        layout.addStretch()
        return panel

    def _build_raid_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        status_title = QLabel("RAID Arrays")
        status_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(status_title)

        status_row = QHBoxLayout()
        btn_list_raid = QPushButton("List RAID Arrays")
        btn_list_raid.clicked.connect(self.run_list_raid_arrays)
        status_row.addWidget(btn_list_raid)
        status_row.addWidget(QLabel("RAID device (optional):"))
        self.raid_status_device_input = QLineEdit()
        self.raid_status_device_input.setPlaceholderText("e.g. /dev/md0")
        status_row.addWidget(self.raid_status_device_input, 1)
        btn_raid_status = QPushButton("RAID Status")
        btn_raid_status.clicked.connect(self.run_raid_status)
        status_row.addWidget(btn_raid_status)
        layout.addLayout(status_row)

        layout.addSpacing(14)

        create_title = QLabel("Configure RAID")
        create_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(create_title)

        create_row1 = QHBoxLayout()
        create_row1.addWidget(QLabel("RAID device:"))
        self.create_raid_device_input = QLineEdit()
        self.create_raid_device_input.setPlaceholderText("e.g. /dev/md0")
        create_row1.addWidget(self.create_raid_device_input, 1)
        create_row1.addWidget(QLabel("Level:"))
        self.create_raid_level_combo = QComboBox()
        self.create_raid_level_combo.addItems(["0", "1", "4", "5", "6", "10"])
        self.create_raid_level_combo.setCurrentText("1")
        create_row1.addWidget(self.create_raid_level_combo)
        layout.addLayout(create_row1)

        create_row2 = QHBoxLayout()
        create_row2.addWidget(QLabel("Member devices:"))
        self.create_raid_devices_input = QLineEdit()
        self.create_raid_devices_input.setPlaceholderText("e.g. /dev/sdb /dev/sdc (space-separated)")
        create_row2.addWidget(self.create_raid_devices_input, 1)
        btn_create_raid = QPushButton("Create RAID Array")
        btn_create_raid.clicked.connect(self.run_create_raid_array)
        create_row2.addWidget(btn_create_raid)
        layout.addLayout(create_row2)

        layout.addSpacing(14)

        replace_title = QLabel("Replace Failed Disk")
        replace_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(replace_title)

        replace_row1 = QHBoxLayout()
        replace_row1.addWidget(QLabel("RAID device:"))
        self.replace_raid_device_input = QLineEdit()
        self.replace_raid_device_input.setPlaceholderText("e.g. /dev/md0")
        replace_row1.addWidget(self.replace_raid_device_input, 1)
        replace_row1.addWidget(QLabel("Failed device:"))
        self.replace_failed_device_input = QLineEdit()
        self.replace_failed_device_input.setPlaceholderText("e.g. /dev/sdb")
        replace_row1.addWidget(self.replace_failed_device_input, 1)
        layout.addLayout(replace_row1)

        replace_row2 = QHBoxLayout()
        replace_row2.addWidget(QLabel("Replacement device:"))
        self.replace_new_device_input = QLineEdit()
        self.replace_new_device_input.setPlaceholderText("e.g. /dev/sdd")
        replace_row2.addWidget(self.replace_new_device_input, 1)
        btn_replace_disk = QPushButton("Replace Failed Disk")
        btn_replace_disk.clicked.connect(self.run_replace_failed_disk)
        replace_row2.addWidget(btn_replace_disk)
        layout.addLayout(replace_row2)

        layout.addStretch()
        return panel

    def _build_swap_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        list_row = QHBoxLayout()
        btn_list_swap = QPushButton("List Swap")
        btn_list_swap.clicked.connect(self.run_list_swap)
        list_row.addWidget(btn_list_swap)
        list_row.addStretch()
        layout.addLayout(list_row)

        layout.addSpacing(14)

        file_title = QLabel("Swap File")
        file_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(file_title)

        file_row = QHBoxLayout()
        file_row.addWidget(QLabel("Path:"))
        self.swap_file_path_input = QLineEdit()
        self.swap_file_path_input.setPlaceholderText("e.g. /swapfile")
        file_row.addWidget(self.swap_file_path_input, 1)
        file_row.addWidget(QLabel("Size (MB):"))
        self.swap_file_size_input = QLineEdit()
        self.swap_file_size_input.setPlaceholderText("e.g. 2048")
        self.swap_file_size_input.setMaximumWidth(80)
        file_row.addWidget(self.swap_file_size_input)
        self.swap_file_persist_check = QCheckBox("Persist (add to /etc/fstab)")
        self.swap_file_persist_check.setChecked(True)
        file_row.addWidget(self.swap_file_persist_check)
        btn_create_swap_file = QPushButton("Create Swap File")
        btn_create_swap_file.clicked.connect(self.run_create_swap_file)
        file_row.addWidget(btn_create_swap_file)
        layout.addLayout(file_row)

        layout.addSpacing(14)

        part_title = QLabel("Swap Partition")
        part_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(part_title)

        part_row = QHBoxLayout()
        part_row.addWidget(QLabel("Device:"))
        self.swap_part_device_input = QLineEdit()
        self.swap_part_device_input.setPlaceholderText("e.g. /dev/sdb2")
        part_row.addWidget(self.swap_part_device_input, 1)
        self.swap_part_persist_check = QCheckBox("Persist (add to /etc/fstab)")
        self.swap_part_persist_check.setChecked(True)
        part_row.addWidget(self.swap_part_persist_check)
        btn_create_swap_part = QPushButton("Create Swap Partition")
        btn_create_swap_part.clicked.connect(self.run_create_swap_partition)
        part_row.addWidget(btn_create_swap_part)
        layout.addLayout(part_row)

        layout.addSpacing(14)

        disable_title = QLabel("Disable Swap")
        disable_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(disable_title)

        disable_row = QHBoxLayout()
        disable_row.addWidget(QLabel("Swap file or device:"))
        self.swap_disable_target_input = QLineEdit()
        disable_row.addWidget(self.swap_disable_target_input, 1)
        self.swap_disable_remove_fstab_check = QCheckBox("Also remove matching /etc/fstab entry")
        disable_row.addWidget(self.swap_disable_remove_fstab_check)
        btn_disable_swap = QPushButton("Disable Swap")
        btn_disable_swap.clicked.connect(self.run_disable_swap)
        disable_row.addWidget(btn_disable_swap)
        layout.addLayout(disable_row)

        layout.addStretch()
        return panel

    def _build_results_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        self.storage_status = QLabel("Pick an action above to run it on all checked hosts.")
        self.storage_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        layout.addWidget(self.storage_status)

        self.storage_tabs = QTabWidget()
        self.storage_tabs.setTabsClosable(True)
        self.storage_tabs.tabCloseRequested.connect(self._close_storage_tab)
        layout.addWidget(self.storage_tabs)
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
            self.storage_status.setStyleSheet(f"color: {STATUS_ERROR_COLOR};")
            self.storage_status.setText(f"Could not load hosts: {e}")
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
    # DISKS ACTIONS
    # =========================================================
    def run_list_disks(self):
        self._run_storage_command(api.cmd_list_disks(), "List Disks")

    def run_monitor_disk_health(self):
        self._run_storage_command(api.cmd_monitor_disk_health(), "Monitor Disk Health")

    def run_check_smart_status(self):
        try:
            cmd = api.cmd_check_smart_status(self.smart_device_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_storage_command(cmd, "Check SMART Status")

    def run_rescan_disks(self):
        self._run_storage_command(api.cmd_rescan_disks(), "Rescan / Detect New Disk")

    def run_remove_disk(self):
        device = self.remove_disk_device_input.text().strip()
        confirm = QMessageBox.question(
            self, "Confirm disk removal",
            f"Offline '{device}' on all checked hosts so it's safe to physically remove?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            cmd = api.cmd_remove_disk(device)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_storage_command(cmd, "Remove Disk")

    # =========================================================
    # PARTITIONS ACTIONS
    # =========================================================
    def run_list_partitions(self):
        try:
            cmd = api.cmd_list_partitions(self.list_partitions_device_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_storage_command(cmd, "List Partitions")

    def run_create_partition_table(self):
        device = self.create_table_device_input.text().strip()
        label_type = self.create_table_label_combo.currentText()
        confirm = QMessageBox.question(
            self, "Confirm partition table creation",
            f"Create a new {label_type} partition table on '{device}' on all checked hosts? "
            "This DESTROYS any existing partition table and the data it describes.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            cmd = api.cmd_create_partition_table(device, label_type)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_storage_command(cmd, "Create Partition Table")

    def run_create_partition(self):
        try:
            cmd = api.cmd_create_partition(
                self.create_part_device_input.text(), self.create_part_fstype_input.text(),
                self.create_part_start_input.text(), self.create_part_end_input.text(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_storage_command(cmd, "Create Partition")

    def run_delete_partition(self):
        device = self.delete_part_device_input.text().strip()
        part_number = self.delete_part_number_input.text().strip()
        confirm = QMessageBox.question(
            self, "Confirm partition deletion",
            f"Delete partition {part_number} on '{device}' on all checked hosts? This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            cmd = api.cmd_delete_partition(device, part_number)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_storage_command(cmd, "Delete Partition")

    def run_resize_partition(self):
        try:
            cmd = api.cmd_resize_partition(
                self.resize_part_device_input.text(), self.resize_part_number_input.text(),
                self.resize_part_end_input.text(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_storage_command(cmd, "Resize Partition")

    # =========================================================
    # FORMAT FILESYSTEM ACTIONS
    # =========================================================
    def run_format_filesystem(self):
        device = self.format_device_input.text().strip()
        fs_type = self.format_fstype_combo.currentText()
        confirm = QMessageBox.question(
            self, "Confirm format",
            f"Format '{device}' as {fs_type} on all checked hosts? This DESTROYS any data currently there.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            cmd = api.cmd_format_filesystem(
                device, fs_type, self.format_label_input.text(), self.format_force_check.isChecked(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_storage_command(cmd, "Format Filesystem")

    # =========================================================
    # LVM ACTIONS
    # =========================================================
    def run_create_physical_volume(self):
        try:
            cmd = api.cmd_create_physical_volume(self.create_pv_devices_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_storage_command(cmd, "Create Physical Volume")

    def run_list_physical_volumes(self):
        self._run_storage_command(api.cmd_list_physical_volumes(), "List Physical Volumes")

    def run_create_volume_group(self):
        try:
            cmd = api.cmd_create_volume_group(self.create_vg_name_input.text(), self.create_vg_devices_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_storage_command(cmd, "Create Volume Group")

    def run_list_volume_groups(self):
        self._run_storage_command(api.cmd_list_volume_groups(), "List Volume Groups")

    def run_extend_volume_group(self):
        try:
            cmd = api.cmd_extend_volume_group(self.resize_vg_name_input.text(), self.resize_vg_devices_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_storage_command(cmd, "Extend Volume Group")

    def run_reduce_volume_group(self):
        vg_name = self.resize_vg_name_input.text().strip()
        confirm = QMessageBox.question(
            self, "Confirm volume group reduction",
            f"Remove the listed device(s) from volume group '{vg_name}' on all checked hosts?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            cmd = api.cmd_reduce_volume_group(vg_name, self.resize_vg_devices_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_storage_command(cmd, "Reduce Volume Group")

    def run_create_logical_volume(self):
        try:
            cmd = api.cmd_create_logical_volume(
                self.create_lv_vg_input.text(), self.create_lv_name_input.text(), self.create_lv_size_input.text(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_storage_command(cmd, "Create Logical Volume")

    def run_list_logical_volumes(self):
        self._run_storage_command(api.cmd_list_logical_volumes(), "List Logical Volumes")

    def run_extend_logical_volume(self):
        try:
            cmd = api.cmd_extend_logical_volume(
                self.extend_lv_vg_input.text(), self.extend_lv_name_input.text(),
                self.extend_lv_size_input.text(), self.extend_lv_resize_fs_check.isChecked(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_storage_command(cmd, "Extend Logical Volume")

    def run_reduce_logical_volume(self):
        vg_name = self.reduce_lv_vg_input.text().strip()
        lv_name = self.reduce_lv_name_input.text().strip()
        confirm = QMessageBox.question(
            self, "Confirm logical volume reduction",
            f"Shrink logical volume '{lv_name}' in volume group '{vg_name}' on all checked hosts? "
            "Make sure the data fits in the new size first.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            cmd = api.cmd_reduce_logical_volume(vg_name, lv_name, self.reduce_lv_size_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_storage_command(cmd, "Reduce Logical Volume")

    # =========================================================
    # RAID ACTIONS
    # =========================================================
    def run_list_raid_arrays(self):
        self._run_storage_command(api.cmd_list_raid_arrays(), "List RAID Arrays")

    def run_raid_status(self):
        self._run_storage_command(api.cmd_raid_status(self.raid_status_device_input.text()), "RAID Status")

    def run_create_raid_array(self):
        raid_device = self.create_raid_device_input.text().strip()
        level = self.create_raid_level_combo.currentText()
        confirm = QMessageBox.question(
            self, "Confirm RAID creation",
            f"Create '{raid_device}' as RAID{level} on all checked hosts? Any data currently on the "
            "member devices will be destroyed.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            cmd = api.cmd_create_raid_array(raid_device, level, self.create_raid_devices_input.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_storage_command(cmd, "Create RAID Array")

    def run_replace_failed_disk(self):
        raid_device = self.replace_raid_device_input.text().strip()
        failed_device = self.replace_failed_device_input.text().strip()
        new_device = self.replace_new_device_input.text().strip()
        confirm = QMessageBox.question(
            self, "Confirm disk replacement",
            f"In '{raid_device}', fail/remove '{failed_device}' and add '{new_device}' on all checked hosts?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            cmd = api.cmd_replace_failed_disk(raid_device, failed_device, new_device)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_storage_command(cmd, "Replace Failed Disk")

    # =========================================================
    # SWAP ACTIONS
    # =========================================================
    def run_list_swap(self):
        self._run_storage_command(api.cmd_list_swap(), "List Swap")

    def run_create_swap_file(self):
        size_text = self.swap_file_size_input.text().strip()
        try:
            size_mb = int(size_text)
        except ValueError:
            QMessageBox.warning(self, "Invalid input", "Size (MB) must be a whole number.")
            return
        try:
            cmd = api.cmd_create_swap_file(
                self.swap_file_path_input.text(), size_mb, self.swap_file_persist_check.isChecked(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_storage_command(cmd, "Create Swap File")

    def run_create_swap_partition(self):
        try:
            cmd = api.cmd_create_swap_partition(
                self.swap_part_device_input.text(), self.swap_part_persist_check.isChecked(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_storage_command(cmd, "Create Swap Partition")

    def run_disable_swap(self):
        target = self.swap_disable_target_input.text().strip()
        confirm = QMessageBox.question(
            self, "Confirm disabling swap",
            f"Deactivate swap on '{target}' on all checked hosts?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            cmd = api.cmd_disable_swap(target, self.swap_disable_remove_fstab_check.isChecked())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_storage_command(cmd, "Disable Swap")

    # =========================================================
    # DISPATCH + RESULTS (same pattern as File System Management /
    # Network Management / Service Management)
    # =========================================================
    def _run_storage_command(self, command, label):
        entries = self.checked_entries()

        if not entries:
            QMessageBox.information(self, "No hosts checked", "Check one or more target hosts first.")
            return

        self.last_command_label = label
        self.storage_results = {}
        self.storage_pending = {}
        self.storage_tabs.clear()

        for entry in entries:
            key = _entry_key(entry)
            result = api.run_on_entry(entry, command)

            if result["sync"]:
                self.storage_results[key] = {
                    "label": entry["label"],
                    "stdout": result["stdout"],
                    "stderr": result["stderr"] or result["error"] or "",
                    "code": result["code"],
                    "pending": False,
                }
            elif result["error"]:
                self.storage_results[key] = {
                    "label": entry["label"], "stdout": "", "stderr": result["error"],
                    "code": None, "pending": False,
                }
            else:
                self.storage_results[key] = {
                    "label": entry["label"], "stdout": "", "stderr": "",
                    "code": None, "pending": True,
                }
                self.storage_pending[key] = (entry, result["task_id"])

            self._add_storage_tab(key)

        self.storage_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        self.storage_status.setText(f"Running '{label}' on {len(entries)} host(s)...")

        if self.storage_tabs.count() > 0:
            self.storage_tabs.setCurrentIndex(0)

        if self.storage_pending:
            self.storage_poll_timer.start(STORAGE_POLL_MS)
        else:
            self.storage_status.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
            self.storage_status.setText(f"'{label}' complete.")

    def _status_text(self, data):
        if data["pending"]:
            return "pending..."
        return "ok" if not data["stderr"] else "error"

    def _render_storage_result(self, text_edit, data):
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

    def _close_storage_tab(self, index):
        bar = self.storage_tabs.tabBar()
        key = bar.tabData(index)
        self.storage_tabs.removeTab(index)
        self.storage_results.pop(key, None)
        self.storage_pending.pop(key, None)

    def _add_storage_tab(self, key):
        data = self.storage_results.get(key)
        if not data:
            return
        status = self._status_text(data)

        text_edit = QTextEdit()
        text_edit.setReadOnly(True)
        text_edit.setStyleSheet("font-family: monospace;")
        self._render_storage_result(text_edit, data)

        idx = self.storage_tabs.addTab(text_edit, f"{data['label']}  [{status}]")
        self.storage_tabs.tabBar().setTabData(idx, key)

    def _refresh_storage_tab(self, key):
        bar = self.storage_tabs.tabBar()
        for i in range(self.storage_tabs.count()):
            if bar.tabData(i) != key:
                continue
            data = self.storage_results.get(key)
            if data:
                status = self._status_text(data)
                self.storage_tabs.setTabText(i, f"{data['label']}  [{status}]")
                self._render_storage_result(self.storage_tabs.widget(i), data)
            return

    def _poll_storage(self):
        if not self.storage_pending:
            self.storage_poll_timer.stop()
            return

        done = []

        for key, (entry, task_id) in list(self.storage_pending.items()):
            result = api.poll_entry_result(entry, task_id)
            if result is None:
                continue

            self.storage_results[key] = {
                "label": entry["label"],
                "stdout": result["stdout"],
                "stderr": result["stderr"] or result["error"] or "",
                "code": result["code"],
                "pending": False,
            }
            self._refresh_storage_tab(key)
            done.append(key)

        for key in done:
            del self.storage_pending[key]

        if not self.storage_pending:
            self.storage_poll_timer.stop()
            self.storage_status.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
            self.storage_status.setText("All hosts reported back.")
