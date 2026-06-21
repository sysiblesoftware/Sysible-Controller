import html
import re

from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout,
    QListWidget, QListWidgetItem, QLabel, QPushButton,
    QLineEdit, QTextEdit, QMessageBox, QSpinBox, QTabWidget,
    QComboBox, QFrame,
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor

from client import api
from client.events import bus
from client.theme import STATUS_NEUTRAL_COLOR, STATUS_SUCCESS_COLOR, STATUS_ERROR_COLOR, STATUS_WARNING_COLOR
from client.branding import make_page_header
from client.collapsible_groups import (
    make_group_header_item, apply_collapse_state, get_collapsed_groups,
    connect_group_toggle, add_collapse_expand_buttons,
)

HOST_REFRESH_MS = 10000
HEALTH_POLL_MS = 2000

# Green for good, red for bad, amber in between - applied to both the
# per-host list rows and the verdict line in the detail panel so the
# health state reads at a glance instead of requiring the actual text
# to be read first.
_STATUS_COLORS = {
    "OK": STATUS_SUCCESS_COLOR,
    "ok": STATUS_SUCCESS_COLOR,
    "WARNING": STATUS_WARNING_COLOR,
    "CRITICAL": STATUS_ERROR_COLOR,
    "error": STATUS_ERROR_COLOR,
    "pending...": STATUS_NEUTRAL_COLOR,
}


def _status_color(status):
    return _STATUS_COLORS.get(status, STATUS_NEUTRAL_COLOR)


_JOURNAL_PRIORITY_CHOICES = ["emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"]


def _entry_key(entry):
    """Hashable identity for a merged-host entry (api.list_merged_hosts()) -
    agent host_ids and SSH host names live in separate namespaces, so the
    kind has to be part of the key."""
    return (entry["kind"], entry["id"])


def _extract_health_verdict(stdout):
    """api.cmd_health_check()'s output leads with a "HEALTH: OK/WARNING/
    CRITICAL" line - pull that out so the host list can show the actual
    verdict (e.g. "[WARNING]") instead of the generic ok/error status
    every other health command gets. Returns None for any other command's
    output, so those fall back to the original ok/error behavior."""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.startswith("HEALTH:"):
            verdict = line.split(":", 1)[1].strip()
            if verdict in ("OK", "WARNING", "CRITICAL"):
                return verdict
    return None


# Tuned to the exact text api.py's health/process/log command builders
# actually produce, not a generic log-output guesser - %{0,3}d-style
# percentage columns only ever show up here on real df rows (ps prints
# its %CPU/%MEM values as bare numbers, no literal "%"), and the bare
# word "failed" only shows up on a real failed-unit row or inside a
# line that's already announced itself OK.
_PCT_RE = re.compile(r"(\d{1,3})%")
_CRITICAL_RE = re.compile(r"(?i)\bcritical\b")
_WARNING_RE = re.compile(r"(?i)\bwarning\b")
_OK_RE = re.compile(r"(?i)\bok\b")
_FAILED_RE = re.compile(r"(?i)\bfailed\b")
_ZOMBIE_FOUND_RE = re.compile(r"(?i)^found \d+ zombie process")

_KNOWN_GOOD_LINES = {
    "no failed services.",
    "no zombie processes found.",
}


def _line_color(line):
    """Pick a highlight color for one line of report output, or None to
    leave it in the default text color. A CRITICAL/WARNING word always
    wins (the verdict line itself, and the per-signal "Disk usage:
    CRITICAL" / "Memory usage: 92% (CRITICAL)" style reason lines),
    then a bare Use%-style percentage (raw `df` rows carry no such
    word), then "failed" outside of a line that's already reported
    itself OK (a raw `systemctl --failed` row, without flagging
    "Failed services: OK (0 failed unit(s))" on a healthy host)."""
    stripped = line.strip()
    lower = stripped.lower()

    if _CRITICAL_RE.search(line):
        return _status_color("CRITICAL")
    if _WARNING_RE.search(line):
        return _status_color("WARNING")
    if lower in _KNOWN_GOOD_LINES or lower.startswith("health: ok"):
        return _status_color("OK")
    if _ZOMBIE_FOUND_RE.match(stripped):
        return _status_color("WARNING")
    if _FAILED_RE.search(line) and not _OK_RE.search(line):
        return _status_color("CRITICAL")

    pct_values = [int(m) for m in _PCT_RE.findall(line)]
    if pct_values:
        worst = max(pct_values)
        if worst >= 90:
            return _status_color("CRITICAL")
        if worst >= 75:
            return _status_color("WARNING")

    return None


