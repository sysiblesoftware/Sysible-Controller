import os
import queue
import re
import time
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QLineEdit, QTextEdit, QComboBox, QInputDialog, QMessageBox,
    QApplication, QFileDialog, QFrame,
)
from PySide6.QtCore import Qt, QTimer, QObject, Signal
from PySide6.QtGui import QFont, QKeySequence

from client import api
from client.branding import make_page_header
from client.events import bus
from client import theme
from client.theme import STATUS_NEUTRAL_COLOR, STATUS_SUCCESS_COLOR, STATUS_ERROR_COLOR
from client.host_panel import build_host_panel
from client.collapsible_groups import (
    make_group_header_item, apply_collapse_state, get_collapsed_groups,
    connect_group_toggle, add_collapse_expand_buttons,
)

_NEW_ENV_OPTION = "+ New environment..."
_UNASSIGNED_LABEL = "Unassigned"

HOST_REFRESH_MS = 10000

AGENT_CMD_POLL_MS = 1500
AGENT_CMD_TIMEOUT_S = 30

# Brief backoff after a transient SSH read error so the completion-
# driven read loop in _TerminalIO doesn't spin against a host that's
# momentarily unreachable (rebooting, network blip).
SSH_TERMINAL_POLL_MS = 60


# Temporary diagnostic logging for the SSH terminal read path. Writes to
# /tmp/sysible_term.log (override with SYSIBLE_TERM_LOG). Off unless the
# file's directory is writable; safe to leave in - it never raises.
import datetime as _dt
_TERM_LOG_PATH = os.getenv("SYSIBLE_TERM_LOG", "/tmp/sysible_term.log")


def _tlog(msg):
    try:
        with open(_TERM_LOG_PATH, "a") as f:
            f.write(f"{_dt.datetime.now().strftime('%H:%M:%S.%f')} {msg}\n")
    except Exception:
        pass


# =====================================================================
# LOW-LEVEL TERMINAL I/O (shared by every TerminalPopout)
# =====================================================================
class _TerminalIO(QObject):
    """Runs the blocking SSH terminal read/write HTTP calls on a
    background thread pool instead of the Qt GUI thread, so neither a
    keystroke nor the backend's long-poll wait (see
    backend/remote_routes.py's TERMINAL_LONG_POLL_S) can ever freeze
    the app. Qt automatically queues a Signal emitted from a worker
    thread onto the thread its receiver lives on, so the connected
    slots are safe to touch widgets directly."""

    read_done = Signal(str, dict)   # host_id, result
    read_failed = Signal(str)       # host_id
    write_failed = Signal(str)      # error text

    def __init__(self):
        super().__init__()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ssh-term-io")
        self._reads_inflight = set()
        # Read results are handed to the GUI thread through this queue
        # (drained by a QTimer there) rather than a Qt signal - see
        # _on_read_finished().
        self.results = queue.Queue()

    def submit_read(self, host_id):
        if host_id in self._reads_inflight:
            _tlog(f"submit_read host={host_id} SKIPPED (inflight)")
            return
        self._reads_inflight.add(host_id)
        _tlog(f"submit_read host={host_id}")

        def work():
            try:
                return True, api.read_terminal(host_id)
            except Exception:
                return False, None

        future = self._pool.submit(work)
        future.add_done_callback(lambda f: self._on_read_finished(host_id, f))

    def _on_read_finished(self, host_id, future):
        self._reads_inflight.discard(host_id)
        ok, result = future.result()
        _tlog(f"read_finished host={host_id} ok={ok} datalen="
              f"{len((result or {}).get('data','')) if (ok and result) else 'NA'}")
        # Push to a thread-safe queue instead of emitting a Qt signal.
        # This callback runs on a plain ThreadPoolExecutor worker (not a
        # QThread), and a signal emitted from such a thread was not being
        # delivered to the GUI reliably - the symptom was a live SSH
        # terminal that forwarded keystrokes but never rendered the
        # remote output, and whose read loop stalled after the first
        # read (the handler that re-issues reads never ran). A queue
        # drained by a QTimer on the GUI thread has no such dependency.
        self.results.put((host_id, result if ok else None))

    def submit_write(self, host_id, data):
        def work():
            try:
                api.write_terminal(host_id, data)
                return None
            except Exception as e:
                return str(e)

        future = self._pool.submit(work)
        future.add_done_callback(lambda f: self._on_write_finished(f))

    def _on_write_finished(self, future):
        err = future.result()
        if err:
            self.write_failed.emit(err)

    def shutdown(self):
        self._pool.shutdown(wait=False)


# Strips ANSI/VT100 escape sequences before display - the output pane
# is a plain scrolling text widget, not a full terminal emulator.
_ANSI_RE = re.compile(r"\x1b(\[[0-9;?]*[a-zA-Z]|\][^\x07]*\x07|[@-Z\\-_])")


def _strip_ansi(text):
    return _ANSI_RE.sub("", text)


_ARROW_KEY_MAP = {
    Qt.Key_Up: "\x1b[A",
    Qt.Key_Down: "\x1b[B",
    Qt.Key_Right: "\x1b[C",
    Qt.Key_Left: "\x1b[D",
    Qt.Key_Home: "\x1b[H",
    Qt.Key_End: "\x1b[F",
    Qt.Key_Delete: "\x1b[3~",
    Qt.Key_PageUp: "\x1b[5~",
    Qt.Key_PageDown: "\x1b[6~",
}


def _key_event_to_bytes(event):
    """Translate one Qt key press into the raw bytes a real terminal
    would send for it. Returns None for keys that carry no input."""
    key = event.key()
    modifiers = event.modifiers()

    if modifiers & Qt.ControlModifier and Qt.Key_A <= key <= Qt.Key_Z:
        return chr(key - Qt.Key_A + 1)  # Ctrl+A=0x01 ... Ctrl+C=0x03 ...

    if key in (Qt.Key_Return, Qt.Key_Enter):
        return "\r"
    if key == Qt.Key_Backspace:
        return "\x7f"
    if key == Qt.Key_Tab:
        return "\t"
    if key == Qt.Key_Escape:
        return "\x1b"
    if key in _ARROW_KEY_MAP:
        return _ARROW_KEY_MAP[key]

    text = event.text()
    return text if text else None


