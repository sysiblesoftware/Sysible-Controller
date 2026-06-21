import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget, QTableWidgetItem,
    QAbstractItemView, QPushButton, QLineEdit, QTextEdit, QComboBox, QInputDialog,
    QMessageBox, QApplication, QFileDialog, QFrame
)
from PySide6.QtCore import Qt, QTimer, QObject, Signal
from PySide6.QtGui import QFont, QKeySequence

from client import api
from client.branding import make_page_header
from client.events import bus
from client import theme
from client.theme import STATUS_NEUTRAL_COLOR, STATUS_SUCCESS_COLOR, STATUS_ERROR_COLOR

_NEW_ENV_OPTION = "+ New environment..."
_UNASSIGNED_LABEL = "Unassigned"

AGENT_CMD_POLL_MS = 1500
AGENT_CMD_TIMEOUT_S = 30

# History: this used to be a 300ms, then a 60ms, fixed-interval
# QTimer driving a *synchronous* read_terminal()/write_terminal() call
# straight on the Qt GUI thread. That kept every poll cheap, but it
# capped responsiveness at the timer interval and meant every single
# keystroke blocked the whole app's UI thread for one full network
# round trip before the keypress even reached the remote shell.
#
# Now: backend/remote_routes.py's /terminal/read does a real
# server-side long-poll (blocks briefly waiting for output instead of
# returning empty right away - see TERMINAL_LONG_POLL_S there), and
# _TerminalIO below runs every read/write on a background thread pool
# instead of the GUI thread, so that backend-side wait never freezes
# the app. The read loop is now completion-driven (each read
# immediately kicks off the next one) rather than timer-driven, so
# new remote output reaches the screen the instant the backend wakes
# up - not on the next fixed tick. SSH_TERMINAL_POLL_MS now only
# governs the brief backoff after a transient read error, so a
# persistent failure (host rebooting, network blip) doesn't spin the
# loop.
SSH_TERMINAL_POLL_MS = 60


class _TerminalIO(QObject):
    """Runs the blocking SSH terminal read/write HTTP calls on a
    background thread pool instead of the Qt GUI thread, so neither a
    keystroke nor the backend's long-poll wait (see
    backend/remote_routes.py's TERMINAL_LONG_POLL_S) can ever freeze
    the app. Qt automatically queues a Signal emitted from a worker
    thread onto the thread its receiver lives on, so the connected
    slots below are still safe to touch widgets directly - no manual
    locking or invokeMethod needed."""

    read_done = Signal(str, dict)   # host_id, result
    read_failed = Signal(str)       # host_id
    write_failed = Signal(str)      # error text

    def __init__(self):
        super().__init__()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ssh-term-io")
        self._reads_inflight = set()

    def submit_read(self, host_id):
        # Guard against a second read for the same host going out
        # before the first one's reply comes back - they'd both be
        # recv()-ing the same channel concurrently, which is exactly
        # the kind of overlap the completion-driven loop is meant to
        # avoid.
        if host_id in self._reads_inflight:
            return
        self._reads_inflight.add(host_id)

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
        if ok:
            self.read_done.emit(host_id, result)
        else:
            self.read_failed.emit(host_id)

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

