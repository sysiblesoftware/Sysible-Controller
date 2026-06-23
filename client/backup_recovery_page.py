from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTabWidget,
)

from client import api
from client import theme
from client.tab_sizing import shrink_tabwidget_to_current_page
from client.fleet_tool_page import FleetToolPage


class BackupRecoveryPage(FleetToolPage):
    """File backups/restore/verify, scheduled backups, LVM snapshots,
    deleted-file recovery guidance, and a read-only DR drill - dispatched
    to whichever hosts are checked. See client/_api_backup.py."""

    def __init__(self):
        super().__init__("Backup & Recovery")

    def build_action_tabs(self):
        tabs = QTabWidget()
        tabs.addTab(self._files_tab(), "Files")
        tabs.addTab(self._snapshots_tab(), "Snapshots")
        tabs.addTab(self._schedule_dr_tab(), "Schedule && DR")
        shrink_tabwidget_to_current_page(tabs, cap_height=True)
        return tabs

    @staticmethod
    def _hint(text):
        lbl = QLabel(text)
        theme.style_hint_label(lbl)
        lbl.setWordWrap(True)
        return lbl

    # ---------------- Files ----------------
    def _files_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        box, g = self.group("Back Up Files")
        row = QHBoxLayout()
        row.addWidget(QLabel("Source path:"))
        self.backup_source = QLineEdit()
        self.backup_source.setPlaceholderText("e.g. /etc or /var/www")
        row.addWidget(self.backup_source, 1)
        row.addWidget(QLabel("Destination dir:"))
        self.backup_dest = QLineEdit()
        self.backup_dest.setPlaceholderText("e.g. /var/backups")
        row.addWidget(self.backup_dest, 1)
        btn = QPushButton("Back Up")
        btn.clicked.connect(self.run_backup)
        row.addWidget(btn)
        g.addLayout(row)
        g.addWidget(self._hint("Creates a timestamped .tar.gz of the source under the destination directory."))
        layout.addWidget(box)

        box2, g2 = self.group("Restore Files")
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Archive:"))
        self.restore_archive = QLineEdit()
        self.restore_archive.setPlaceholderText("e.g. /var/backups/backup-etc-20240101-020000.tar.gz")
        row2.addWidget(self.restore_archive, 1)
        row2.addWidget(QLabel("Restore into:"))
        self.restore_dest = QLineEdit()
        self.restore_dest.setPlaceholderText("e.g. / or /tmp/restore")
        row2.addWidget(self.restore_dest, 1)
        btn2 = QPushButton("Restore")
        btn2.clicked.connect(self.run_restore)
        row2.addWidget(btn2)
        g2.addLayout(row2)
        layout.addWidget(box2)

        box3, g3 = self.group("Verify Backup Integrity")
        row3 = QHBoxLayout()
        row3.addWidget(QLabel("Archive:"))
        self.verify_archive = QLineEdit()
        self.verify_archive.setPlaceholderText("e.g. /var/backups/backup-etc-...tar.gz")
        row3.addWidget(self.verify_archive, 1)
        btn3 = QPushButton("Verify")
        btn3.clicked.connect(self.run_verify)
        row3.addWidget(btn3)
        g3.addLayout(row3)
        layout.addWidget(box3)

        layout.addStretch()
        return panel

    def run_backup(self):
        self.run_with("File Backup", lambda: api.cmd_backup_files(
            self.backup_source.text(), self.backup_dest.text()))

    def run_restore(self):
        self.run_with("Restore Files", lambda: api.cmd_restore_files(
            self.restore_archive.text(), self.restore_dest.text()))

    def run_verify(self):
        self.run_with("Verify Backup", lambda: api.cmd_verify_backup(self.verify_archive.text()))

    # ---------------- Snapshots ----------------
    def _snapshots_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        box, g = self.group("Create LVM Snapshot")
        row = QHBoxLayout()
        row.addWidget(QLabel("VG:"))
        self.snap_vg = QLineEdit()
        self.snap_vg.setMaximumWidth(120)
        row.addWidget(self.snap_vg)
        row.addWidget(QLabel("LV:"))
        self.snap_lv = QLineEdit()
        self.snap_lv.setMaximumWidth(120)
        row.addWidget(self.snap_lv)
        row.addWidget(QLabel("Snapshot name:"))
        self.snap_name = QLineEdit()
        self.snap_name.setMaximumWidth(140)
        row.addWidget(self.snap_name)
        row.addWidget(QLabel("Size:"))
        self.snap_size = QLineEdit()
        self.snap_size.setPlaceholderText("e.g. 1G")
        self.snap_size.setMaximumWidth(90)
        row.addWidget(self.snap_size)
        btn = QPushButton("Create Snapshot")
        btn.clicked.connect(self.run_create_snapshot)
        row.addWidget(btn)
        g.addLayout(row)
        g.addWidget(self._hint("Requires LVM. The snapshot size must be large enough to hold changes "
                               "to the origin while the snapshot exists."))
        layout.addWidget(box)

        box2, g2 = self.group("Restore From Snapshot")
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("VG:"))
        self.restore_snap_vg = QLineEdit()
        self.restore_snap_vg.setMaximumWidth(120)
        row2.addWidget(self.restore_snap_vg)
        row2.addWidget(QLabel("Snapshot name:"))
        self.restore_snap_name = QLineEdit()
        self.restore_snap_name.setMaximumWidth(160)
        row2.addWidget(self.restore_snap_name)
        btn2 = QPushButton("Merge Snapshot Back")
        btn2.clicked.connect(self.run_restore_snapshot)
        row2.addWidget(btn2)
        row2.addStretch()
        g2.addLayout(row2)
        g2.addWidget(self._hint("Merges the snapshot into its origin. The merge completes when the "
                                "origin is next deactivated (typically at reboot)."))
        layout.addWidget(box2)

        layout.addStretch()
        return panel

    def run_create_snapshot(self):
        self.run_with("Create Snapshot", lambda: api.cmd_create_snapshot(
            self.snap_vg.text(), self.snap_lv.text(), self.snap_name.text(), self.snap_size.text()))

    def run_restore_snapshot(self):
        self.run_with("Restore Snapshot", lambda: api.cmd_restore_snapshot(
            self.restore_snap_vg.text(), self.restore_snap_name.text()))

    # ---------------- Schedule & DR ----------------
    def _schedule_dr_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        box, g = self.group("Configure Backup Schedule")
        row = QHBoxLayout()
        row.addWidget(QLabel("Source:"))
        self.sched_source = QLineEdit()
        self.sched_source.setPlaceholderText("e.g. /etc")
        row.addWidget(self.sched_source, 1)
        row.addWidget(QLabel("Destination:"))
        self.sched_dest = QLineEdit()
        self.sched_dest.setPlaceholderText("e.g. /var/backups")
        row.addWidget(self.sched_dest, 1)
        g.addLayout(row)
        row_b = QHBoxLayout()
        row_b.addWidget(QLabel("Cron schedule:"))
        self.sched_cron = QLineEdit("0 2 * * *")
        self.sched_cron.setMaximumWidth(140)
        row_b.addWidget(self.sched_cron)
        btn = QPushButton("Install Scheduled Backup")
        btn.clicked.connect(self.run_schedule)
        row_b.addWidget(btn)
        row_b.addStretch()
        g.addLayout(row_b)
        g.addWidget(self._hint("Installs /etc/cron.d/sysible-backup running a timestamped tar.gz on the "
                               "given schedule (default: daily at 02:00)."))
        layout.addWidget(box)

        box2, g2 = self.group("Recover Deleted Files")
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Device / path:"))
        self.recover_device = QLineEdit()
        self.recover_device.setPlaceholderText("e.g. /dev/sdb1")
        row2.addWidget(self.recover_device, 1)
        btn2 = QPushButton("Check Recovery Options")
        btn2.clicked.connect(self.run_recover)
        row2.addWidget(btn2)
        g2.addLayout(row2)
        g2.addWidget(self._hint("Reports which recovery tools are present and the safe procedure - "
                                "it does not auto-run recovery, which must be done with the filesystem unmounted."))
        layout.addWidget(box2)

        box3, g3 = self.group("Test Disaster Recovery")
        row3 = QHBoxLayout()
        row3.addWidget(QLabel("Backup directory:"))
        self.dr_dest = QLineEdit()
        self.dr_dest.setPlaceholderText("e.g. /var/backups")
        row3.addWidget(self.dr_dest, 1)
        btn3 = QPushButton("Run DR Drill")
        btn3.clicked.connect(self.run_dr_test)
        row3.addWidget(btn3)
        g3.addLayout(row3)
        g3.addWidget(self._hint("Read-only: confirms a recent backup exists, reports its age, and verifies "
                                "its integrity - changes nothing."))
        layout.addWidget(box3)

        layout.addStretch()
        return panel

    def run_schedule(self):
        self.run_with("Configure Backup Schedule", lambda: api.cmd_configure_backup_schedule(
            self.sched_source.text(), self.sched_dest.text(), self.sched_cron.text()))

    def run_recover(self):
        self.run_with("Recover Deleted Files", lambda: api.cmd_recover_deleted(self.recover_device.text()))

    def run_dr_test(self):
        self.run_with("Disaster Recovery Test", lambda: api.cmd_test_disaster_recovery(self.dr_dest.text()))