# Plain-English rewrites for the handful of raw command-output shapes
# that show up across the board here: a `df -hT` table (Disk Usage,
# and the "-- Raw signals --" section of Host Health Check), `free -h`'s
# Mem:/Swap: rows (Memory & CPU Snapshot), `systemctl --failed`'s unit
# rows (Failed Services, Host Health Check), and the `uptime` one-liner
# (Uptime, Memory & CPU Snapshot, Investigate High Load). Process
# tables (`ps`), `find` listings, and raw log lines are left alone -
# a process table read as a table isn't "terminal-like" in the way a
# raw df/free/uptime dump is, and guessing wrong on those formats would
# do more harm than just leaving them as-is.
_DF_HEADER_TOKENS = ["Filesystem", "Type", "Size", "Used", "Avail", "Use%", "Mounted", "on"]
_FREE_HEADER_TOKENS = ["total", "used", "free", "shared", "buff/cache", "available"]
_UNIT_SUFFIXES = (".service", ".timer", ".socket", ".mount", ".path", ".target", ".device", ".swap", ".scope")
_UPTIME_RE = re.compile(
    r"up\s+(?P<uptime>.+?),\s+(?P<users>\d+)\s+users?,\s+load average:\s*"
    r"(?P<l1>[\d.]+),\s*(?P<l5>[\d.]+),\s*(?P<l15>[\d.]+)"
)


def _parse_df_block(lines, start):
    """lines[start] is a `df -hT` header row (already matched against
    _DF_HEADER_TOKENS by the caller). Parse every row up to the next
    blank line / end of input. Returns (end_index, rows) on a clean
    parse where every row has all 7 columns, or (end_index, None) the
    moment any row doesn't - end_index is the same either way, so the
    caller can skip the block whether or not the parse succeeded,
    falling back to the untouched raw table on a None."""
    rows = []
    ok = True
    i = start + 1
    while i < len(lines) and lines[i].strip() != "":
        parts = lines[i].split()
        if len(parts) < 7 or not parts[5].endswith("%"):
            ok = False
        else:
            fs, ftype, size, used, avail, usepct = parts[0:6]
            mount = " ".join(parts[6:])
            rows.append((fs, ftype, size, used, avail, usepct, mount))
        i += 1
    return i, (rows if ok and rows else None)


def _humanize_free_line(line):
    """Rewrite free -h's `Mem:` / `Swap:` rows into a sentence. Only
    fires on those exact line-starts so it can't misfire on anything
    else in the report."""
    parts = line.split()
    if len(parts) < 4 or parts[0] not in ("Mem:", "Swap:"):
        return None
    label = "Memory" if parts[0] == "Mem:" else "Swap"
    total, used, free = parts[1], parts[2], parts[3]
    sentence = f"{label}: {used} used of {total} total, {free} free"
    if parts[0] == "Mem:" and len(parts) >= 6:
        sentence += f", {parts[-1]} available"
    return sentence


def _humanize_failed_unit_line(line):
    """Rewrite one `systemctl --failed --no-legend` row into a
    sentence. Requires the unit name to end in a real systemd unit
    suffix and the LOAD column to read "loaded" before treating a line
    as a failed-unit row at all, so it can't catch some unrelated line
    that happens to contain the word "failed"."""
    stripped = line.strip().lstrip("●").strip()  # drop a leading bullet, if present
    parts = stripped.split(None, 4)
    if len(parts) < 4:
        return None
    unit, load, active, sub = parts[0], parts[1], parts[2], parts[3]
    if load != "loaded" or not unit.endswith(_UNIT_SUFFIXES):
        return None
    description = parts[4] if len(parts) > 4 else ""
    detail = f" — {description}" if description else ""
    return f"{unit}: FAILED (active={active}, sub={sub}){detail}"