# Strips ANSI/VT100 escape sequences (cursor moves, color codes, etc.)
# before display - the output pane is a plain scrolling text widget,
# not a real terminal emulator, so sequences that redraw in place
# (progress bars, full-screen apps like vim/top) will still scroll
# instead of rendering correctly. Plain shell I/O, including sudo
# password prompts, displays correctly.
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
    would send for it. Returns None for keys that carry no input
    (modifier-only presses, function keys we don't map, etc.)."""
    key = event.key()
    modifiers = event.modifiers()

    if modifiers & Qt.ControlModifier and Qt.Key_A <= key <= Qt.Key_Z:
        return chr(key - Qt.Key_A + 1)  # Ctrl+A=0x01 ... Ctrl+C=0x03 ... Ctrl+Z=0x1a

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
    """QLineEdit with bash-style Up/Down command history, so the
    command box at the bottom of the terminal actually feels like
    one."""

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
    being inserted locally - what shows up on screen is only ever
    what the remote shell echoes back (or doesn't, e.g. while typing
    a sudo password), exactly like a real terminal. With no session
    attached (on_key_data is None) it behaves like an ordinary
    read-only log."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.on_key_data = None

    def keyPressEvent(self, event):
        if self.on_key_data is None:
            super().keyPressEvent(event)
            return

        # Only treat the platform Copy shortcut (Ctrl+C on
        # Windows/Linux, which collides with the terminal interrupt
        # byte) as "copy" when there's actually a selection to copy -
        # otherwise it falls through to _key_event_to_bytes below,
        # which sends the real \x03 interrupt. Without this, Ctrl+C
        # with nothing selected silently did nothing instead of
        # interrupting the remote command (see also the dedicated
        # "Send Ctrl+C" button for a keyboard-independent way to do
        # the same thing).
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
        """Render incoming remote bytes in-place rather than via
        append() (which always starts a new paragraph) - interprets
        backspace/delete the way a real terminal would (erase the
        previous on-screen character) instead of showing it as a
        stray control character."""
        cursor = self.textCursor()
        cursor.movePosition(cursor.End)

        for ch in _strip_ansi(text):
            if ch == "\r":
                continue
            elif ch in ("\x08", "\x7f"):
                cursor.deletePreviousChar()
            else:
                cursor.insertText(ch)

        self.setTextCursor(cursor)


# Moved into client/api.py as merge_duplicate_host_entries() so System
# Health & Logs and User & Group Administration (both of which list
# hosts via api.list_merged_hosts()) get the same agent+SSH dedup this
# page pioneered, instead of each page growing its own copy. Aliased
# back to the original name so the rest of this file (display_entries
# = _merge_entries(entries) below) didn't need to change.
_merge_entries = api.merge_duplicate_host_entries


class RemoteAdministrationPage(QWidget):
    """
    Unified administration console for every managed host - both
    SSH-connected hosts and Sysible-agent-enrolled hosts - grouped by
    environment to match Host Enrollment and User Administration.

    Connecting a *new* host here is always SSH/password-based
    ("Connect Host (SSH)" below): name/ip/user/password -> the backend
    installs Sysible's own controller SSH key on the target using that
    password once, then discards it. Agent-enrolled hosts are never
    added from here - enroll those from Host Enrollment instead
    (token-based, no password involved) - they just show up
    automatically once enrolled.

    The Terminal works for either kind, but not identically under the
    hood: SSH hosts get a real interactive session - a persistent
    PTY-backed shell over the stored key (see backend/remote_routes.py's
    /terminal/* endpoints), polled and streamed live, so sudo prompts,
    vim, top, etc. behave like a genuine terminal. Agent hosts have no
    direct connection to the controller, so a command there is queued
    instead and this page polls for the result until the agent reports
    back (or it times out) - a real interactive session isn't possible
    over that polling model.
    """

    def __init__(self):
        super().__init__()

        self.setWindowTitle("Remote Host Administration")

        self.environments = []

        self.active_kind = None   # "agent" or "ssh"
        self.active_id = None     # host_id (agent) or name (ssh)
        self.active_label = None

        # When the selected row is a merged (Agent + SSH) host, this
        # holds that merged entry so the connection-type picker below
        # can re-derive the agent/ssh sub-entry on demand; None for a
        # plain (non-merged) row. connection_type_host_label tracks
        # which merged host the picker's current selection belongs to,
        # so re-clicking the same merged row keeps whatever connection
        # type you last explicitly picked for it instead of silently
        # resetting back to SSH every time.
        self.merged_entry_for_selection = None
        self.connection_type_host_label = None

        self.pending_agent_task = None  # {"host_id", "task_id", "deadline"}
        self.pending_file_task = None   # {"direction", "host_id", "task_id", "label", "deadline", ...}

        layout = QVBoxLayout()

        layout.addLayout(make_page_header("Remote Host Administration"))

        # =====================================================
        # HOSTS
        # =====================================================
        hosts_label = QLabel("Managed Hosts (grouped by environment)")
        hosts_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(hosts_label)

        # Columns instead of one long "name (kind detail)" string per
        # row - the hostname used to get repeated almost verbatim
        # across an agent row and an SSH row for the very same
        # physical machine, which read as cluttered. Each fact (host,
        # connection type, address) now has its own column instead.
        self.host_list = QTableWidget(0, 3)
        self.host_list.setHorizontalHeaderLabels(["Host", "Type", "Address"])
        self.host_list.verticalHeader().setVisible(False)
        self.host_list.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.host_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.host_list.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.host_list.horizontalHeader().setStretchLastSection(True)
        self.host_list.setColumnWidth(1, 70)
        self.host_list.setFixedHeight(140)
        self.host_list.itemSelectionChanged.connect(self.on_host_selected)
        layout.addWidget(self.host_list)

        quick_connect_hint = QLabel(
            "Click a host above to connect instantly - already-enrolled hosts "
            "(agent or SSH) need no password re-entry."
        )
        theme.style_hint_label(quick_connect_hint)
        layout.addWidget(quick_connect_hint)

        host_buttons = QHBoxLayout()

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.load_hosts)

        delete_btn = QPushButton("Remove Host")
        delete_btn.clicked.connect(self.delete_host)

        show_key_btn = QPushButton("Show Sysible Controller Public Key")
        show_key_btn.clicked.connect(self.show_controller_key)

        host_buttons.addWidget(refresh_btn)
        host_buttons.addWidget(delete_btn)
        host_buttons.addWidget(show_key_btn)

        layout.addLayout(host_buttons)

        layout.addWidget(self._divider())

        # =====================================================
        # SSH TO A NEW HOST (NOT YET JOINED) - the one-time key handoff
        # happens automatically behind this single button. This section
        # is only for hosts that aren't in the Managed Hosts table above
        # yet. This is *not* the same as Host Enrollment's token-based
        # agent enrollment - see the class docstring.
        # =====================================================
        enroll_label = QLabel("SSH to a New Host (Not Yet Joined)")
        enroll_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(enroll_label)

        enroll_hint = QLabel(
            "Only needed once per host. Password installs the controller key, then is "
            "discarded - after that, the host appears above and connects with no password."
        )
        theme.style_hint_label(enroll_hint)
        layout.addWidget(enroll_hint)

        enroll_row = QHBoxLayout()

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Host name")
        self.name_input.setMaximumWidth(160)

        self.ip_input = QLineEdit()
        self.ip_input.setPlaceholderText("IP address")
        self.ip_input.setMaximumWidth(140)

        self.user_input = QLineEdit()
        self.user_input.setPlaceholderText("Username")
        self.user_input.setMaximumWidth(120)

        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("SSH password")
        self.password_input.setEchoMode(QLineEdit.Password)
        self.password_input.setMaximumWidth(160)

        self.connect_env_combo = QComboBox()
        self.connect_env_combo.currentTextChanged.connect(self._handle_env_combo_change)
        self.connect_env_combo.setMaximumWidth(160)

        for w in [self.name_input, self.ip_input, self.user_input, self.password_input, self.connect_env_combo]:
            enroll_row.addWidget(w)

        layout.addLayout(enroll_row)

        enroll_btn = QPushButton("Connect Host")
        enroll_btn.clicked.connect(self.connect_host)
        layout.addWidget(enroll_btn)

        self.enroll_status = QLabel()
        self.enroll_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        layout.addWidget(self.enroll_status)

        # =====================================================
        # TERMINAL
        # =====================================================
        self.terminal_label = QLabel("Terminal — (no host selected)")
        self.terminal_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.terminal_label)

        # Shown only when the selected row is a host enrolled BOTH ways
        # (merged Agent + SSH). Previously this case silently always
        # used SSH for the terminal with no way to tell, and no way to
        # fall back to the agent connection on a host where SSH itself
        # doesn't work - this makes the choice explicit and switchable
        # instead of an invisible, fixed decision the program made for
        # you.
        connection_type_row = QHBoxLayout()
        self.connection_type_label = QLabel("Connection:")
        self.connection_type_combo = QComboBox()
        self.connection_type_combo.addItem("SSH (interactive terminal)", "ssh")
        self.connection_type_combo.addItem("Agent (queued commands)", "agent")
        self.connection_type_combo.currentIndexChanged.connect(self._on_connection_type_changed)
        connection_type_row.addWidget(self.connection_type_label)
        connection_type_row.addWidget(self.connection_type_combo)
        connection_type_row.addStretch()
        layout.addLayout(connection_type_row)

        self.output = _LiveTerminalOutput()
        self.output.setReadOnly(True)

        # A bare QFont("Courier New") only actually resolves on Windows -
        # on macOS/Linux Qt silently substitutes some generic font
        # instead, which read as inconsistent/"unappealing" depending on
        # platform. setFamilies() tries each in turn and only falls back
        # to the Monospace style hint below if none of them exist.
        mono = QFont()
        mono.setFamilies(["Menlo", "Consolas", "DejaVu Sans Mono", "Courier New"])
        mono.setStyleHint(QFont.Monospace)
        mono.setPointSize(11)
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
        self.output.setMinimumHeight(220)
        layout.addWidget(self.output)

        self.ssh_hint = QLabel(
            "SSH terminal is live - click into the panel above and type "
            "directly (sudo prompts, vim, Ctrl+C, arrow keys, etc. all work)."
        )
        theme.style_hint_label(self.ssh_hint)
        self.ssh_hint.setVisible(False)
        layout.addWidget(self.ssh_hint)

        cmd_row = QHBoxLayout()

        self.cmd_input = _TerminalInput()
        self.cmd_input.setFont(mono)
        self.cmd_input.setPlaceholderText("Select a host above, then type a command and press Enter...")
        self.cmd_input.setEnabled(False)
        self.cmd_input.returnPressed.connect(self.run_command)

        self.run_btn = QPushButton("Run")
        self.run_btn.clicked.connect(self.run_command)

        # Only shown during a live SSH session (swaps places with
        # cmd_input/run_btn, which aren't used in that mode - see
        # _start_ssh_session/_end_ssh_session) - a guaranteed way to
        # send SIGINT to a hung/runaway remote command without relying
        # on the Ctrl+C keyboard shortcut landing correctly.
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

        # =====================================================
        # FILE TRANSFER (selected host above - same active_kind/
        # active_id the Terminal section tracks, so there's no separate
        # host picker here). SSH hosts transfer over SFTP with no size
        # limit; agent hosts go through the same queued-task channel as
        # the command box above, capped at AGENT_FILE_TRANSFER_LIMIT_BYTES
        # (see client/api.py).
        # =====================================================
        file_label = QLabel("File Transfer (selected host)")
        file_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(file_label)

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

        layout.addLayout(upload_row)

        download_row = QHBoxLayout()

        self.download_remote_path = QLineEdit()
        self.download_remote_path.setPlaceholderText("Remote file path to fetch")

        self.download_btn = QPushButton("Download...")
        self.download_btn.clicked.connect(self.download_file)

        download_row.addWidget(self.download_remote_path, 3)
        download_row.addWidget(self.download_btn)

        layout.addLayout(download_row)

        self.file_status = QLabel(
            f"Agent-host transfers are limited to ~{api.AGENT_FILE_TRANSFER_LIMIT_BYTES // 1000} KB; "
            "SSH hosts have no such limit."
        )
        self.file_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        layout.addWidget(self.file_status)

        self.setLayout(layout)

        self.agent_poll_timer = QTimer()
        self.agent_poll_timer.timeout.connect(self._poll_agent_task)

        self._ssh_session_active = False
        self._terminal_io = _TerminalIO()
        self._terminal_io.read_done.connect(self._on_ssh_read_done)
        self._terminal_io.read_failed.connect(self._on_ssh_read_failed)
        self._terminal_io.write_failed.connect(self._on_ssh_write_failed)

        self._ssh_retry_timer = QTimer()
        self._ssh_retry_timer.setSingleShot(True)
        self._ssh_retry_timer.timeout.connect(self._resume_ssh_read_loop)

        self.file_poll_timer = QTimer()
        self.file_poll_timer.timeout.connect(self._poll_file_task)

        bus.host_removed.connect(self.load_hosts)

        self._set_connection_type_visible(False)

        self.load_hosts()

    @staticmethod
    def _divider():
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        return line

    # =====================================================
    # HOSTS (agent hosts + SSH hosts, merged and grouped by
    # environment)
    # =====================================================
    def load_hosts(self):
        self.host_list.setRowCount(0)

        try:
            agents = api.get_agents()
        except Exception as e:
            agents = []
            self.enroll_status.setStyleSheet(f"color: {STATUS_ERROR_COLOR};")
            self.enroll_status.setText(f"Could not load agent hosts: {e}")

        try:
            ssh_hosts = api.list_hosts()
        except Exception as e:
            ssh_hosts = {}
            self.enroll_status.setStyleSheet(f"color: {STATUS_ERROR_COLOR};")
            self.enroll_status.setText(f"Could not load SSH hosts: {e}")

        try:
            environments = api.list_environments()
        except Exception:
            environments = []

        self._populate_env_combos(environments)

        entries = []

        for a in agents:
            host_id = a.get("host_id")
            entries.append({
                "kind": "agent",
                "id": host_id,
                "label": a.get("hostname") or host_id,
                "type_text": "Agent",
                # Agent-reported local IP (host_agent/agent.py's
                # _local_ip(), sent on enroll/heartbeat) when available -
                # falls back to the opaque host_id for agents that
                # haven't reported one yet (older agent build, or no
                # heartbeat received since upgrading the controller).
                "address": a.get("ip") or host_id,
                "environment": a.get("environment") or "",
            })

        for name, h in ssh_hosts.items():
            entries.append({
                "kind": "ssh",
                "id": name,
                "label": name,
                "type_text": "SSH",
                "address": f"{h.get('user', 'root')}@{h.get('ip', '?')}",
                "environment": h.get("environment") or "",
            })

        # current_ids must reflect the real, unmerged backend state (a
        # merged row's own "kind" is "merged", not "agent"/"ssh") so the
        # "did the active connection disappear" check below still works.
        current_ids = {(e["kind"], e["id"]) for e in entries}

        display_entries = _merge_entries(entries)

        groups = {}
        for e in display_entries:
            groups.setdefault(e["environment"], []).append(e)

        known_envs = [env for env in environments if env in groups]
        extra_envs = sorted(env for env in groups if env and env not in environments)
        unassigned = groups.get("", [])

        for env in known_envs + extra_envs:
            self._add_header(env)
            for e in sorted(groups[env], key=lambda x: x["label"]):
                self._add_host_row(e)

        if unassigned:
            self._add_header(_UNASSIGNED_LABEL)
            for e in sorted(unassigned, key=lambda x: x["label"]):
                self._add_host_row(e)

        if self.active_id is not None and (self.active_kind, self.active_id) not in current_ids:
            self._cancel_pending_agent_task()
            self._end_ssh_session()
            self.active_kind = None
            self.active_id = None
            self.active_label = None
            self.merged_entry_for_selection = None
            self.connection_type_host_label = None
            self._set_connection_type_visible(False)
            self.terminal_label.setText("Terminal — (no host selected)")
            self.cmd_input.setEnabled(False)
            self.cmd_input.setPlaceholderText("Select a host above, then type a command and press Enter...")

    def _add_header(self, text):
        row = self.host_list.rowCount()
        self.host_list.insertRow(row)

        item = QTableWidgetItem(text.upper())
        item.setFlags(Qt.NoItemFlags)

        font = item.font()
        font.setBold(True)
        item.setFont(font)

        self.host_list.setItem(row, 0, item)
        self.host_list.setSpan(row, 0, 1, 3)

    def _add_host_row(self, entry):
        row = self.host_list.rowCount()
        self.host_list.insertRow(row)

        host_item = QTableWidgetItem(entry["label"])
        host_item.setData(Qt.UserRole, entry)
        self.host_list.setItem(row, 0, host_item)
        self.host_list.setItem(row, 1, QTableWidgetItem(entry["type_text"]))
        self.host_list.setItem(row, 2, QTableWidgetItem(entry["address"]))

    def _selected_entry(self):
        row = self.host_list.currentRow()
        if row < 0:
            return None

        item = self.host_list.item(row, 0)
        return item.data(Qt.UserRole) if item else None

    def on_host_selected(self):
        entry = self._selected_entry()

        if entry is None:
            return  # environment header row, not a host

        # A merged row has both an agent and an SSH connection for the
        # same host. Rather than silently always picking SSH (the old
        # behavior - confusing when SSH is the side that's broken),
        # show the connection-type picker and let the user choose. The
        # picker defaults to SSH only the first time a given merged
        # host is selected; re-clicking the same merged row keeps
        # whatever connection type was last explicitly picked for it.
        if entry["kind"] == "merged":
            self.merged_entry_for_selection = entry
            self._set_connection_type_visible(True)
            if self.connection_type_host_label != entry["label"]:
                self.connection_type_host_label = entry["label"]
                self.connection_type_combo.blockSignals(True)
                self.connection_type_combo.setCurrentIndex(0)
                self.connection_type_combo.blockSignals(False)
            underlying = self._merged_underlying_for_combo()
        else:
            self.merged_entry_for_selection = None
            self.connection_type_host_label = None
            self._set_connection_type_visible(False)
            underlying = entry

        self._switch_active_connection(underlying, entry["label"])

    def _set_connection_type_visible(self, visible):
        self.connection_type_label.setVisible(visible)
        self.connection_type_combo.setVisible(visible)

    def _merged_underlying_for_combo(self):
        entry = self.merged_entry_for_selection
        if entry is None:
            return None
        choice = self.connection_type_combo.currentData()
        if choice == "agent":
            return entry["agent_entry"]
        return entry["ssh_entry"]

    def _on_connection_type_changed(self, index):
        if self.merged_entry_for_selection is None:
            return
        self.connection_type_host_label = self.merged_entry_for_selection["label"]
        underlying = self._merged_underlying_for_combo()
        self._switch_active_connection(underlying, self.merged_entry_for_selection["label"])

    def _switch_active_connection(self, underlying, label):
        if underlying is None:
            return

        if underlying["kind"] == self.active_kind and underlying["id"] == self.active_id:
            return  # re-selecting the already-active connection - leave its session alone

        self._cancel_pending_agent_task()
        self._end_ssh_session()

        self.active_kind = underlying["kind"]
        self.active_id = underlying["id"]
        self.active_label = label

        if underlying["kind"] == "ssh":
            self._start_ssh_session(underlying)
        else:
            self.terminal_label.setText(f"Terminal — {label} [agent]")
            self.cmd_input.setEnabled(True)
            self.cmd_input.setPlaceholderText(f"{label} $ ")
            self.cmd_input.setFocus()

    # =====================================================
    # SSH LIVE TERMINAL SESSION (persistent PTY - see
    # backend/remote_routes.py's /terminal/* endpoints)
    # =====================================================
    def _start_ssh_session(self, entry):
        self.terminal_label.setText(f"Terminal — {self.active_label} [SSH] (connecting...)")
        self.cmd_input.setEnabled(False)
        self.run_btn.setEnabled(False)
        QApplication.processEvents()

        try:
            api.open_terminal(entry["id"])
        except Exception as e:
            self.output.append(f"[could not open terminal session: {e}]")
            self.terminal_label.setText("Terminal — (no host selected)")
            self.cmd_input.setEnabled(False)
            self.run_btn.setEnabled(True)
            # Clear active_* so reselecting this same row retries the
            # connection instead of being treated as a no-op by the
            # "already active" early return in _switch_active_connection().
            self.active_kind = None
            self.active_id = None
            self.active_label = None
            return

        self.terminal_label.setText(f"Terminal — {self.active_label} [SSH] (live)")
        self.cmd_input.hide()
        self.run_btn.hide()
        self.interrupt_btn.setVisible(True)
        self.ssh_hint.setVisible(True)
        self.output.setReadOnly(False)
        self.output.on_key_data = self._send_terminal_input
        self.output.setFocus()
        self._ssh_session_active = True
        self._terminal_io.submit_read(self.active_id)

    def _send_terminal_input(self, data):
        self._terminal_io.submit_write(self.active_id, data)

    def send_interrupt(self):
        """Send Ctrl+C (\\x03) into the live SSH session - the button
        equivalent of pressing it on the keyboard, for a hung/runaway
        remote command (or anyone whose Ctrl+C got eaten as a Copy
        shortcut before the keyPressEvent fix above)."""
        if self.active_kind == "ssh" and self.output.on_key_data is not None:
            self._send_terminal_input("\x03")
            self.output.setFocus()

    def _on_ssh_read_done(self, host_id, result):
        # The session may have moved on (host switched, ended, etc.)
        # since this particular read was issued - drop a stale reply
        # rather than acting on it or restarting its loop.
        if not self._ssh_session_active or self.active_kind != "ssh" or self.active_id != host_id:
            return

        if result.get("data"):
            self.output.append_terminal_text(result["data"])
            self._scroll_to_bottom()

        if result.get("closed"):
            self.output.append_terminal_text("\n[remote session ended - select the host again to reconnect]\n")
            self._end_ssh_session(close_remote=False)
            # Clear active_* so re-clicking the same row is treated as
            # a fresh selection (and actually reopens a session)
            # instead of the early-return "already active" no-op in
            # on_host_selected().
            self.active_kind = None
            self.active_id = None
            self.active_label = None
            self.terminal_label.setText("Terminal — (no host selected)")
            return

        # Go again immediately - the backend's /terminal/read now
        # long-polls (see TERMINAL_LONG_POLL_S in
        # backend/remote_routes.py), so this naturally paces itself:
        # it comes back the instant new output exists, or after a
        # bounded idle wait either way, instead of needing a fixed
        # QTimer tick to notice.
        self._terminal_io.submit_read(host_id)

    def _on_ssh_read_failed(self, host_id):
        if not self._ssh_session_active or self.active_kind != "ssh" or self.active_id != host_id:
            return
        # Transient (network blip, host rebooting) - brief backoff so
        # the loop doesn't spin, then try again.
        self._ssh_retry_timer.start(SSH_TERMINAL_POLL_MS)

    def _resume_ssh_read_loop(self):
        if self._ssh_session_active and self.active_kind == "ssh" and self.active_id:
            self._terminal_io.submit_read(self.active_id)

    def _on_ssh_write_failed(self, err):
        self.output.append_terminal_text(f"\n[write failed: {err}]\n")

    def _end_ssh_session(self, close_remote=True):
        """Leave SSH-live mode (called when switching to another host,
        deselecting, or the host disappearing) and, unless the remote
        side already closed it, tear down the backend's PTY session
        too rather than leaving it running unattended."""
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

    def delete_host(self):
        entry = self._selected_entry()
        if entry is None:
            return

        if entry["kind"] == "merged":
            confirm = QMessageBox.question(
                self, "Confirm",
                f"Remove {entry['label']}? This removes both its Agent and SSH connections."
            )
            if confirm != QMessageBox.Yes:
                return

            agent_entry = entry["agent_entry"]
            ssh_entry = entry["ssh_entry"]

            if self.active_id in (agent_entry["id"], ssh_entry["id"]):
                self._end_ssh_session()

            errors = []
            try:
                api.disenroll_agent(agent_entry["id"])
            except Exception as e:
                errors.append(f"agent: {e}")
            try:
                api.delete_host(ssh_entry["id"])
            except Exception as e:
                errors.append(f"ssh: {e}")

            if errors:
                QMessageBox.critical(self, "Error", "\n".join(errors))

            bus.host_removed.emit(agent_entry["id"])
            bus.host_removed.emit(ssh_entry["id"])
            return

        confirm = QMessageBox.question(
            self, "Confirm",
            f"Remove {entry['label']}?"
        )
        if confirm != QMessageBox.Yes:
            return

        if entry["kind"] == self.active_kind and entry["id"] == self.active_id:
            self._end_ssh_session()

        try:
            if entry["kind"] == "agent":
                api.disenroll_agent(entry["id"])
            else:
                api.delete_host(entry["id"])
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        bus.host_removed.emit(entry["id"])

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
    # ENVIRONMENTS (shared registry - used by the connect form to tag
    # a newly-connected SSH host; reassigning environment for an
    # already-connected host is handled in Host Enrollment / User &
    # Group Administration, not duplicated here)
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
    # CONNECT (password in, key-based access out - one click)
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
        self.enroll_status.setText(f"'{name}' connected — key installed, ready in the Terminal below.")
        self.load_hosts()

    # =====================================================
    # TERMINAL
    #
    # SSH hosts now run through the live PTY session above (see
    # _start_ssh_session / _send_terminal_input / _on_ssh_read_done) -
    # this command box is only wired up for agent hosts, which have no
    # direct connection: the controller queues a task and this page
    # polls for the result until the agent reports back (or it times
    # out).
    # =====================================================
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
        """Stop watching for an in-flight agent command's result -
        called whenever the active connection is about to change (a
        different host, or a merged host's connection-type switching
        away from "agent"). Without this, agent_poll_timer kept firing
        against a host that was no longer the active connection: its
        eventual _finish_command() call re-focused cmd_input even
        while a live SSH session was using the same panel instead -
        cmd_input is hidden during an SSH session, so that stole focus
        away from the terminal with nothing actually able to receive
        it, which is what made the SSH terminal look like it had
        stopped accepting keystrokes. The agent still finishes the
        command server-side either way - this only stops the client
        from watching for/displaying that result."""
        self.pending_agent_task = None
        self.agent_poll_timer.stop()

    def _poll_agent_task(self):
        task = self.pending_agent_task

        if not task:
            self.agent_poll_timer.stop()
            return

        # Belt-and-suspenders against the same staleness this is meant
        # to prevent (see _cancel_pending_agent_task) - if the active
        # connection has since moved on, drop this result instead of
        # rendering it into whatever's on screen now and stealing focus.
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
        self.cmd_input.setEnabled(True)
        self.cmd_input.setFocus()
        self._scroll_to_bottom()

    def _scroll_to_bottom(self):
        scrollbar = self.output.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    # =====================================================
    # FILE TRANSFER
    #
    # SSH hosts run synchronously (SFTP, no size limit, ready
    # immediately). Agent hosts queue through the same task channel as
    # the command box above, so they're dispatched, then polled by
    # self.file_poll_timer/_poll_file_task until the agent reports
    # back - mirrors _run_agent_command/_poll_agent_task above.
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
            self.file_status.setText(
                f"[timed out waiting for agent to confirm {task['direction']}]"
            )
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

        # download
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
        # Don't leave an SSH PTY session running on the backend after
        # this window closes - the operator would have no way back
        # into it anyway since closing the popout drops active_id.
        self._end_ssh_session()
        self._terminal_io.shutdown()
        super().closeEvent(event)
