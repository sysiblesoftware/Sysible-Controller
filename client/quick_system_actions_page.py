import time

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTabWidget,
    QMessageBox, QListWidget,
)

from client import api
from client import theme
from client.tab_sizing import shrink_tabwidget_to_current_page
from client.fleet_tool_page import FleetToolPage
from client.fleet_dashboard_page import _Worker


def _list_services_on(entry, running):
    """List service unit names on one host (read-only, no sudo). Returns
    {"names": [...]} or {"error": "..."}. Runs off the GUI thread via _Worker."""
    cmd = api.cmd_list_running_services() if running else api.cmd_list_services()
    out = api.run_on_entry(entry, cmd, needs_sudo=False)
    if out.get("error"):
        return {"error": out["error"]}
    if out.get("sync"):
        text = out.get("stdout") or ""
    else:
        tid = out.get("task_id")
        if tid is None:
            return {"error": "failed to queue task"}
        text = None
        deadline = time.time() + 30
        while time.time() < deadline:
            r = api.poll_entry_result(entry, tid)
            if r is not None:
                text = r.get("stdout") or ""
                break
            time.sleep(1.0)
        if text is None:
            return {"error": "timed out waiting for host"}
    names = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.lower().startswith("systemctl not"):
            continue
        names.append(ln.split()[0])
    return {"names": sorted(set(n for n in names if n))}