class _TerminalInput(QLineEdit):
    """QLineEdit with bash-style Up/Down command history, used for the
    agent (queued-command) console at the bottom of a TerminalPopout."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._history = []
        self._history_index = 0

    def remember(self, text):
        if text and (not self._history or self._history[-1] != text):
            self._history.append(text)
        self._history_index = len(self._history)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Up:
            if self._history:
                self._history_index = max(0, self._history_index - 1)
                self.setText(self._history[self._history_index])
            return

        if event.key() == Qt.Key_Down:
            if self._history_index < len(self._history):
                self._history_index += 1
            self.setText(
                self._history[self._history_index]
                if self._history_index < len(self._history)
                else ""
            )
            return

        super().keyPressEvent(event)


class _LiveTerminalOutput(QTextEdit):
    """The terminal output pane doubles as the input surface for an
    SSH host's live session: while `on_key_data` is set, keystrokes
    typed here are forwarded straight to the remote pty instead of
    being inserted locally - what shows up is only ever what the remote
    shell echoes back. With no session attached it behaves like an
    ordinary read-only log."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.on_key_data = None

    def keyPressEvent(self, event):
        if self.on_key_data is None:
            super().keyPressEvent(event)
            return

        if event.matches(QKeySequence.Copy) and self.textCursor().hasSelection():
            super().keyPressEvent(event)
            return

        if event.matches(QKeySequence.Paste):
            text = QApplication.clipboard().text()
            if text:
                self.on_key_data(text)
            return

        data = _key_event_to_bytes(event)
        if data is not None:
            self.on_key_data(data)

    def append_terminal_text(self, text):
        clean = _strip_ansi(text)
        _tlog(f"append called len={len(clean)}")
        try:
            cursor = self.textCursor()
            cursor.movePosition(cursor.End)

            for ch in clean:
                if ch == "\r":
                    continue
                elif ch in ("\x08", "\x7f"):
                    cursor.deletePreviousChar()
                else:
                    cursor.insertText(ch)

            self.setTextCursor(cursor)
            self.ensureCursorVisible()
        except Exception as e:
            # A rendering hiccup must never bubble up and kill the read
            # loop (that would leave the terminal blank but still
            # "connected"). Fall back to a plain append so output still
            # shows, and swallow anything that still goes wrong.
            _tlog(f"append PRIMARY EXC: {e!r}")
            try:
                self.moveCursor(self.textCursor().End)
                self.insertPlainText(clean.replace("\r", ""))
            except Exception as e2:
                _tlog(f"append FALLBACK EXC: {e2!r}")


def _connection_options(entry):
    """Return [(label, underlying_entry), ...] for a host entry. A
    merged host (enrolled both ways) offers SSH first (the only
    transport with a real interactive terminal) then Agent; single-mode
    hosts offer just the one they have."""
    if entry["kind"] == "merged":
        return [
            ("SSH (interactive terminal)", entry["ssh_entry"]),
            ("Agent (queued commands)", entry["agent_entry"]),
        ]
    if entry["kind"] == "ssh":
        return [("SSH (interactive terminal)", entry)]
    return [("Agent (queued commands)", entry)]


def _default_underlying(entry):
    """The sub-entry the main page targets for quick commands and file
    transfer - SSH preferred on a merged host, otherwise the entry
    itself."""
    if entry["kind"] == "merged":
        return entry["ssh_entry"] or entry["agent_entry"]
    return entry


def _row_ip(entry):
    """A short IP/address to show next to a host in the list. SSH
    addresses come through as "user@ip", so strip the user; merged
    hosts (same physical box, two transports) show a single IP,
    preferring the SSH side."""
    def _ip(addr):
        return (addr or "").split("@")[-1].strip()

    if entry["kind"] == "merged":
        ssh = entry.get("ssh_entry") or {}
        agent = entry.get("agent_entry") or {}
        return _ip(ssh.get("address")) or _ip(agent.get("address"))
    return _ip(entry.get("address"))


