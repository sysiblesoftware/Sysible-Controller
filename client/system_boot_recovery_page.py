from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTabWidget, QComboBox,
)

from client import api
from client import theme
from client.tab_sizing import shrink_tabwidget_to_current_page
from client.fleet_tool_page import FleetToolPage


class SystemBootRecoveryPage(FleetToolPage):
    """Boot failure analysis, GRUB configuration, boot targets, initramfs,
    kernel parameters, and kernel cleanup. See client/_api_boot.py."""

    def __init__(self):
        super().__init__("System Boot & Recovery")

    def build_action_tabs(self):
        tabs = QTabWidget()
        tabs.addTab(self._boot_tab(), "Boot && GRUB")
        tabs.addTab(self._kernel_tab(), "Kernels")
        shrink_tabwidget_to_current_page(tabs, cap_height=True)
        return tabs

    @staticmethod
    def _hint(text):
        lbl = QLabel(text)
        theme.style_hint_label(lbl)
        lbl.setWordWrap(True)
        return lbl

    # ---------------- Boot & GRUB ----------------
    def _boot_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        box, g = self.group("Diagnostics")
        row = QHBoxLayout()
        btn = QPushButton("Analyze Boot Failures")
        btn.clicked.connect(lambda: self.run_command(api.cmd_analyze_boot_failures(), "Analyze Boot Failures"))
        row.addWidget(btn)
        btn_show = QPushButton("Show GRUB Config")
        btn_show.clicked.connect(lambda: self.run_command(api.cmd_show_grub_config(), "Show GRUB Config"))
        row.addWidget(btn_show)
        row.addStretch()
        g.addLayout(row)
        layout.addWidget(box)

        box2, g2 = self.group("GRUB Settings")
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Default entry (index or title):"))
        self.grub_default = QLineEdit()
        self.grub_default.setPlaceholderText("e.g. 0")
        row2.addWidget(self.grub_default, 1)
        b1 = QPushButton("Set Default")
        b1.clicked.connect(lambda: self.run_with("Set GRUB Default", lambda: api.cmd_set_grub_default(self.grub_default.text())))
        row2.addWidget(b1)
        row2.addWidget(QLabel("Timeout (s):"))
        self.grub_timeout = QLineEdit()
        self.grub_timeout.setMaximumWidth(60)
        row2.addWidget(self.grub_timeout)
        b2 = QPushButton("Set Timeout")
        b2.clicked.connect(lambda: self.run_with("Set GRUB Timeout", lambda: api.cmd_set_grub_timeout(self.grub_timeout.text())))
        row2.addWidget(b2)
        g2.addLayout(row2)
        row3 = QHBoxLayout()
        b3 = QPushButton("Rebuild GRUB")
        b3.clicked.connect(lambda: self.run_command(api.cmd_rebuild_grub(), "Rebuild GRUB"))
        row3.addWidget(b3)
        row3.addStretch()
        g2.addLayout(row3)
        g2.addWidget(self._hint("Changes are written to /etc/default/grub and grub.cfg is rebuilt; they take effect on the next boot."))
        layout.addWidget(box2)

        box3, g3 = self.group("Recovery Boot Target")
        row4 = QHBoxLayout()
        row4.addWidget(QLabel("Default target for next boot:"))
        self.boot_target = QComboBox()
        self.boot_target.addItems(["multi-user", "graphical", "rescue", "emergency"])
        row4.addWidget(self.boot_target)
        b4 = QPushButton("Set Boot Target")
        b4.clicked.connect(lambda: self.run_with("Set Boot Target", lambda: api.cmd_set_boot_target(self.boot_target.currentText())))
        row4.addWidget(b4)
        row4.addStretch()
        g3.addLayout(row4)
        g3.addWidget(self._hint("rescue = single-user maintenance shell; emergency = most minimal shell. Set the target, then reboot the host to enter it. Remember to set it back to multi-user/graphical afterward."))
        layout.addWidget(box3)

        layout.addStretch()
        return panel

    # ---------------- Kernels ----------------
    def _kernel_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        box, g = self.group("Kernel Parameters")
        row = QHBoxLayout()
        row.addWidget(QLabel("GRUB_CMDLINE_LINUX:"))
        self.kernel_cmdline = QLineEdit()
        self.kernel_cmdline.setPlaceholderText("e.g. quiet splash console=ttyS0,115200")
        row.addWidget(self.kernel_cmdline, 1)
        b = QPushButton("Apply & Rebuild")
        b.clicked.connect(lambda: self.run_with("Set Kernel Parameters", lambda: api.cmd_set_kernel_cmdline(self.kernel_cmdline.text())))
        row.addWidget(b)
        g.addLayout(row)
        g.addWidget(self._hint("Replaces the GRUB_CMDLINE_LINUX line and rebuilds grub.cfg. Effective on next boot."))
        layout.addWidget(box)

        box2, g2 = self.group("initramfs")
        row2 = QHBoxLayout()
        b2 = QPushButton("Regenerate initramfs")
        b2.clicked.connect(lambda: self.run_command(api.cmd_regenerate_initramfs(), "Regenerate initramfs"))
        row2.addWidget(b2)
        row2.addStretch()
        g2.addLayout(row2)
        g2.addWidget(self._hint("Rebuilds the initramfs for installed kernels (dracut / update-initramfs / mkinitcpio, whichever the host uses)."))
        layout.addWidget(box2)

        box3, g3 = self.group("Manage Kernels")
        row3 = QHBoxLayout()
        b3 = QPushButton("List Kernels")
        b3.clicked.connect(lambda: self.run_command(api.cmd_list_kernels(), "List Kernels"))
        row3.addWidget(b3)
        row3.addWidget(QLabel("Keep newest:"))
        self.kernel_keep = QLineEdit("2")
        self.kernel_keep.setMaximumWidth(50)
        row3.addWidget(self.kernel_keep)
        b4 = QPushButton("Remove Old Kernels")
        b4.clicked.connect(lambda: self.run_with("Remove Old Kernels", lambda: api.cmd_remove_old_kernels(self.kernel_keep.text())))
        row3.addWidget(b4)
        row3.addStretch()
        g3.addLayout(row3)
        g3.addWidget(self._hint("The currently running kernel is always kept. Frees /boot space taken by superseded kernels."))
        layout.addWidget(box3)

        layout.addStretch()
        return panel