class QuickSystemActionsPage(FleetToolPage):
    """One-click common remediations against the checked hosts: reboot / power
    off, restart a service (NetworkManager, SSH, time sync, or any by name),
    flush DNS, clear failed units, and reload systemd. Every button delegates
    to an existing cmd_* builder — nothing new runs on the host that couldn't
    already be run from Service Management or a raw command."""

    def __init__(self):
        super().__init__("Quick System Actions")

    def build_action_tabs(self):
        tabs = QTabWidget()
        tabs.addTab(self._common_tab(), "Common Fixes")
        tabs.addTab(self._power_tab(), "Power")
        shrink_tabwidget_to_current_page(tabs, cap_height=True)
        return tabs

    @staticmethod
    def _hint(text):
        lbl = QLabel(text)
        theme.style_hint_label(lbl)
        lbl.setWordWrap(True)
        return lbl

    # ---------------- Common Fixes ----------------
    def _common_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        box, g = self.group("Service (by name)")
        row = QHBoxLayout()
        row.addWidget(QLabel("Service name:"))
        self.svc_input = QLineEdit()
        self.svc_input.setPlaceholderText("e.g. nginx, docker, postgresql")
        row.addWidget(self.svc_input, 1)
        b = QPushButton("Restart")
        b.clicked.connect(lambda: self.run_with(
            "Restart Service", lambda: api.cmd_service_restart(self.svc_input.text())))
        row.addWidget(b)
        b_start = QPushButton("Start")
        b_start.clicked.connect(lambda: self.run_with(
            "Start Service", lambda: api.cmd_service_start(self.svc_input.text())))
        row.addWidget(b_start)
        b_stop = QPushButton("Stop")
        b_stop.clicked.connect(lambda: self.run_with(
            "Stop Service", lambda: api.cmd_service_stop(self.svc_input.text())))
        row.addWidget(b_stop)
        g.addLayout(row)

        # Service browser: list running/installed services on the first checked
        # host, click one to fill the field (parity with the web console).
        browse = QHBoxLayout()
        self._svc_list_running_btn = QPushButton("List Running Services")
        self._svc_list_running_btn.clicked.connect(lambda: self._list_services(True))
        browse.addWidget(self._svc_list_running_btn)
        self._svc_list_installed_btn = QPushButton("List Installed Services")
        self._svc_list_installed_btn.clicked.connect(lambda: self._list_services(False))
        browse.addWidget(self._svc_list_installed_btn)
        browse.addStretch()
        g.addLayout(browse)
        self.svc_list = QListWidget()
        self.svc_list.setMaximumHeight(150)
        self.svc_list.itemClicked.connect(lambda it: self.svc_input.setText(it.text()))
        self.svc_input.textChanged.connect(self._filter_svc_list)
        g.addWidget(self.svc_list)
        g.addWidget(self._hint("List services on the first checked host, click one to select it, then "
                               "Restart / Start / Stop — runs on every checked host. Typing filters the list."))
        layout.addWidget(box)

        box2, g2 = self.group("Common services")
        row2 = QHBoxLayout()
        for label, fn in [
            ("Restart NetworkManager", lambda: api.cmd_service_restart("NetworkManager")),
            ("Flush DNS Cache", api.cmd_flush_dns),
            ("Restart SSH Server", api.cmd_restart_ssh),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=False, l=label, f=fn: self.run_command(f(), l))
            row2.addWidget(btn)
        row2.addStretch()
        g2.addLayout(row2)
        row2b = QHBoxLayout()
        for label, fn in [
            ("Restart Time Sync", api.cmd_restart_timesync),
            ("Sync Clock Now", api.cmd_sync_time_now),
            ("Restart Docker", lambda: api.cmd_service_restart("docker")),
            ("Restart Sysible Agent", api.cmd_restart_agent),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=False, l=label, f=fn: self.run_command(f(), l))
            row2b.addWidget(btn)
        row2b.addStretch()
        g2.addLayout(row2b)
        layout.addWidget(box2)

        box4, g4 = self.group("Free up resources")
        row4 = QHBoxLayout()
        for label, fn in [
            ("Free Memory (drop caches)", api.cmd_drop_caches),
            ("Clean Package Cache", api.cmd_clean_package_cache),
            ("Vacuum Journal Logs", lambda: api.cmd_vacuum_journal(7)),
            ("Trim Filesystems", api.cmd_fstrim),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=False, l=label, f=fn: self.run_command(f(), l))
            row4.addWidget(btn)
        row4.addStretch()
        g4.addLayout(row4)
        g4.addWidget(self._hint("Reclaim memory and disk: drop clean caches (no data lost), clear the "
                                "package download cache, shrink the journal to 7 days, and trim SSDs."))
        layout.addWidget(box4)

        box3, g3 = self.group("Systemd housekeeping")
        row3 = QHBoxLayout()
        b_rf = QPushButton("Clear Failed Units")
        b_rf.clicked.connect(lambda: self.run_command(api.cmd_reset_failed_units(), "Clear Failed Units"))
        row3.addWidget(b_rf)
        b_dr = QPushButton("Reload systemd")
        b_dr.clicked.connect(lambda: self.run_command(api.cmd_daemon_reload(), "Reload systemd"))
        row3.addWidget(b_dr)
        row3.addStretch()
        g3.addLayout(row3)
        g3.addWidget(self._hint("Clear Failed Units wipes stale 'failed' markers (it does not start "
                                "anything). Reload systemd re-reads unit files after they're edited."))
        layout.addWidget(box3)

        layout.addStretch()
        return panel

    # ---------------- Service browser ----------------
    def _list_services(self, running):
        entries = self.checked_entries()
        if not entries:
            QMessageBox.information(self, "No hosts checked", "Check a host first — services are read from one host.")
            return
        entry = entries[0]
        self._svc_list_running_btn.setEnabled(False)
        self._svc_list_installed_btn.setEnabled(False)
        self.svc_list.clear()
        self.svc_list.addItem(f"Listing services on {entry['label']}…")
        self._svc_worker = _Worker(lambda e=entry, r=running: _list_services_on(e, r))
        self._svc_worker.done.connect(self._on_services_listed)
        self._svc_worker.fail.connect(self._on_services_failed)
        self._svc_worker.start()

    def _on_services_listed(self, res):
        self._svc_list_running_btn.setEnabled(True)
        self._svc_list_installed_btn.setEnabled(True)
        self.svc_list.clear()
        if res.get("error"):
            self.svc_list.addItem(f"Error: {res['error']}")
            self._all_svcs = []
            return
        self._all_svcs = res.get("names") or []
        self._filter_svc_list(self.svc_input.text())
        if not self._all_svcs:
            self.svc_list.addItem("No services found.")

    def _on_services_failed(self, msg):
        self._svc_list_running_btn.setEnabled(True)
        self._svc_list_installed_btn.setEnabled(True)
        self.svc_list.clear()
        self.svc_list.addItem(f"Error: {msg}")
        self._all_svcs = []

    def _filter_svc_list(self, text):
        names = getattr(self, "_all_svcs", None)
        if names is None:
            return
        text = (text or "").lower()
        self.svc_list.clear()
        for n in names:
            if text in n.lower():
                self.svc_list.addItem(n)

    # ---------------- Power ----------------
    def _power_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        box, g = self.group("Power (careful — affects the whole host)")
        row = QHBoxLayout()
        b_reboot = QPushButton("Reboot Host(s)")
        b_reboot.clicked.connect(lambda: self._confirm_power(
            api.cmd_reboot_host(), "Reboot Host",
            "Reboot every checked host now?"))
        row.addWidget(b_reboot)
        b_off = QPushButton("Power Off Host(s)")
        b_off.clicked.connect(lambda: self._confirm_power(
            api.cmd_poweroff_host(), "Power Off Host",
            "Power off every checked host now?\n\nThey will NOT come back until "
            "powered on out-of-band."))
        row.addWidget(b_off)
        row.addStretch()
        g.addLayout(row)
        g.addWidget(self._hint("Reboot schedules 'shutdown -r +0'; power off schedules "
                               "'shutdown -P +0'. The host reports the command was accepted before "
                               "it goes down."))
        layout.addWidget(box)

        layout.addStretch()
        return panel

    def _confirm_power(self, command, label, prompt):
        entries = self.checked_entries()
        if not entries:
            QMessageBox.information(self, "No hosts checked", "Check one or more target hosts first.")
            return
        names = ", ".join(e["label"] for e in entries[:8])
        if len(entries) > 8:
            names += f", … (+{len(entries) - 8} more)"
        resp = QMessageBox.question(
            self, label, f"{prompt}\n\nHosts: {names}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if resp == QMessageBox.Yes:
            self.run_command(command, label)