# =====================================================================
# PER-HOST TERMINAL POPOUT
# =====================================================================
class TerminalPopout(QWidget):
    """A standalone terminal window for one host, opened by double-
    clicking a row in Remote Host Administration's host list.

    For a host enrolled both ways, the connection picker at the top
    switches between SSH (a real interactive PTY session) and Agent (a
    queued-command console). Single-transport hosts show only what they
    have. Each open host gets its own window, so several can be live at
    once.
    """

    def __init__(self, entry, on_close=None):
        super().__init__()

        self.entry = entry
        self.on_close = on_close

        self.setWindowTitle(f"Terminal — {entry['label']}")
        self.resize(840, 540)

        self.active_kind = None
        self.active_id = None
        self.active_label = entry["label"]

        self.pending_agent_task = None
        self._ssh_session_active = False

        mono = QFont()
        mono.setFamilies(["Menlo", "Consolas", "DejaVu Sans Mono", "Courier New"])
        mono.setStyleHint(QFont.Monospace)
        mono.setPointSize(11)

        layout = QVBoxLayout(self)

        # ---- connection picker ----
        options = _connection_options(entry)

        top = QHBoxLayout()
        self.title_label = QLabel(entry["label"])
        self.title_label.setStyleSheet("font-weight: bold;")
        top.addWidget(self.title_label)
        top.addStretch()
        top.addWidget(QLabel("Connection:"))

        self.conn_combo = QComboBox()
        for label, underlying in options:
            self.conn_combo.addItem(label, underlying)
        self.conn_combo.setEnabled(len(options) > 1)
        self.conn_combo.currentIndexChanged.connect(self._on_conn_changed)
        top.addWidget(self.conn_combo)
        layout.addLayout(top)

        # ---- terminal output ----
        self.output = _LiveTerminalOutput()
        self.output.setReadOnly(True)
        self.output.setFont(mono)
        self.output.setStyleSheet(
            "QTextEdit {"
            "  background-color: #1E1E1E;"
            "  color: #E8E8E8;"
            "  border: 1px solid #3A3A3A;"
            "  border-radius: 6px;"
            "  padding: 8px;"
            "  selection-background-color: #4A4A4A;"
            "}"
        )
        self.output.setMinimumHeight(280)
        layout.addWidget(self.output, 1)

        self.ssh_hint = QLabel(
            "SSH terminal is live - click into the panel above and type "
            "directly (sudo prompts, vim, Ctrl+C, arrow keys all work)."
        )
        theme.style_hint_label(self.ssh_hint)
        self.ssh_hint.setVisible(False)
        layout.addWidget(self.ssh_hint)

        # ---- agent command row (only used in Agent mode) ----
        cmd_row = QHBoxLayout()

        self.cmd_input = _TerminalInput()
        self.cmd_input.setFont(mono)
        self.cmd_input.setPlaceholderText("Type a command and press Enter...")
        self.cmd_input.setEnabled(False)
        self.cmd_input.returnPressed.connect(self.run_command)

        self.run_btn = QPushButton("Run")
        self.run_btn.clicked.connect(self.run_command)

        self.interrupt_btn = QPushButton("Send Ctrl+C")
        self.interrupt_btn.clicked.connect(self.send_interrupt)
        self.interrupt_btn.setVisible(False)

        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.output.clear)

        cmd_row.addWidget(self.cmd_input, 4)
        cmd_row.addWidget(self.run_btn)
        cmd_row.addWidget(self.interrupt_btn)
        cmd_row.addWidget(clear_btn)
        layout.addLayout(cmd_row)

        # ---- timers / io ----
        self.agent_poll_timer = QTimer()
        self.agent_poll_timer.timeout.connect(self._poll_agent_task)

        self._terminal_io = _TerminalIO()
        self._terminal_io.write_failed.connect(self._on_ssh_write_failed)

        # Drain SSH read results on the GUI thread. This replaces the
        # old cross-thread read_done/read_failed signals (see
        # _TerminalIO._on_read_finished for why). Runs continuously and
        # is a no-op whenever the queue is empty.
        self._read_drain_timer = QTimer()
        self._read_drain_timer.setInterval(20)
        self._read_drain_timer.timeout.connect(self._drain_read_results)
        self._read_drain_timer.start()

        self._ssh_retry_timer = QTimer()
        self._ssh_retry_timer.setSingleShot(True)
        self._ssh_retry_timer.timeout.connect(self._resume_ssh_read_loop)

        # ---- start on the first (preferred) connection ----
        self._switch_to(options[0][1])

    # ---------------- connection switching ----------------
    def _on_conn_changed(self, _index):
        underlying = self.conn_combo.currentData()
        if underlying is not None:
            self._switch_to(underlying)

    def _switch_to(self, underlying):
        if underlying is None:
            return
        if underlying["kind"] == self.active_kind and underlying["id"] == self.active_id:
            return

        self._cancel_pending_agent_task()
        self._end_ssh_session()

        self.active_kind = underlying["kind"]
        self.active_id = underlying["id"]

        if underlying["kind"] == "ssh":
            self._start_ssh_session(underlying)
        else:
            self.output.append(f"[agent connection - queued commands on {self.active_label}]")
            self.cmd_input.setEnabled(True)
            self.cmd_input.setPlaceholderText(f"{self.active_label} $ ")
            self.cmd_input.setFocus()

    # ---------------- SSH live session ----------------
    def _start_ssh_session(self, entry):
        self.title_label.setText(f"{self.active_label}  [SSH] (connecting...)")
        self.cmd_input.setEnabled(False)
        self.run_btn.setEnabled(False)
        QApplication.processEvents()

        # Force a fresh PTY: close any session the backend may still be
        # holding for this host from a previous GUI run that didn't shut
        # down cleanly, so we never attach to a dead/stale channel (which
        # shows "live" but produces no output and accepts no input).
        # close_terminal is idempotent - a no-op if there's nothing open.
        try:
            api.close_terminal(entry["id"])
        except Exception:
            pass

        try:
            api.open_terminal(entry["id"])
        except Exception as e:
            self.output.append(f"[could not open terminal session: {e}]")
            self.title_label.setText(f"{self.active_label}  [SSH] (failed)")
            self.run_btn.setEnabled(True)
            self.active_kind = None
            self.active_id = None
            return

        self.title_label.setText(f"{self.active_label}  [SSH] (live)")
        self.cmd_input.hide()
        self.run_btn.hide()
        self.interrupt_btn.setVisible(True)
        self.ssh_hint.setVisible(True)
        self.output.setReadOnly(False)
        self.output.on_key_data = self._send_terminal_input
        self.output.setFocus()
        self._ssh_session_active = True
        _tlog(f"ssh session LIVE host={self.active_id}, issuing first read")
        self._terminal_io.submit_read(self.active_id)

    def _send_terminal_input(self, data):
        self._terminal_io.submit_write(self.active_id, data)

    def send_interrupt(self):
        if self.active_kind == "ssh" and self.output.on_key_data is not None:
            self._send_terminal_input("\x03")
            self.output.setFocus()

    def _drain_read_results(self):
        """Pull any read results the worker threads have queued and handle
        them here on the GUI thread. Each handled read also re-issues the
        next read, so this is what keeps the live SSH read loop turning."""
        while True:
            try:
                host_id, result = self._terminal_io.results.get_nowait()
            except queue.Empty:
                return
            if result is None:
                self._on_ssh_read_failed(host_id)
            else:
                self._on_ssh_read_done(host_id, result)

    def _on_ssh_read_done(self, host_id, result):
        _tlog(f"read_done host={host_id} active={self.active_id} kind={self.active_kind} "
              f"sess={self._ssh_session_active} datalen={len(result.get('data',''))} "
              f"closed={result.get('closed')}")
        if not self._ssh_session_active or self.active_kind != "ssh" or self.active_id != host_id:
            return

        if result.get("data"):
            self.output.append_terminal_text(result["data"])
            self._scroll_to_bottom()

        if result.get("closed"):
            self.output.append_terminal_text(
                "\n[remote session ended - switch connection or reopen to reconnect]\n"
            )
            self._end_ssh_session(close_remote=False)
            self.active_kind = None
            self.active_id = None
            self.title_label.setText(f"{self.active_label}  [SSH] (closed)")
            return

        self._terminal_io.submit_read(host_id)

    def _on_ssh_read_failed(self, host_id):
        if not self._ssh_session_active or self.active_kind != "ssh" or self.active_id != host_id:
            return
        self._ssh_retry_timer.start(SSH_TERMINAL_POLL_MS)

    def _resume_ssh_read_loop(self):
        if self._ssh_session_active and self.active_kind == "ssh" and self.active_id:
            self._terminal_io.submit_read(self.active_id)

    def _on_ssh_write_failed(self, err):
        self.output.append_terminal_text(f"\n[write failed: {err}]\n")

    def _end_ssh_session(self, close_remote=True):
        was_ssh = self.active_kind == "ssh" and self._ssh_session_active

        self._ssh_session_active = False
        self._ssh_retry_timer.stop()
        self.output.on_key_data = None
        self.output.setReadOnly(True)
        self.cmd_input.show()
        self.run_btn.show()
        self.interrupt_btn.setVisible(False)
        self.cmd_input.setEnabled(False)
        self.run_btn.setEnabled(True)
        self.ssh_hint.setVisible(False)

        if was_ssh and close_remote and self.active_id:
            try:
                api.close_terminal(self.active_id)
            except Exception:
                pass

    # ---------------- Agent queued-command console ----------------
    def run_command(self):
        if not self.active_id or self.active_kind != "agent":
            return

        cmd = self.cmd_input.text().strip()
        if not cmd:
            return

        self.cmd_input.remember(cmd)
        self.cmd_input.clear()

        self.output.append(f"{self.active_label} $ {cmd}")
        self._scroll_to_bottom()
        self._run_agent_command(cmd)

    def _run_agent_command(self, cmd):
        if self.pending_agent_task:
            self.output.append("[a command is already running on this host - wait for it to finish]")
            return

        try:
            task_ids = api.queue_command_on_hosts([self.active_id], cmd)
        except Exception as e:
            self.output.append(str(e))
            return

        task_id = task_ids.get(self.active_id)
        if task_id is None:
            self.output.append("[failed to queue command on agent]")
            return

        self.output.append("running...")
        self._scroll_to_bottom()
        self.cmd_input.setEnabled(False)

        self.pending_agent_task = {
            "host_id": self.active_id,
            "task_id": task_id,
            "deadline": time.time() + AGENT_CMD_TIMEOUT_S,
        }
        self.agent_poll_timer.start(AGENT_CMD_POLL_MS)

    def _cancel_pending_agent_task(self):
        self.pending_agent_task = None
        self.agent_poll_timer.stop()

    def _poll_agent_task(self):
        task = self.pending_agent_task
        if not task:
            self.agent_poll_timer.stop()
            return

        if self.active_kind != "agent" or self.active_id != task["host_id"]:
            self.pending_agent_task = None
            self.agent_poll_timer.stop()
            return

        try:
            raw = api.get_result_by_task(task["host_id"], task["task_id"])
        except Exception:
            raw = None

        if raw is None:
            if time.time() > task["deadline"]:
                self.output.append("[timed out waiting for agent to report back]")
                self.pending_agent_task = None
                self.agent_poll_timer.stop()
                self._finish_command()
            return

        output = api.parse_task_output(raw)
        if output:
            if output.get("stdout"):
                self.output.append(output["stdout"].rstrip("\n"))
            if output.get("stderr"):
                self.output.append(output["stderr"].rstrip("\n"))
            if output.get("returncode", 0) != 0:
                self.output.append(f"[exit code {output.get('returncode')}]")
        else:
            self.output.append("[agent reported no usable output]")

        self.pending_agent_task = None
        self.agent_poll_timer.stop()
        self._finish_command()

    def _finish_command(self):
        if self.active_kind == "agent":
            self.cmd_input.setEnabled(True)
            self.cmd_input.setFocus()
        self._scroll_to_bottom()

    def _scroll_to_bottom(self):
        scrollbar = self.output.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def closeEvent(self, event):
        self._cancel_pending_agent_task()
        self._end_ssh_session()
        self._terminal_io.shutdown()
        if self.on_close:
            self.on_close(self.entry)
        super().closeEvent(event)