def _humanize_uptime_line(line):
    """Rewrite a raw `uptime` line into a sentence. Bails out (returns
    None, leaving the line untouched) on anything that doesn't match
    the regex instead of guessing - uptime's "up ..." segment alone
    has several different formats (minutes, HH:MM, "N days, HH:MM")
    and a wrong guess would be worse than the raw line."""
    m = _UPTIME_RE.search(line)
    if not m:
        return None
    users = m.group("users")
    user_word = "user" if users == "1" else "users"
    return (
        f"Up for {m.group('uptime')}. {users} {user_word} logged in. "
        f"Load average (1/5/15 min): {m.group('l1')}, {m.group('l5')}, {m.group('l15')}."
    )


def _humanize_report(text):
    """Run the line/block rewrites above over a whole report. Anything
    not recognized passes through completely unchanged."""
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]

        if line.split() == _DF_HEADER_TOKENS:
            end, rows = _parse_df_block(lines, i)
            if rows is not None:
                for fs, ftype, size, used, avail, usepct, mount in rows:
                    out.append(
                        f"{mount}: {usepct} used ({used} of {size} total, "
                        f"{avail} available; {ftype})"
                    )
            else:
                out.extend(lines[i:end])
            i = end
            continue

        if line.split() == _FREE_HEADER_TOKENS:
            # The free -h column header is redundant once the Mem:/
            # Swap: rows right below it are rewritten into sentences.
            i += 1
            continue

        rewritten = (
            _humanize_free_line(line)
            or _humanize_failed_unit_line(line)
            or _humanize_uptime_line(line)
        )
        out.append(rewritten if rewritten is not None else line)
        i += 1

    return "\n".join(out)


def _highlight_problems(text):
    """HTML-escape `text` line by line, wrapping any line _line_color()
    flags in a colored, bold span - applied to every health/process/log
    report (not just the combined Host Health Check verdict) so a
    problem reads at a glance instead of requiring the operator to
    scan a wall of monospace output for it."""
    out_lines = []
    for line in text.splitlines():
        escaped = html.escape(line)
        color = _line_color(line)
        if color:
            out_lines.append(f'<span style="color:{color}; font-weight:bold;">{escaped}</span>')
        else:
            out_lines.append(escaped)
    return "\n".join(out_lines)


