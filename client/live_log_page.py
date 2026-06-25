"""
Live Activity & Logs - a dashboard window (under System Administration)
that shows, live:
  * Activity: a human-readable, attributed feed of actions the controller
    carried out - "<admin> <description> on <host>" (e.g. "cdovbish changed
    user-tester's password on prod1"). Polled incrementally by id.
  * Controller Log: the tail of the sysible-backend service journal.

Both auto-refresh on a timer; refresh can be paused.
"""
import time

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QCheckBox,
    QTabWidget, QListWidget, QListWidgetItem, QTextEdit, QSpinBox,
    QDialog, QApplication,
)
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor

from client import api, theme
from client.branding import make_page_header, center_on_screen

_REFRESH_MS = 3000


class ActivityDetailDialog(QDialog):
    """Pop-out showing one activity entry in full - who/what/where/when and,
    crucially, the COMPLETE command or script that was run (the feed only
    shows a short summary, since a script can be many lines long)."""

    def __init__(self, entry, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Activity Detail")
        self.resize(720, 480)
        self._centered = False

        v = QVBoxLayout(self)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(8)

        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.get("timestamp", 0)))
        user = entry.get("username") or "(unknown)"
        host = entry.get("host") or "—"
        desc = entry.get("description") or ""

        header = QLabel(f"<b>{desc}</b>")
        header.setWordWrap(True)
        v.addWidget(header)
        meta = QLabel(f"{user}  ·  {host}  ·  {ts}")
        theme.style_hint_label(meta)
        v.addWidget(meta)

        v.addWidget(QLabel("Command / script run:"))
        self.cmd_view = QTextEdit()
        self.cmd_view.setReadOnly(True)
        self.cmd_view.setStyleSheet("font-family: monospace;")
        self.cmd_view.setLineWrapMode(QTextEdit.NoWrap)
        cmd = (entry.get("command") or "").strip()
        self.cmd_view.setPlainText(cmd or "(no command recorded for this action)")
        v.addWidget(self.cmd_view, 1)

        buttons = QHBoxLayout()
        copy_btn = QPushButton("Copy command")
        copy_btn.setEnabled(bool(cmd))
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(cmd))
        buttons.addWidget(copy_btn)
        buttons.addStretch()
        close_btn = QPushButton("Close")
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        buttons.addWidget(close_btn)
        v.addLayout(buttons)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._centered:
            self._centered = True
            center_on_screen(self)