# =====================================================================
# MAIN PAGE
# =====================================================================
class RemoteAdministrationPage(QWidget):
    """
    Unified console for every managed host - both SSH-connected hosts
    and Sysible-agent-enrolled hosts - grouped by environment in a
    left-hand host list that matches the System Administration tools.

    Double-click a host to open its terminal in its own window (see
    TerminalPopout); for a host enrolled both ways, that window's
    connection picker chooses SSH or agent. The host list itself stays
    a simple single-select list - whether a row is agent, SSH, or both
    is shown in its [type] tag and in the detail line on the right, not
    baked into how you act on it.

    Connecting a *new* host here is always SSH/password-based ("Connect
    Host" below): the password is used once to install the controller's
    key, then discarded. Agent hosts are enrolled from Host Enrollment.

    The right-hand panel keeps the quick one-off command box and file
    transfer, both acting on the currently selected host.
    """

    def __init__(self):
        super().__init__()

        self.setWindowTitle("Remote Host Administration")
        self.resize(1180, 760)

        self.environments = []
        self._collapsed_envs = set()

        self.active_entry = None
        self.active_kind = None
        self.active_id = None
        self.active_label = None

        self.pending_quick_task = None     # agent one-off command
        self.pending_file_task = None      # agent file transfer

        self._terminals = {}               # host label -> TerminalPopout

        main = QVBoxLayout()
        self.setLayout(main)

        main.addLayout(make_page_header("Remote Host Administration"))

        body = QHBoxLayout()

        # ---------------- LEFT: host list (consistent column) ----------------
        self.host_list = QListWidget()
        connect_group_toggle(self.host_list)
        self.host_list.itemSelectionChanged.connect(self.on_host_selected)
        self.host_list.itemDoubleClicked.connect(self.open_terminal_for_item)

        btn_refresh = QPushButton("Refresh")
        btn_refresh.clicked.connect(self.load_hosts)
        btn_collapse_all, btn_expand_all = add_collapse_expand_buttons(self.host_list)

        host_panel = build_host_panel(
            "Managed Hosts (agent + SSH)",
            self.host_list,
            [
                [btn_refresh],
                [btn_collapse_all, btn_expand_all],
            ],
            width=340,  # wider so each host's IP fits next to it
        )
        body.addWidget(host_panel)

        # ---------------- RIGHT: detail + command + files + connect ----------------
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.addWidget(self._build_content_panel())
        body.addWidget(content, 1)

        main.addLayout(body, 1)

        # ---------------- timers / data ----------------
        self.quick_timer = QTimer()
        self.quick_timer.timeout.connect(self._poll_quick_task)

        self.file_poll_timer = QTimer()
        self.file_poll_timer.timeout.connect(self._poll_file_task)

        self.host_refresh_timer = QTimer()
        self.host_refresh_timer.timeout.connect(self.load_hosts)
        self.host_refresh_timer.start(HOST_REFRESH_MS)

        bus.host_removed.connect(self.load_hosts)

        self.load_hosts()

    @staticmethod
    def _divider():
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        return line

    def _build_content_panel(self):
        panel = QWidget()
        col = QVBoxLayout(panel)
        col.setContentsMargins(5, 0, 0, 0)

        self.active_host_label = QLabel("Viewing: (no host selected)")
        self.active_host_label.setStyleSheet("font-weight: bold;")
        col.addWidget(self.active_host_label)

        self.connection_label = QLabel("")
        self.connection_label.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        col.addWidget(self.connection_label)

        open_hint = QLabel("Double-click a host in the list to open its terminal in a new window.")
        theme.style_hint_label(open_hint)
        col.addWidget(open_hint)

        detail_buttons = QHBoxLayout()
        self.remove_host_btn = QPushButton("Remove Host")
        self.remove_host_btn.clicked.connect(self.delete_host)
        detail_buttons.addWidget(self.remove_host_btn)
        detail_buttons.addStretch()
        col.addLayout(detail_buttons)

        col.addWidget(self._divider())

        # ---- quick one-off command ----
        cmd_label = QLabel("Quick Command (runs once on the selected host)")
        cmd_label.setStyleSheet("font-weight: bold;")
        col.addWidget(cmd_label)

        cmd_row = QHBoxLayout()
        self.quick_input = QLineEdit()
        self.quick_input.setPlaceholderText("Select a host, type a command, press Enter...")
        self.quick_input.returnPressed.connect(self.run_quick_command)
        self.quick_run_btn = QPushButton("Run")
        self.quick_run_btn.clicked.connect(self.run_quick_command)
        quick_clear_btn = QPushButton("Clear")
        cmd_row.addWidget(self.quick_input, 4)
        cmd_row.addWidget(self.quick_run_btn)
        cmd_row.addWidget(quick_clear_btn)
        col.addLayout(cmd_row)

        mono = QFont()
        mono.setFamilies(["Menlo", "Consolas", "DejaVu Sans Mono", "Courier New"])
        mono.setStyleHint(QFont.Monospace)
        mono.setPointSize(11)

        self.quick_output = QTextEdit()
        self.quick_output.setReadOnly(True)
        self.quick_output.setFont(mono)
        self.quick_output.setMinimumHeight(160)
        quick_clear_btn.clicked.connect(self.quick_output.clear)
        col.addWidget(self.quick_output, 1)

        col.addWidget(self._divider())

        # ---- file transfer ----
        file_label = QLabel("File Transfer (selected host)")
        file_label.setStyleSheet("font-weight: bold;")
        col.addWidget(file_label)

        upload_row = QHBoxLayout()
        self.upload_local_path = QLineEdit()
        self.upload_local_path.setPlaceholderText("Local file to send...")
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_upload_file)
        self.upload_remote_path = QLineEdit()
        self.upload_remote_path.setPlaceholderText("Remote destination path (or directory)")
        self.upload_btn = QPushButton("Upload")
        self.upload_btn.clicked.connect(self.upload_file)
        upload_row.addWidget(self.upload_local_path, 3)
        upload_row.addWidget(browse_btn)
        upload_row.addWidget(self.upload_remote_path, 3)
        upload_row.addWidget(self.upload_btn)
        col.addLayout(upload_row)

        download_row = QHBoxLayout()
        self.download_remote_path = QLineEdit()
        self.download_remote_path.setPlaceholderText("Remote file path to fetch")
        self.download_btn = QPushButton("Download...")
        self.download_btn.clicked.connect(self.download_file)
        download_row.addWidget(self.download_remote_path, 3)
        download_row.addWidget(self.download_btn)
        col.addLayout(download_row)

        self.file_status = QLabel(
            f"Agent-host transfers are limited to ~{api.AGENT_FILE_TRANSFER_LIMIT_BYTES // 1000} KB; "
            "SSH hosts have no such limit."
        )
        self.file_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        self.file_status.setWordWrap(True)
        col.addWidget(self.file_status)

        col.addWidget(self._divider())

        # ---- connect a new SSH host ----
        enroll_label = QLabel("SSH to a New Host (Not Yet Joined)")
        enroll_label.setStyleSheet("font-weight: bold;")
        col.addWidget(enroll_label)

        enroll_hint = QLabel(
            "Only needed once per host. The password installs the controller key, then is "
            "discarded - after that the host appears in the list and connects with no password."
        )
        theme.style_hint_label(enroll_hint)
        col.addWidget(enroll_hint)

        enroll_row = QHBoxLayout()
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Host name")
        self.name_input.setMaximumWidth(150)
        self.ip_input = QLineEdit()
        self.ip_input.setPlaceholderText("IP address")
        self.ip_input.setMaximumWidth(130)
        self.user_input = QLineEdit()
        self.user_input.setPlaceholderText("Username")
        self.user_input.setMaximumWidth(110)
        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("SSH password")
        self.password_input.setEchoMode(QLineEdit.Password)
        self.password_input.setMaximumWidth(150)
        self.connect_env_combo = QComboBox()
        self.connect_env_combo.currentTextChanged.connect(self._handle_env_combo_change)
        self.connect_env_combo.setMaximumWidth(150)
        for w in [self.name_input, self.ip_input, self.user_input, self.password_input, self.connect_env_combo]:
            enroll_row.addWidget(w)
        col.addLayout(enroll_row)

        connect_buttons = QHBoxLayout()
        enroll_btn = QPushButton("Connect Host")
        enroll_btn.clicked.connect(self.connect_host)
        show_key_btn = QPushButton("Show Controller Public Key")
        show_key_btn.clicked.connect(self.show_controller_key)
        connect_buttons.addWidget(enroll_btn)
        connect_buttons.addWidget(show_key_btn)
        connect_buttons.addStretch()
        col.addLayout(connect_buttons)

        self.enroll_status = QLabel()
        self.enroll_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        self.enroll_status.setWordWrap(True)
        col.addWidget(self.enroll_status)

        return panel

    # =====================================================
    # HOSTS
    # =====================================================
    def load_hosts(self):
        prev_label = self.active_label

        try:
            entries = api.list_merged_hosts()
        except Exception as e:
            self.enroll_status.setStyleSheet(f"color: {STATUS_ERROR_COLOR};")
            self.enroll_status.setText(f"Could not load hosts: {e}")
            entries = []

        try:
            environments = api.list_environments()
        except Exception:
            environments = []

        self._populate_env_combos(environments)

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

        present_labels = set()
        for env in known_envs + extra_envs:
            self._add_host_header(env)
            for e in sorted(groups[env], key=lambda x: x["label"]):
                self._add_host_row(e)
                present_labels.add(e["label"])

        if unassigned:
            self._add_host_header(_UNASSIGNED_LABEL)
            for e in sorted(unassigned, key=lambda x: x["label"]):
                self._add_host_row(e)
                present_labels.add(e["label"])

        apply_collapse_state(self.host_list)
        self.host_list.blockSignals(False)

        # Keep the previously selected host selected through the refresh.
        if prev_label and prev_label in present_labels:
            self._reselect_label(prev_label)
        elif prev_label and prev_label not in present_labels:
            self._clear_active()

    def _add_host_header(self, text):
        item = make_group_header_item(text, collapsed=text in self._collapsed_envs)
        self.host_list.addItem(item)

    def _add_host_row(self, entry):
        ip = _row_ip(entry)
        text = f"    {entry['label']}  [{entry['type_text']}]"
        if ip:
            text += f"   {ip}"
        item = QListWidgetItem(text)
        item.setData(Qt.UserRole, entry)
        self.host_list.addItem(item)

    def _reselect_label(self, label):
        for i in range(self.host_list.count()):
            item = self.host_list.item(i)
            entry = item.data(Qt.UserRole)
            if entry and entry["label"] == label:
                self.host_list.blockSignals(True)
                self.host_list.setCurrentItem(item)
                self.host_list.blockSignals(False)
                return

    def _selected_entry(self):
        item = self.host_list.currentItem()
        if item is None:
            return None
        return item.data(Qt.UserRole)

    def _clear_active(self):
        self.active_entry = None
        self.active_kind = None
        self.active_id = None
        self.active_label = None
        self.active_host_label.setText("Viewing: (no host selected)")
        self.connection_label.setText("")

    def on_host_selected(self):
        entry = self._selected_entry()
        if entry is None:
            return  # header row

        self.active_entry = entry
        underlying = _default_underlying(entry)
        self.active_kind = underlying["kind"]
        self.active_id = underlying["id"]
        self.active_label = entry["label"]

        self.active_host_label.setText(f"Viewing: {entry['label']}  [{entry['type_text']}]")

        if entry["kind"] == "merged":
            self.connection_label.setText(
                "Available connections: Agent and SSH — double-click to open a terminal and pick one. "
                "Quick Command and File Transfer below use SSH."
            )
        elif entry["kind"] == "ssh":
            self.connection_label.setText("Connection: SSH.")
        else:
            # Agent-only host. The controller tries to auto-enroll every
            # agent host for a real SSH terminal (installs its key, checks
            # for sshd); surface where that stands so the operator knows
            # whether to expect a live terminal or to start sshd.
            ssh_state = entry.get("ssh_terminal_state")
            if ssh_state == "sshd_missing":
                self.connection_label.setText(
                    "Connection: Agent (queued commands). SSH terminal unavailable — "
                    "no SSH server is running on this host. Install/start sshd, then it "
                    "will be picked up automatically (or re-enroll the agent)."
                )
            elif ssh_state == "pending":
                self.connection_label.setText(
                    "Connection: Agent (queued commands). Setting up an SSH terminal for "
                    "this host — it will appear as Agent + SSH once the agent reports back."
                )
            else:
                self.connection_label.setText("Connection: Agent (queued commands).")

    def open_terminal_for_item(self, item):
        entry = item.data(Qt.UserRole)
        if entry is None:
            return  # header

        key = entry["label"]
        existing = self._terminals.get(key)
        if existing is not None:
            existing.show()
            existing.raise_()
            existing.activateWindow()
            return

        popout = TerminalPopout(entry, on_close=self._on_terminal_closed)
        self._terminals[key] = popout
        popout.show()
        popout.raise_()
        popout.activateWindow()

    def _on_terminal_closed(self, entry):
        self._terminals.pop(entry["label"], None)

    def _close_terminal_for(self, label):
        popout = self._terminals.get(label)
        if popout is not None:
            popout.close()

    # =====================================================
    # REMOVE HOST
    # =====================================================
    def delete_host(self):
        entry = self.active_entry
        if entry is None:
            QMessageBox.information(self, "No host selected", "Select a host first.")
            return

        # A host enrolled both ways gets a choice: drop just the SSH
        # connection, just the agent connection, or both. Removing one
        # leaves the host reachable via the other (it simply re-appears
        # in the list as a single-transport host).
        if entry["kind"] == "merged":
            agent_entry = entry["agent_entry"]
            ssh_entry = entry["ssh_entry"]

            box = QMessageBox(self)
            box.setIcon(QMessageBox.Question)
            box.setWindowTitle("Remove Connection")
            box.setText(
                f"{entry['label']} is enrolled both ways (Agent + SSH).\n"
                "What would you like to remove?"
            )
            ssh_btn = box.addButton("SSH connection only", QMessageBox.AcceptRole)
            agent_btn = box.addButton("Agent connection only", QMessageBox.AcceptRole)
            both_btn = box.addButton("Both connections", QMessageBox.DestructiveRole)
            box.addButton("Cancel", QMessageBox.RejectRole)
            box.exec()
            clicked = box.clickedButton()

            if clicked is ssh_btn:
                self._remove_connections(entry["label"], ssh_ids=[ssh_entry["id"]])
            elif clicked is agent_btn:
                self._remove_connections(entry["label"], agent_ids=[agent_entry["id"]])
            elif clicked is both_btn:
                self._remove_connections(
                    entry["label"],
                    agent_ids=[agent_entry["id"]],
                    ssh_ids=[ssh_entry["id"]],
                )
            return

        # Single-transport host.
        confirm = QMessageBox.question(self, "Confirm", f"Remove {entry['label']}?")
        if confirm != QMessageBox.Yes:
            return

        if entry["kind"] == "agent":
            self._remove_connections(entry["label"], agent_ids=[entry["id"]])
        else:
            self._remove_connections(entry["label"], ssh_ids=[entry["id"]])

    def _remove_connections(self, label, agent_ids=None, ssh_ids=None):
        """Drop the given agent and/or SSH connections for a host. Used
        for both whole-host removal and removing just one transport from
        a host that's enrolled both ways."""
        errors = []
        for agent_id in (agent_ids or []):
            try:
                api.disenroll_agent(agent_id)
            except Exception as e:
                errors.append(f"agent: {e}")
        for ssh_id in (ssh_ids or []):
            try:
                api.delete_host(ssh_id)
            except Exception as e:
                errors.append(f"ssh: {e}")

        if errors:
            QMessageBox.critical(self, "Error", "\n".join(errors))

        # Close any open terminal for this host - if a connection it was
        # using just went away, the session is no longer valid; the list
        # refresh below lets the user reopen against whatever remains.
        self._close_terminal_for(label)

        for agent_id in (agent_ids or []):
            bus.host_removed.emit(agent_id)
        for ssh_id in (ssh_ids or []):
            bus.host_removed.emit(ssh_id)

    # =====================================================
    # QUICK ONE-OFF COMMAND
    # =====================================================
    def run_quick_command(self):
        if not self.active_id:
            self.quick_output.append("[select a host first]")
            return

        cmd = self.quick_input.text().strip()
        if not cmd:
            return

        self.quick_input.clear()
        self.quick_output.append(f"{self.active_label} ({self.active_kind}) $ {cmd}")
        self._scroll_quick()

        if self.active_kind == "ssh":
            try:
                result = api.exec_remote(self.active_id, cmd)
            except Exception as e:
                self.quick_output.append(str(e))
                self._scroll_quick()
                return
            if result.get("stdout"):
                self.quick_output.append(result["stdout"].rstrip("\n"))
            if result.get("stderr"):
                self.quick_output.append(result["stderr"].rstrip("\n"))
            if result.get("code", 0) != 0:
                self.quick_output.append(f"[exit code {result.get('code')}]")
            self._scroll_quick()
            return

        # agent
        if self.pending_quick_task:
            self.quick_output.append("[a command is already running on this host - wait for it to finish]")
            return

        try:
            task_ids = api.queue_command_on_hosts([self.active_id], cmd)
        except Exception as e:
            self.quick_output.append(str(e))
            return

        task_id = task_ids.get(self.active_id)
        if task_id is None:
            self.quick_output.append("[failed to queue command on agent]")
            return

        self.quick_output.append("running...")
        self.quick_run_btn.setEnabled(False)
        self.pending_quick_task = {
            "host_id": self.active_id,
            "task_id": task_id,
            "deadline": time.time() + AGENT_CMD_TIMEOUT_S,
        }
        self.quick_timer.start(AGENT_CMD_POLL_MS)

    def _poll_quick_task(self):
        task = self.pending_quick_task
        if not task:
            self.quick_timer.stop()
            return

        try:
            raw = api.get_result_by_task(task["host_id"], task["task_id"])
        except Exception:
            raw = None

        if raw is None:
            if time.time() > task["deadline"]:
                self.quick_output.append("[timed out waiting for agent to report back]")
                self.pending_quick_task = None
                self.quick_timer.stop()
                self.quick_run_btn.setEnabled(True)
            return

        output = api.parse_task_output(raw)
        if output:
            if output.get("stdout"):
                self.quick_output.append(output["stdout"].rstrip("\n"))
            if output.get("stderr"):
                self.quick_output.append(output["stderr"].rstrip("\n"))
            if output.get("returncode", 0) != 0:
                self.quick_output.append(f"[exit code {output.get('returncode')}]")
        else:
            self.quick_output.append("[agent reported no usable output]")

        self.pending_quick_task = None
        self.quick_timer.stop()
        self.quick_run_btn.setEnabled(True)
        self._scroll_quick()

    def _scroll_quick(self):
        sb = self.quick_output.verticalScrollBar()
        sb.setValue(sb.maximum())

    # =====================================================
    # ENVIRONMENTS (connect form)
    # =====================================================
    def _populate_env_combos(self, environments):
        self.environments = environments
        combo = self.connect_env_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(environments)
        combo.addItem(_NEW_ENV_OPTION)
        combo.blockSignals(False)

    def _handle_env_combo_change(self, text):
        if text != _NEW_ENV_OPTION:
            return
        combo = self.sender()
        name, ok = QInputDialog.getText(self, "New Environment", "Environment name:")
        if not ok or not name.strip():
            self._populate_env_combos(self.environments)
            return
        name = name.strip()
        try:
            environments = api.create_environment(name)
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            self._populate_env_combos(self.environments)
            return
        self._populate_env_combos(environments)
        if combo is not None:
            combo.setCurrentText(name)

    # =====================================================
    # CONNECT NEW SSH HOST
    # =====================================================
    def connect_host(self):
        name = self.name_input.text().strip()
        ip = self.ip_input.text().strip()
        user = self.user_input.text().strip() or "root"
        password = self.password_input.text()

        environment = self.connect_env_combo.currentText()
        if environment == _NEW_ENV_OPTION:
            environment = ""

        if not name or not ip or not password:
            self.enroll_status.setStyleSheet(f"color: {STATUS_ERROR_COLOR};")
            self.enroll_status.setText("Host name, IP, and password are required.")
            return

        try:
            api.enroll_ssh(name, ip, user, password, environment)
        except Exception as e:
            self.enroll_status.setStyleSheet(f"color: {STATUS_ERROR_COLOR};")
            self.enroll_status.setText(f"Connection failed: {e}")
            return

        self.password_input.clear()
        self.enroll_status.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
        self.enroll_status.setText(f"'{name}' connected — double-click it in the list to open a terminal.")
        self.load_hosts()

    def show_controller_key(self):
        try:
            key = api.get_controller_key()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return
        QMessageBox.information(
            self,
            "Sysible Controller Public Key",
            "This is the same key Connect Host installs automatically. "
            "Only needed if you'd rather install it manually (e.g. baked "
            "into a host image, or a host with password auth disabled):\n\n"
            + key,
        )

    # =====================================================
    # FILE TRANSFER (selected host)
    # =====================================================
    def _browse_upload_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose file to upload")
        if path:
            self.upload_local_path.setText(path)

    def upload_file(self):
        if not self.active_id:
            QMessageBox.information(self, "No host selected", "Select a host first.")
            return

        local_path = self.upload_local_path.text().strip()
        remote_path = self.upload_remote_path.text().strip()

        if not local_path or not remote_path:
            QMessageBox.information(
                self, "Missing info", "Choose a local file and a remote destination path."
            )
            return

        if not os.path.isfile(local_path):
            QMessageBox.critical(self, "Error", f"Local file not found: {local_path}")
            return

        if self.pending_file_task:
            QMessageBox.information(
                self, "Transfer in progress", "Wait for the current file transfer to finish first."
            )
            return

        if self.active_kind == "ssh":
            self.file_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
            self.file_status.setText(f"Uploading to {self.active_label}...")
            QApplication.processEvents()
            try:
                result = api.upload_file_ssh(self.active_id, local_path, remote_path)
            except Exception as e:
                self.file_status.setStyleSheet(f"color: {STATUS_ERROR_COLOR};")
                self.file_status.setText(f"Upload failed: {e}")
                return
            self.file_status.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
            self.file_status.setText(
                f"Uploaded to {self.active_label}:{result.get('remote_path', remote_path)} "
                f"({result.get('size', 0)} bytes)."
            )
            return

        try:
            result = api.queue_agent_upload(self.active_id, local_path, remote_path)
        except Exception as e:
            self.file_status.setStyleSheet(f"color: {STATUS_ERROR_COLOR};")
            self.file_status.setText(f"Upload failed: {e}")
            return

        if result["error"]:
            self.file_status.setStyleSheet(f"color: {STATUS_ERROR_COLOR};")
            self.file_status.setText(result["error"])
            return

        self.file_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        self.file_status.setText(f"Uploading to {self.active_label} (agent)...")
        self.pending_file_task = {
            "direction": "upload",
            "host_id": self.active_id,
            "task_id": result["task_id"],
            "label": self.active_label,
            "remote_path": remote_path,
            "deadline": time.time() + AGENT_CMD_TIMEOUT_S,
        }
        self.file_poll_timer.start(AGENT_CMD_POLL_MS)

    def download_file(self):
        if not self.active_id:
            QMessageBox.information(self, "No host selected", "Select a host first.")
            return

        remote_path = self.download_remote_path.text().strip()
        if not remote_path:
            QMessageBox.information(self, "Missing info", "Enter a remote file path to download.")
            return

        if self.pending_file_task:
            QMessageBox.information(
                self, "Transfer in progress", "Wait for the current file transfer to finish first."
            )
            return

        suggested_name = remote_path.rstrip("/").rsplit("/", 1)[-1] or "download"
        local_path, _ = QFileDialog.getSaveFileName(self, "Save downloaded file as", suggested_name)
        if not local_path:
            return

        if self.active_kind == "ssh":
            self.file_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
            self.file_status.setText(f"Downloading from {self.active_label}...")
            QApplication.processEvents()
            try:
                api.download_file_ssh(self.active_id, remote_path, local_path)
            except Exception as e:
                self.file_status.setStyleSheet(f"color: {STATUS_ERROR_COLOR};")
                self.file_status.setText(f"Download failed: {e}")
                return
            self.file_status.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
            self.file_status.setText(f"Saved to {local_path}.")
            return

        try:
            result = api.queue_agent_download(self.active_id, remote_path)
        except Exception as e:
            self.file_status.setStyleSheet(f"color: {STATUS_ERROR_COLOR};")
            self.file_status.setText(f"Download failed: {e}")
            return

        if result["error"]:
            self.file_status.setStyleSheet(f"color: {STATUS_ERROR_COLOR};")
            self.file_status.setText(result["error"])
            return

        self.file_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        self.file_status.setText(f"Downloading from {self.active_label} (agent)...")
        self.pending_file_task = {
            "direction": "download",
            "host_id": self.active_id,
            "task_id": result["task_id"],
            "label": self.active_label,
            "local_path": local_path,
            "deadline": time.time() + AGENT_CMD_TIMEOUT_S,
        }
        self.file_poll_timer.start(AGENT_CMD_POLL_MS)

    def _poll_file_task(self):
        task = self.pending_file_task
        if not task:
            self.file_poll_timer.stop()
            return

        if time.time() > task["deadline"]:
            self.file_status.setStyleSheet(f"color: {STATUS_ERROR_COLOR};")
            self.file_status.setText(f"[timed out waiting for agent to confirm {task['direction']}]")
            self.pending_file_task = None
            self.file_poll_timer.stop()
            return

        if task["direction"] == "upload":
            try:
                result = api.poll_agent_upload(task["host_id"], task["task_id"])
            except Exception:
                result = None
            if result is None:
                return
            if result["error"]:
                self.file_status.setStyleSheet(f"color: {STATUS_ERROR_COLOR};")
                self.file_status.setText(f"Upload to {task['label']} failed: {result['error']}")
            else:
                shown_path = result.get("remote_path") or task["remote_path"]
                self.file_status.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
                self.file_status.setText(f"Uploaded to {task['label']}:{shown_path}.")
            self.pending_file_task = None
            self.file_poll_timer.stop()
            return

        try:
            result = api.poll_agent_download(task["host_id"], task["task_id"], task["local_path"])
        except Exception:
            result = None
        if result is None:
            return
        if result["error"]:
            self.file_status.setStyleSheet(f"color: {STATUS_ERROR_COLOR};")
            self.file_status.setText(f"Download from {task['label']} failed: {result['error']}")
        else:
            self.file_status.setStyleSheet(f"color: {STATUS_SUCCESS_COLOR};")
            self.file_status.setText(f"Saved to {task['local_path']}.")
        self.pending_file_task = None
        self.file_poll_timer.stop()

    def closeEvent(self, event):
        # Close any terminal popouts this page spawned so their SSH PTY
        # sessions get torn down rather than left running on the backend.
        for popout in list(self._terminals.values()):
            popout.close()
        self._terminals.clear()
        super().closeEvent(event)