class SystemHealthLogsPage(QWidget):
    """
    System Health & Logs against a *merged* host list (agent-enrolled
    hosts AND SSH-enrolled hosts).

    Split out of the original combined System Administration page so it
    opens as its own focused window from the System Administration menu.
    There's no single "active host" concept here (unlike User & Group
    Administration) - every health action just runs against whichever
    hosts are checked.
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("System Health & Logs")
        self.resize(980, 640)

        self.health_results = {}    # entry_key -> {label, stdout, stderr, code, pending}
        self.health_pending = {}    # entry_key -> (entry, task_id)

        main = QVBoxLayout()
        self.setLayout(main)

        main.addLayout(make_page_header("System Health & Logs"))

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
        # HEALTH PANEL
        # =========================================================
        main.addWidget(self._build_health_panel())

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

        self.health_poll_timer = QTimer()
        self.health_poll_timer.timeout.connect(self._poll_health)

        bus.host_removed.connect(self.load_hosts)

    # =========================================================
    # PANEL BUILDER
    # =========================================================
    def _build_health_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)

        actions_row = QHBoxLayout()
        btn_health = QPushButton("Check Host Health")
        btn_health.setStyleSheet("font-weight: bold;")
        btn_health.clicked.connect(self.run_health_check)
        btn_disk = QPushButton("Disk Usage")
        btn_disk.clicked.connect(self.run_disk_usage)
        btn_mem = QPushButton("Memory && CPU Snapshot")
        btn_mem.clicked.connect(self.run_memory_cpu)
        btn_uptime = QPushButton("Uptime")
        btn_uptime.clicked.connect(self.run_uptime)
        btn_failed = QPushButton("Failed Services")
        btn_failed.clicked.connect(self.run_failed_services)
        actions_row.addWidget(btn_health)
        actions_row.addWidget(btn_disk)
        actions_row.addWidget(btn_mem)
        actions_row.addWidget(btn_uptime)
        actions_row.addWidget(btn_failed)
        layout.addLayout(actions_row)

        large_files_row = QHBoxLayout()
        self.large_files_path = QLineEdit()
        self.large_files_path.setPlaceholderText("Path (default /)")
        self.large_files_path.setMaximumWidth(220)
        self.large_files_top_n = QSpinBox()
        self.large_files_top_n.setRange(1, 200)
        self.large_files_top_n.setValue(20)
        btn_large_files = QPushButton("Find Large Files")
        btn_large_files.clicked.connect(self.run_find_large_files)
        large_files_row.addWidget(QLabel("Find Large Files:"))
        large_files_row.addWidget(self.large_files_path)
        large_files_row.addWidget(QLabel("Top N:"))
        large_files_row.addWidget(self.large_files_top_n)
        large_files_row.addWidget(btn_large_files)
        layout.addLayout(large_files_row)

        log_row = QHBoxLayout()
        self.log_pattern = QLineEdit()
        self.log_pattern.setPlaceholderText("Search pattern (optional)")
        self.log_pattern.setMaximumWidth(280)
        self.log_lines = QSpinBox()
        self.log_lines.setRange(1, 5000)
        self.log_lines.setValue(200)
        btn_search_log = QPushButton("Search / Tail Logs")
        btn_search_log.clicked.connect(self.run_search_log)
        log_row.addWidget(QLabel("Search Logs:"))
        log_row.addWidget(self.log_pattern)
        log_row.addWidget(QLabel("Lines:"))
        log_row.addWidget(self.log_lines)
        log_row.addWidget(btn_search_log)
        layout.addLayout(log_row)

        divider = QFrame()
        divider.setFrameShape(QFrame.HLine)
        divider.setFrameShadow(QFrame.Sunken)
        layout.addWidget(divider)

        proc_header = QLabel("Process Management")
        proc_header.setStyleSheet("font-weight: bold;")
        layout.addWidget(proc_header)

        proc_view_row = QHBoxLayout()
        btn_proc_cpu = QPushButton("View Processes (by CPU)")
        btn_proc_cpu.clicked.connect(self.run_list_processes_cpu)
        btn_proc_mem = QPushButton("View Processes (by Memory)")
        btn_proc_mem.clicked.connect(self.run_list_processes_mem)
        btn_high_load = QPushButton("Investigate High Load")
        btn_high_load.clicked.connect(self.run_investigate_high_load)
        btn_zombies = QPushButton("Zombie Processes")
        btn_zombies.clicked.connect(self.run_zombie_processes)
        proc_view_row.addWidget(btn_proc_cpu)
        proc_view_row.addWidget(btn_proc_mem)
        proc_view_row.addWidget(btn_high_load)
        proc_view_row.addWidget(btn_zombies)
        layout.addLayout(proc_view_row)

        proc_action_row = QHBoxLayout()
        self.proc_pid = QLineEdit()
        self.proc_pid.setPlaceholderText("PID")
        self.proc_pid.setFixedWidth(70)
        self.proc_signal = QComboBox()
        self.proc_signal.addItems(
            ["TERM", "KILL", "HUP", "INT", "QUIT", "USR1", "USR2", "STOP", "CONT"]
        )
        self.proc_signal.setMaximumWidth(100)
        btn_kill = QPushButton("Kill Process")
        btn_kill.clicked.connect(self.run_kill_process)
        self.proc_nice = QSpinBox()
        self.proc_nice.setRange(-20, 19)
        self.proc_nice.setValue(0)
        btn_renice = QPushButton("Set Priority")
        btn_renice.clicked.connect(self.run_renice_process)
        btn_restart = QPushButton("Restart Process")
        btn_restart.setToolTip(
            "Captures the process's command line, stops it, then relaunches the same "
            "command line in the background. Linux hosts only (needs /proc)."
        )
        btn_restart.clicked.connect(self.run_restart_process)
        proc_action_row.addWidget(QLabel("Target PID:"))
        proc_action_row.addWidget(self.proc_pid)
        proc_action_row.addWidget(QLabel("Signal:"))
        proc_action_row.addWidget(self.proc_signal)
        proc_action_row.addWidget(btn_kill)
        proc_action_row.addWidget(QLabel("Niceness:"))
        proc_action_row.addWidget(self.proc_nice)
        proc_action_row.addWidget(btn_renice)
        proc_action_row.addWidget(btn_restart)
        layout.addLayout(proc_action_row)

        divider2 = QFrame()
        divider2.setFrameShape(QFrame.HLine)
        divider2.setFrameShadow(QFrame.Sunken)
        layout.addWidget(divider2)

        log_header = QLabel("Logging and Troubleshooting")
        log_header.setStyleSheet("font-weight: bold;")
        layout.addWidget(log_header)

        log_review_row = QHBoxLayout()
        self.logging_lines = QSpinBox()
        self.logging_lines.setRange(1, 5000)
        self.logging_lines.setValue(200)
        btn_review_logs = QPushButton("Review System Logs")
        btn_review_logs.clicked.connect(self.run_review_system_logs)
        self.journal_priority = QComboBox()
        self.journal_priority.addItems(["(any priority)"] + _JOURNAL_PRIORITY_CHOICES)
        self.journal_priority.setMaximumWidth(130)
        btn_analyze_journal = QPushButton("Analyze Journal Logs")
        btn_analyze_journal.clicked.connect(self.run_analyze_journal_logs)
        btn_kernel_msgs = QPushButton("Monitor Kernel Messages")
        btn_kernel_msgs.clicked.connect(self.run_monitor_kernel_messages)
        btn_audit_logs = QPushButton("Review Audit Logs")
        btn_audit_logs.clicked.connect(self.run_review_audit_logs)
        log_review_row.addWidget(QLabel("Lines:"))
        log_review_row.addWidget(self.logging_lines)
        log_review_row.addWidget(btn_review_logs)
        log_review_row.addWidget(self.journal_priority)
        log_review_row.addWidget(btn_analyze_journal)
        log_review_row.addWidget(btn_kernel_msgs)
        log_review_row.addWidget(btn_audit_logs)
        layout.addLayout(log_review_row)

        app_errors_row = QHBoxLayout()
        self.app_error_unit = QLineEdit()
        self.app_error_unit.setPlaceholderText("Service/unit (optional - blank = whole journal)")
        self.app_error_unit.setMaximumWidth(260)
        btn_trace_errors = QPushButton("Trace Application Errors")
        btn_trace_errors.clicked.connect(self.run_trace_application_errors)
        app_errors_row.addWidget(QLabel("Trace Application Errors:"))
        app_errors_row.addWidget(self.app_error_unit)
        app_errors_row.addWidget(btn_trace_errors)
        layout.addLayout(app_errors_row)

        diag_row = QHBoxLayout()
        btn_boot_failures = QPushButton("Investigate Boot Failures")
        btn_boot_failures.clicked.connect(self.run_investigate_boot_failures)
        btn_crashes = QPushButton("Investigate Crashes")
        btn_crashes.clicked.connect(self.run_investigate_crashes)
        btn_mem_issues = QPushButton("Troubleshoot Memory Issues")
        btn_mem_issues.clicked.connect(self.run_troubleshoot_memory_issues)
        btn_cpu_bottlenecks = QPushButton("Analyze CPU Bottlenecks")
        btn_cpu_bottlenecks.clicked.connect(self.run_analyze_cpu_bottlenecks)
        diag_row.addWidget(btn_boot_failures)
        diag_row.addWidget(btn_crashes)
        diag_row.addWidget(btn_mem_issues)
        diag_row.addWidget(btn_cpu_bottlenecks)
        layout.addLayout(diag_row)

        support_row = QHBoxLayout()
        btn_support_info = QPushButton("Collect Support Information")
        btn_support_info.clicked.connect(self.run_collect_support_info)
        btn_sos_report = QPushButton("Generate sos Report")
        btn_sos_report.setToolTip(
            "Runs the distro's sos/sosreport tool in unattended batch mode. "
            "Does not install it if missing - install the 'sos' (RHEL/Fedora/openSUSE) "
            "or 'sosreport' (Debian/Ubuntu) package first."
        )
        btn_sos_report.clicked.connect(self.run_generate_sos_report)
        support_row.addWidget(btn_support_info)
        support_row.addWidget(btn_sos_report)
        layout.addLayout(support_row)

        self.health_status = QLabel("Pick an action above to run it on all checked hosts.")
        self.health_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        layout.addWidget(self.health_status)

        # One tab per (host, report) pair that's actually been run,
        # instead of a host-list-plus-single-shared-output-panel - that
        # older layout meant only the clicked-on host's result was ever
        # visible, which is exactly the "can't see two hosts' results at
        # once" complaint User & Group Administration ran into with
        # account status (see its "View Status by Host..." popup).
        # Running a different report no longer clears these either - it
        # used to wipe every open tab so e.g. a Host Health Check result
        # would vanish the moment Disk Usage was run afterward; now each
        # (host, report) combination keeps its own tab, which only that
        # same host+report combination's next run refreshes in place.
        reports_header = QHBoxLayout()
        reports_title = QLabel("Reports")
        reports_title.setStyleSheet("font-weight: bold;")
        reports_header.addWidget(reports_title)
        reports_header.addStretch()
        btn_clear_reports = QPushButton("Clear All Reports")
        btn_clear_reports.clicked.connect(self.clear_health_tabs)
        reports_header.addWidget(btn_clear_reports)
        layout.addLayout(reports_header)

        self.health_tabs = QTabWidget()
        self.health_tabs.setTabsClosable(True)
        self.health_tabs.tabCloseRequested.connect(self._close_health_tab)
        layout.addWidget(self.health_tabs)
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
            self.health_status.setStyleSheet(f"color: {STATUS_ERROR_COLOR};")
            self.health_status.setText(f"Could not load hosts: {e}")
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
        """A fixed 110px box looked fine with a handful of hosts but
        left a large, obviously-unused blank gap below the rows when
        there were only one or two - size to the actual *visible* row
        count instead (collapsed groups should shrink the box, not
        just hide their rows inside an unchanged-size box), capped so
        a long host list still scrolls rather than taking over the
        page."""
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
    # SYSTEM HEALTH & LOGS
    # =========================================================
    def run_disk_usage(self):
        self._run_health_command(api.cmd_disk_usage(), "Disk Usage")

    def run_memory_cpu(self):
        self._run_health_command(api.cmd_memory_cpu_snapshot(), "Memory & CPU Snapshot")

    def run_failed_services(self):
        self._run_health_command(api.cmd_failed_services(), "Failed Services")

    def run_uptime(self):
        self._run_health_command(api.cmd_uptime(), "Uptime")

    def run_health_check(self):
        self._run_health_command(api.cmd_health_check(), "Host Health Check")

    def run_find_large_files(self):
        path = self.large_files_path.text().strip() or "/"
        top_n = self.large_files_top_n.value()
        cmd = api.cmd_find_large_files(path, top_n)
        self._run_health_command(cmd, f"Find Large Files ({path}, top {top_n})")

    def run_search_log(self):
        pattern = self.log_pattern.text().strip()
        lines = self.log_lines.value()
        cmd = api.cmd_search_log(pattern, lines)
        label = f"Search Logs ('{pattern}')" if pattern else f"Tail Logs ({lines} lines)"
        self._run_health_command(cmd, label)

    # ---------------------------------------------------------
    # PROCESS MANAGEMENT
    # ---------------------------------------------------------
    def run_list_processes_cpu(self):
        self._run_health_command(api.cmd_list_processes("cpu"), "Processes by CPU")

    def run_list_processes_mem(self):
        self._run_health_command(api.cmd_list_processes("mem"), "Processes by Memory")

    def run_investigate_high_load(self):
        self._run_health_command(api.cmd_investigate_high_load(), "Investigate High Load")

    def run_zombie_processes(self):
        self._run_health_command(api.cmd_zombie_processes(), "Zombie Processes")

    def _target_pid(self):
        pid = self.proc_pid.text().strip()
        if not pid:
            QMessageBox.information(self, "PID required", "Enter a target PID first.")
            return None
        return pid

    def run_kill_process(self):
        pid = self._target_pid()
        if pid is None:
            return
        signal = self.proc_signal.currentText()
        try:
            cmd = api.cmd_kill_process(pid, signal)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_health_command(cmd, f"Kill PID {pid} (SIG{signal})")

    def run_renice_process(self):
        pid = self._target_pid()
        if pid is None:
            return
        niceness = self.proc_nice.value()
        try:
            cmd = api.cmd_renice_process(pid, niceness)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_health_command(cmd, f"Set Priority of PID {pid} to {niceness}")

    def run_restart_process(self):
        pid = self._target_pid()
        if pid is None:
            return
        try:
            cmd = api.cmd_restart_process(pid)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        self._run_health_command(cmd, f"Restart Process (PID {pid})")

    # ---------------------------------------------------------
    # LOGGING AND TROUBLESHOOTING
    # ---------------------------------------------------------
    def run_review_system_logs(self):
        lines = self.logging_lines.value()
        cmd = api.cmd_review_system_logs(lines)
        self._run_health_command(cmd, f"Review System Logs ({lines} lines)")

    def run_analyze_journal_logs(self):
        lines = self.logging_lines.value()
        priority = self.journal_priority.currentText().strip()
        if priority == "(any priority)":
            priority = ""
        try:
            cmd = api.cmd_analyze_journal_logs(priority, lines)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        label = f"Analyze Journal Logs ({priority or 'any priority'}, {lines} lines)"
        self._run_health_command(cmd, label)

    def run_monitor_kernel_messages(self):
        lines = self.logging_lines.value()
        cmd = api.cmd_monitor_kernel_messages(lines)
        self._run_health_command(cmd, f"Monitor Kernel Messages ({lines} lines)")

    def run_review_audit_logs(self):
        lines = self.logging_lines.value()
        cmd = api.cmd_review_audit_logs(lines)
        self._run_health_command(cmd, f"Review Audit Logs ({lines} lines)")

    def run_trace_application_errors(self):
        unit = self.app_error_unit.text().strip()
        lines = self.logging_lines.value()
        cmd = api.cmd_trace_application_errors(unit, lines)
        label = f"Trace Application Errors ({unit})" if unit else f"Trace Application Errors ({lines} lines, whole journal)"
        self._run_health_command(cmd, label)

    def run_investigate_boot_failures(self):
        self._run_health_command(api.cmd_investigate_boot_failures(), "Investigate Boot Failures")

    def run_investigate_crashes(self):
        self._run_health_command(api.cmd_investigate_crashes(), "Investigate Crashes")

    def run_troubleshoot_memory_issues(self):
        self._run_health_command(api.cmd_troubleshoot_memory_issues(), "Troubleshoot Memory Issues")

    def run_analyze_cpu_bottlenecks(self):
        self._run_health_command(api.cmd_analyze_cpu_bottlenecks(), "Analyze CPU Bottlenecks")

    def run_collect_support_info(self):
        self._run_health_command(api.cmd_collect_support_info(), "Collect Support Information")

    def run_generate_sos_report(self):
        self._run_health_command(api.cmd_generate_sos_report(), "Generate sos Report")

    def _tab_key(self, entry, label):
        """Identity for one (host, report) result tab. Including the
        report label alongside the host's own key means running a
        second, different report no longer collides with - or clears -
        a first one; running the *same* report on the same host again
        still lands on that one tab instead of piling up duplicates."""
        return (entry["kind"], entry["id"], label)

    def _find_tab_index(self, key):
        bar = self.health_tabs.tabBar()
        for i in range(self.health_tabs.count()):
            if bar.tabData(i) == key:
                return i
        return None

    def clear_health_tabs(self):
        self.health_tabs.clear()
        self.health_results = {}
        self.health_pending = {}
        self.health_poll_timer.stop()
        self.health_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        self.health_status.setText("Pick an action above to run it on all checked hosts.")

    def _run_health_command(self, command, label):
        entries = self.checked_entries()

        if not entries:
            QMessageBox.information(self, "No hosts checked", "Check one or more target hosts first.")
            return

        first_idx = None

        for entry in entries:
            key = self._tab_key(entry, label)
            result = api.run_on_entry(entry, command)

            if result["sync"]:
                self.health_results[key] = {
                    "label": entry["label"],
                    "stdout": result["stdout"],
                    "stderr": result["stderr"] or result["error"] or "",
                    "code": result["code"],
                    "pending": False,
                }
            elif result["error"]:
                self.health_results[key] = {
                    "label": entry["label"], "stdout": "", "stderr": result["error"],
                    "code": None, "pending": False,
                }
            else:
                self.health_results[key] = {
                    "label": entry["label"], "stdout": "", "stderr": "",
                    "code": None, "pending": True,
                }
                self.health_pending[key] = (entry, result["task_id"])

            self._add_health_tab(key)

            if first_idx is None:
                first_idx = self._find_tab_index(key)

        self.health_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        self.health_status.setText(f"Running '{label}' on {len(entries)} host(s)...")

        # Jump to this run's own first tab, not tab 0 - tab 0 may belong
        # to an earlier, unrelated report that's still sitting open.
        if first_idx is not None:
            self.health_tabs.setCurrentIndex(first_idx)

        if self.health_pending:
            self.health_poll_timer.start(HEALTH_POLL_MS)
        else:
            self.health_status.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
            self.health_status.setText(f"'{label}' complete.")

    def _status_text(self, data):
        if data["pending"]:
            return "pending..."
        verdict = _extract_health_verdict(data["stdout"])
        if verdict:
            return verdict
        return "ok" if not data["stderr"] else "error"

    def _render_result(self, text_edit, data):
        """Render one host's result into its tab's QTextEdit. Pulled out
        of the old single-shared-panel click handler so both tab
        creation and live-updating an already-open tab on poll
        completion can share it."""
        if data["pending"]:
            text_edit.setPlainText("Waiting for this agent host to report back...")
            return

        if data["stderr"] and not data["stdout"]:
            text_edit.setHtml(
                f'<span style="color:{_status_color("error")}; font-weight:bold;">'
                f'ERROR:</span><pre style="white-space:pre-wrap; margin:4px 0 0 0;">'
                f'{html.escape(data["stderr"])}</pre>'
            )
            return

        text = data["stdout"]
        if data["stderr"]:
            text += f"\n\n--- stderr ---\n{data['stderr']}"

        # Rewrite the raw df/free/systemctl/uptime shapes into plain
        # sentences first, then highlight every problematic line
        # (verdicts, disk/memory/load percentages over threshold,
        # failed services, zombie counts) on the rewritten text - not
        # just the combined Host Health Check's leading verdict, every
        # health/process/log report gets the same treatment.
        text_edit.setHtml(
            f'<pre style="font-family:monospace; white-space:pre-wrap; margin:0;">'
            f'{_highlight_problems(_humanize_report(text))}</pre>'
        )

    def _tab_title(self, key, data):
        # key is (kind, id, report_label) - include the report label
        # alongside the host's own label so two tabs for the same host
        # (Host Health Check vs. Disk Usage, say) read as distinct
        # tabs instead of two identically-titled ones.
        report_label = key[2]
        return f"{data['label']} — {report_label}  [{self._status_text(data)}]"

    def _close_health_tab(self, index):
        bar = self.health_tabs.tabBar()
        key = bar.tabData(index)
        self.health_tabs.removeTab(index)
        self.health_results.pop(key, None)
        self.health_pending.pop(key, None)

    def _add_health_tab(self, key):
        data = self.health_results.get(key)
        if not data:
            return

        existing = self._find_tab_index(key)
        if existing is not None:
            self._refresh_health_tab(key)
            return

        text_edit = QTextEdit()
        text_edit.setReadOnly(True)
        text_edit.setStyleSheet("font-family: monospace;")
        self._render_result(text_edit, data)

        idx = self.health_tabs.addTab(text_edit, self._tab_title(key, data))
        self.health_tabs.tabBar().setTabTextColor(idx, QColor(_status_color(self._status_text(data))))
        self.health_tabs.tabBar().setTabData(idx, key)

    def _refresh_health_tab(self, key):
        bar = self.health_tabs.tabBar()
        for i in range(self.health_tabs.count()):
            if bar.tabData(i) != key:
                continue
            data = self.health_results.get(key)
            if data:
                self.health_tabs.setTabText(i, self._tab_title(key, data))
                bar.setTabTextColor(i, QColor(_status_color(self._status_text(data))))
                self._render_result(self.health_tabs.widget(i), data)
            return

    def _poll_health(self):
        if not self.health_pending:
            self.health_poll_timer.stop()
            return

        done = []

        for key, (entry, task_id) in list(self.health_pending.items()):
            result = api.poll_entry_result(entry, task_id)
            if result is None:
                continue

            self.health_results[key] = {
                "label": entry["label"],
                "stdout": result["stdout"],
                "stderr": result["stderr"] or result["error"] or "",
                "code": result["code"],
                "pending": False,
            }
            self._refresh_health_tab(key)
            done.append(key)

        for key in done:
            del self.health_pending[key]

        if not self.health_pending:
            self.health_poll_timer.stop()
            self.health_status.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
            self.health_status.setText("All hosts reported back.")