class LiveLogPage(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Live Activity & Logs")
        self.resize(820, 620)

        self._last_id = 0  # highest activity id seen, for incremental polling

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.addLayout(make_page_header("Live Activity & Logs"))

        controls = QHBoxLayout()
        self.auto = QCheckBox("Auto-refresh")
        self.auto.setChecked(True)
        self.auto.stateChanged.connect(self._toggle_auto)
        controls.addWidget(self.auto)
        controls.addWidget(QLabel("every"))
        self.interval = QSpinBox()
        self.interval.setRange(1, 60)
        self.interval.setValue(_REFRESH_MS // 1000)
        self.interval.setSuffix(" s")
        self.interval.valueChanged.connect(self._restart_timer)
        controls.addWidget(self.interval)
        refresh_btn = QPushButton("Refresh now")
        refresh_btn.clicked.connect(self.refresh)
        controls.addWidget(refresh_btn)
        controls.addStretch()
        clear_btn = QPushButton("Clear view")
        clear_btn.clicked.connect(self._clear_view)
        controls.addWidget(clear_btn)
        layout.addLayout(controls)

        self.tabs = QTabWidget()

        # --- Activity feed ---
        self.activity = QListWidget()
        self.activity.setStyleSheet("font-family: monospace;")
        # Double-click an entry to see the full command/script in a pop-out
        # (the row itself only shows a short summary).
        self.activity.itemActivated.connect(self._show_entry_details)
        self.activity.itemDoubleClicked.connect(self._show_entry_details)
        activity_tab = QWidget()
        at = QVBoxLayout(activity_tab)
        at.setContentsMargins(0, 0, 0, 0)
        at.setSpacing(4)
        at.addWidget(self.activity, 1)
        activity_hint = QLabel("Double-click an entry to view the full command or script that ran.")
        theme.style_hint_label(activity_hint)
        at.addWidget(activity_hint)
        self.tabs.addTab(activity_tab, "Activity")

        # --- Controller log ---
        self.controller_log = QTextEdit()
        self.controller_log.setReadOnly(True)
        self.controller_log.setStyleSheet("font-family: monospace; font-size: 11px;")
        self.controller_log.setLineWrapMode(QTextEdit.NoWrap)
        self.tabs.addTab(self.controller_log, "Controller Log")

        layout.addWidget(self.tabs, 1)

        self.status = QLabel("")
        theme.style_hint_label(self.status)
        layout.addWidget(self.status)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(_REFRESH_MS)
        self.refresh(initial=True)

    # ------------------------------------------------------------------
    def _toggle_auto(self):
        if self.auto.isChecked():
            self._restart_timer()
        else:
            self._timer.stop()

    def _restart_timer(self):
        self._timer.start(self.interval.value() * 1000)

    def _clear_view(self):
        self.activity.clear()
        self.controller_log.clear()
        self._last_id = 0

    def refresh(self, initial=False):
        self._load_activity()
        # The controller log is heavier; refresh it on the timer too but it's
        # fine to always re-pull the tail.
        self._load_controller_log()

    def _load_activity(self):
        try:
            entries = api.get_activity_log(limit=200, since_id=self._last_id)
        except Exception as e:
            self.status.setText(f"Activity feed unavailable: {e}")
            return
        # Endpoint returns newest-first; insert oldest-first at the top so the
        # newest ends up at the very top and order stays correct.
        for e in reversed(entries):
            self._add_activity_row(e)
            self._last_id = max(self._last_id, e.get("id", 0))
        if entries:
            self.status.setText(f"{self.activity.count()} event(s) — last update {time.strftime('%H:%M:%S')}")

    def _add_activity_row(self, e):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.get("timestamp", 0)))
        user = e.get("username") or "(unknown)"
        host = e.get("host") or ""
        desc = e.get("description") or ""
        on = f"  on {host}" if host else ""
        item = QListWidgetItem(f"{ts}   {user}  —  {desc}{on}")
        # Keep the full entry (incl. the complete command) on the item so a
        # double-click can show it in full - see _show_entry_details.
        item.setData(Qt.UserRole, e)
        cmd = (e.get("command") or "").strip()
        if cmd:
            # Hover shows a short single-line command; multi-line/scripts just
            # invite the double-click instead of dumping code into a tooltip.
            if "\n" not in cmd and len(cmd) <= 200 and not cmd.lstrip().startswith(
                    ("import ", "python", "#!", "cat <<", "base64", "{")):
                item.setToolTip(cmd)
            else:
                item.setToolTip("Double-click to view the full command/script.")
        # Tint destructive-looking actions.
        low = desc.lower()
        if any(k in low for k in ("delet", "remov", "lock", "kill", "reboot", "power off", "disable")):
            item.setForeground(QColor("#f0a0a0"))
        self.activity.insertItem(0, item)
        # Cap the view so it doesn't grow without bound.
        while self.activity.count() > 1000:
            self.activity.takeItem(self.activity.count() - 1)

    def _show_entry_details(self, item):
        entry = item.data(Qt.UserRole)
        if not entry:
            return
        ActivityDetailDialog(entry, self).exec()

    def _load_controller_log(self):
        try:
            text = api.get_controller_log(lines=400)
        except Exception as e:
            self.controller_log.setPlainText(f"Controller log unavailable: {e}")
            return
        at_bottom = (self.controller_log.verticalScrollBar().value()
                     >= self.controller_log.verticalScrollBar().maximum() - 4)
        self.controller_log.setPlainText(text)
        if at_bottom:
            sb = self.controller_log.verticalScrollBar()
            sb.setValue(sb.maximum())

    def closeEvent(self, event):
        self._timer.stop()
        super().closeEvent(event)
