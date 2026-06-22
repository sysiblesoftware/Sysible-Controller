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
from PySide6.QtGui import QFont, QFontMetrics, QKeySequence, QTextCursor, QColor, QTextCharFormat

# pyte gives us a real VT screen (a 2D character grid with cursor
# addressing) so cursor-positioned, full-screen apps - vi, top, less,
# htop - render correctly instead of having their redraws appended as
# stray lines. It's optional: if it isn't installed the terminal still
# works for ordinary shell use, full-screen apps just degrade to the old
# append behaviour rather than crashing.
try:
    import pyte
except ImportError:
    pyte = None

# PySide6 6.4+ uses scoped enums, so QTextCursor.End no longer exists -
# it's QTextCursor.MoveOperation.End. Resolve it once, with a fallback
# to the old unscoped name so this works on any PySide6 6.x.
_CURSOR_END = (
    QTextCursor.MoveOperation.End
    if hasattr(QTextCursor, "MoveOperation")
    else QTextCursor.End
)

# Standard 16-colour ANSI palette (xterm-ish): 0-7 normal, 8-15 bright.
# Used to colourise SGR ("\x1b[...m") escape sequences in the live
# terminal instead of stripping them out.
_ANSI_PALETTE = [
    "#1E1E1E", "#CD3131", "#0DBC79", "#E5E510",
    "#2472C8", "#BC3FBC", "#11A8CD", "#E5E5E5",
    "#666666", "#F14C4C", "#23D18B", "#F5F543",
    "#3B8EEA", "#D670D6", "#29B8DB", "#FFFFFF",
]
_TERM_DEFAULT_FG = "#E8E8E8"
# Qt6 font weights are numeric (Normal=400, Bold=700); use the ints
# directly to avoid any scoped-enum pitfalls like the one above.
_WEIGHT_NORMAL = 400
_WEIGHT_BOLD = 700

# Privilege-cue colouring for "user@host" tokens in the terminal: green
# normally, red when it's root@. Applied GUI-side (see
# _LiveTerminalOutput._insert_run) so it works no matter how root was
# reached - su, sudo -i, etc. - unlike a remote PS1 tweak, which a fresh
# root shell would just override.
_PROMPT_GREEN = "#23D18B"
_PROMPT_RED = "#F14C4C"
_PROMPT_TOKEN_RE = re.compile(r"[A-Za-z_][\w.\-]*@[A-Za-z0-9][\w.\-]*")

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

    def submit_resize(self, session_id, cols, rows):
        # Fire-and-forget; a failed pty resize is non-fatal to the session.
        def work():
            try:
                api.resize_terminal(session_id, cols, rows)
            except Exception:
                pass
        self._pool.submit(work)

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


# pyte reports cell colours as names (or a 6-hex string for 256/true
# colour). Map the names onto the same palette the append renderer uses.
_PYTE_COLORS = {
    "black": "#1E1E1E", "red": "#CD3131", "green": "#23D18B",
    "brown": "#E5E510", "yellow": "#E5E510", "blue": "#3B8EEA",
    "magenta": "#BC3FBC", "cyan": "#29B8DB", "white": "#E8E8E8",
    "brightblack": "#666666", "brightred": "#F14C4C", "brightgreen": "#23D18B",
    "brightbrown": "#F5F543", "brightyellow": "#F5F543", "brightblue": "#3B8EEA",
    "brightmagenta": "#D670D6", "brightcyan": "#29B8DB", "brightwhite": "#FFFFFF",
}
_TERM_DEFAULT_BG = "#1E1E1E"
_HEXDIGITS = set("0123456789abcdefABCDEF")


class _LiveTerminalOutput(QTextEdit):
    """The terminal output pane doubles as the input surface for an
    SSH host's live session: while `on_key_data` is set, keystrokes
    typed here are forwarded straight to the remote pty instead of
    being inserted locally - what shows up is only ever what the remote
    shell echoes back. With no session attached it behaves like an
    ordinary read-only log.

    Two rendering modes share this one widget:
      * Normal mode - an append-only scrolling log (with scrollback),
        honouring colour/erase escapes. Good for ordinary shell output.
      * Alternate-screen mode - when a full-screen app (vi, top, less)
        switches to the xterm alternate screen, output is driven through
        a pyte VT grid and the widget is repainted from that grid, so
        cursor-addressed redraws land in the right place. On exit the
        saved normal log is restored intact."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.on_key_data = None
        self._ansi_pending = ""        # incomplete escape carried across reads
        self._reset_char_format()
        # alternate-screen (pyte) state
        self._in_alt = False
        self._alt_screen = None
        self._alt_stream = None
        self._saved_html = None
        self._cols = 120              # remote pty grid size (kept in sync
        self._rows = 32               # with the window via resize)

    def _reset_char_format(self):
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(_TERM_DEFAULT_FG))
        fmt.setFontWeight(_WEIGHT_NORMAL)
        self._char_format = fmt

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
        # Render remote output, honouring SGR colour escapes. A rendering
        # hiccup must never bubble up and kill the read loop (that would
        # leave the terminal blank but still "connected"), so fall back to
        # plain, colour-stripped text if anything goes wrong.
        try:
            self._render(self._ansi_pending + text)
        except Exception:
            try:
                self.moveCursor(_CURSOR_END)
                self.insertPlainText(_strip_ansi(text).replace("\r", ""))
            except Exception:
                pass

    def _render(self, text):
        # Outer pass: split the stream at alternate-screen enter/exit
        # boundaries and route each segment to the right renderer. Only
        # the alt-screen mode-set/reset escapes are interpreted here;
        # everything else is handed off whole.
        self._ansi_pending = ""
        i, n = 0, len(text)
        seg_start = 0
        while i < n:
            if text[i] != "\x1b":
                nxt = text.find("\x1b", i)
                if nxt == -1:
                    break
                i = nxt
                continue
            consumed, kind, params = self._scan_escape(text, i)
            if consumed is None:
                # Incomplete escape split across reads: render up to it and
                # carry the fragment to the next read.
                self._flush_segment(text[seg_start:i])
                self._ansi_pending = text[i:]
                return
            if kind in ("sm", "rm") and self._is_alt_mode_params(params):
                want_alt = (kind == "sm")
                if want_alt != self._in_alt:
                    self._flush_segment(text[seg_start:i])
                    self._set_alt(want_alt)
                    i += consumed
                    seg_start = i
                    continue
            i += consumed
        self._flush_segment(text[seg_start:n])

    def _flush_segment(self, seg):
        if not seg:
            return
        if self._in_alt and self._alt_stream is not None:
            self._alt_stream.feed(seg)
            self._repaint_alt()
        else:
            self._render_normal(seg)

    def _render_normal(self, text):
        """Append-only renderer for ordinary shell output. Assumes the
        segment contains only complete escapes (the outer pass carries any
        trailing partial escape across reads)."""
        cursor = self.textCursor()
        cursor.movePosition(_CURSOR_END)
        i, n = 0, len(text)
        while i < n:
            ch = text[i]
            if ch == "\x1b":
                consumed, kind, params = self._scan_escape(text, i)
                if consumed is None:
                    break
                if kind == "sgr":
                    self._apply_sgr(params)
                elif kind == "ed" and params in ("2", "3"):
                    # Erase entire display (what `clear` / Ctrl-L send):
                    # wipe the pane so the shell can redraw into it.
                    self.clear()
                    cursor = self.textCursor()
                    cursor.movePosition(_CURSOR_END)
                i += consumed
                continue
            if ch == "\r":
                i += 1
                continue
            if ch in ("\x08", "\x7f"):
                cursor.deletePreviousChar()
                i += 1
                continue
            j = i
            while j < n and text[j] not in ("\x1b", "\r", "\x08", "\x7f"):
                j += 1
            self._insert_run(cursor, text[i:j])
            i = j
        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    # ---------------- alternate-screen (pyte grid) ----------------
    @staticmethod
    def _is_alt_mode_params(params):
        # True only for the xterm alternate-screen private modes, not other
        # "?..h/l" modes (e.g. ?2004 bracketed paste, ?25 cursor visibility).
        if not params.startswith("?"):
            return False
        return any(p in ("47", "1047", "1049") for p in params[1:].split(";"))

    def _set_alt(self, on):
        if on:
            if pyte is None:
                return  # no emulator available - stay in append mode
            self._saved_html = self.toHtml()
            self._alt_screen = pyte.Screen(self._cols, self._rows)
            self._alt_stream = pyte.Stream(self._alt_screen)
            self._in_alt = True
            self.clear()
        else:
            if not self._in_alt:
                return
            self._in_alt = False
            self._alt_screen = None
            self._alt_stream = None
            if self._saved_html is not None:
                self.setHtml(self._saved_html)
                self._saved_html = None
            self.moveCursor(_CURSOR_END)
            self.ensureCursorVisible()

    def resize_grid(self, cols, rows):
        """Track the remote pty grid size and, if a full-screen app is
        currently up, resize its pyte screen to match so it redraws to the
        new window dimensions."""
        self._cols, self._rows = cols, rows
        if self._in_alt and self._alt_screen is not None:
            self._alt_screen.resize(rows, cols)  # pyte: (lines, columns)
            self._repaint_alt()

    @staticmethod
    def _pyte_color(name, default):
        if name == "default":
            return default
        hexv = _PYTE_COLORS.get(name)
        if hexv:
            return hexv
        if len(name) == 6 and all(c in _HEXDIGITS for c in name):
            return "#" + name
        return default

    def _repaint_alt(self):
        screen = self._alt_screen
        if screen is None:
            return
        lines_html = []
        buf = screen.buffer
        for y in range(screen.lines):
            row = buf[y]
            spans = []
            run = []
            cur_style = None
            for x in range(screen.columns):
                cell = row[x]
                fg = self._pyte_color(cell.fg, _TERM_DEFAULT_FG)
                bg = self._pyte_color(cell.bg, _TERM_DEFAULT_BG)
                if cell.reverse:
                    fg, bg = bg, fg
                style = (
                    f"color:{fg};background-color:{bg};"
                    f"font-weight:{'bold' if cell.bold else 'normal'};"
                    f"text-decoration:{'underline' if cell.underscore else 'none'};"
                )
                if style != cur_style:
                    if run:
                        spans.append(f'<span style="{cur_style}">{"".join(run)}</span>')
                    run = []
                    cur_style = style
                ch = cell.data or " "
                ch = (ch.replace("&", "&amp;").replace("<", "&lt;")
                      .replace(">", "&gt;").replace(" ", "&nbsp;"))
                run.append(ch)
            if run:
                spans.append(f'<span style="{cur_style}">{"".join(run)}</span>')
            lines_html.append("".join(spans))
        html = (
            '<div style="font-family:Menlo,Consolas,\'DejaVu Sans Mono\',monospace;">'
            + "<br>".join(lines_html)
            + "</div>"
        )
        self.setUpdatesEnabled(False)
        self.setHtml(html)
        self.setUpdatesEnabled(True)

    def grid_size_for_viewport(self):
        """Compute the (cols, rows) that fit the current viewport at the
        widget's font - used to size the remote pty to the window."""
        fm = QFontMetrics(self.font())
        cw = fm.horizontalAdvance("M") or 8
        ch = fm.lineSpacing() or 16
        vp = self.viewport()
        cols = max(20, vp.width() // cw)
        rows = max(4, vp.height() // ch)
        return int(cols), int(rows)

    def _insert_run(self, cursor, run):
        """Insert plain text, tinting any "user@host" token as a privilege
        cue: green normally, red when it's root@ (works regardless of how
        root was reached - su, sudo -i, etc.)."""
        pos = 0
        for m in _PROMPT_TOKEN_RE.finditer(run):
            s, e = m.span()
            if s > pos:
                cursor.insertText(run[pos:s], self._char_format)
            token = run[s:e]
            fmt = QTextCharFormat(self._char_format)
            fmt.setForeground(QColor(_PROMPT_RED if token.startswith("root@") else _PROMPT_GREEN))
            fmt.setFontWeight(_WEIGHT_BOLD)
            cursor.insertText(token, fmt)
            pos = e
        if pos < len(run):
            cursor.insertText(run[pos:], self._char_format)

    @staticmethod
    def _scan_escape(text, i):
        """Scan one escape sequence starting at text[i] == ESC. Returns
        (consumed, kind, params): kind is 'sgr' (colour, final 'm'),
        'ed' (erase-display, final 'J'), or 'other'; params is the
        parameter string. Returns (None, None, None) if the sequence is
        incomplete (carry it to the next read)."""
        n = len(text)
        if i + 1 >= n:
            return None, None, None
        nxt = text[i + 1]
        if nxt == "[":  # CSI ... final byte 0x40-0x7e
            j = i + 2
            while j < n and not (0x40 <= ord(text[j]) <= 0x7e):
                j += 1
            if j >= n:
                return None, None, None
            final = text[j]
            params = text[i + 2:j]
            consumed = j + 1 - i
            if final == "m":
                return consumed, "sgr", params
            if final == "J":
                return consumed, "ed", params
            if final == "h":
                return consumed, "sm", params   # set mode
            if final == "l":
                return consumed, "rm", params   # reset mode
            return consumed, "other", params
        if nxt == "]":  # OSC ... terminated by BEL or ESC-backslash (ST)
            j = i + 2
            while j < n:
                if text[j] == "\x07":
                    return j + 1 - i, "other", None
                if text[j] == "\x1b":
                    if j + 1 < n and text[j + 1] == "\\":
                        return j + 2 - i, "other", None
                    return None, None, None
                j += 1
            return None, None, None
        # Any other two-character escape (ESC =, ESC >, charset selects...)
        return 2, "other", None

    def _apply_sgr(self, params):
        codes = params.split(";") if params else ["0"]
        fmt = self._char_format
        idx = 0
        while idx < len(codes):
            tok = codes[idx]
            code = int(tok) if tok.isdigit() else 0
            if code == 0:
                self._reset_char_format()
                fmt = self._char_format
            elif code == 1:
                fmt.setFontWeight(_WEIGHT_BOLD)
            elif code == 22:
                fmt.setFontWeight(_WEIGHT_NORMAL)
            elif code == 4:
                fmt.setFontUnderline(True)
            elif code == 24:
                fmt.setFontUnderline(False)
            elif 30 <= code <= 37:
                fmt.setForeground(QColor(_ANSI_PALETTE[code - 30]))
            elif 90 <= code <= 97:
                fmt.setForeground(QColor(_ANSI_PALETTE[code - 90 + 8]))
            elif code == 39:
                fmt.setForeground(QColor(_TERM_DEFAULT_FG))
            elif 40 <= code <= 47:
                fmt.setBackground(QColor(_ANSI_PALETTE[code - 40]))
            elif 100 <= code <= 107:
                fmt.setBackground(QColor(_ANSI_PALETTE[code - 100 + 8]))
            elif code == 49:
                fmt.clearBackground()
            elif code in (38, 48):
                # 256-colour (38;5;N) or truecolour (38;2;R;G;B): apply the
                # colour and skip its extra params so they aren't misread.
                is_fg = code == 38
                if idx + 1 < len(codes) and codes[idx + 1] == "5" and idx + 2 < len(codes):
                    col = QColor(_ANSI_PALETTE[int(codes[idx + 2])]) if codes[idx + 2].isdigit() and int(codes[idx + 2]) < 16 else None
                    if col is not None:
                        fmt.setForeground(col) if is_fg else fmt.setBackground(col)
                    idx += 2
                elif idx + 1 < len(codes) and codes[idx + 1] == "2" and idx + 4 < len(codes):
                    try:
                        col = QColor(int(codes[idx + 2]), int(codes[idx + 3]), int(codes[idx + 4]))
                        fmt.setForeground(col) if is_fg else fmt.setBackground(col)
                    except (ValueError, IndexError):
                        pass
                    idx += 4
            idx += 1
        self._char_format = fmt


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
    """The sub-entry the main page targets for file transfer - SSH
    preferred on a merged host, otherwise the entry itself."""
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

    def __init__(self, entry, on_close=None, session_num=1):
        super().__init__()

        self.entry = entry
        self.on_close = on_close

        # session_num distinguishes multiple windows open on the same host.
        suffix = f"  (#{session_num})" if session_num > 1 else ""
        self.setWindowTitle(f"Terminal — {entry['label']}{suffix}")
        self.resize(840, 540)

        self.active_kind = None
        self.active_id = None
        self.active_label = entry["label"]

        self.pending_agent_task = None
        self._ssh_session_active = False
        # Opaque id for the current backend PTY session (one per open SSH
        # shell). All terminal read/write/close calls address this, not the
        # host name, so several shells to one host stay independent.
        self._session_id = None

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
        clear_btn.clicked.connect(self._clear_terminal)

        exit_btn = QPushButton("Exit Session")
        exit_btn.clicked.connect(self.close)

        cmd_row.addWidget(self.cmd_input, 4)
        cmd_row.addWidget(self.run_btn)
        cmd_row.addWidget(self.interrupt_btn)
        cmd_row.addWidget(clear_btn)
        cmd_row.addWidget(exit_btn)
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

        # Debounce window resizes - only push the new pty size to the host
        # once the user stops dragging, not on every intermediate pixel.
        self._resize_timer = QTimer()
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(150)
        self._resize_timer.timeout.connect(self._apply_pty_resize)

        # ---- start on the first (preferred) connection ----
        self._switch_to(options[0][1])

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._ssh_session_active:
            self._resize_timer.start()

    def _apply_pty_resize(self):
        if not self._ssh_session_active or self._session_id is None:
            return
        cols, rows = self.output.grid_size_for_viewport()
        if (cols, rows) == (self.output._cols, self.output._rows):
            return
        self.output.resize_grid(cols, rows)
        self._terminal_io.submit_resize(self._session_id, cols, rows)

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

        # Each open mints a brand-new backend PTY session and returns its
        # id; we never reuse one. This is what lets a single host carry
        # several independent shells at once - including more than one
        # window pointed at the same host.
        try:
            resp = api.open_terminal(entry["id"])
            session_id = resp.get("session_id")
            if not session_id:
                raise RuntimeError("backend did not return a session id")
        except Exception as e:
            self.output.append(f"[could not open terminal session: {e}]")
            self.title_label.setText(f"{self.active_label}  [SSH] (failed)")
            self.run_btn.setEnabled(True)
            self.active_kind = None
            self.active_id = None
            return

        self._session_id = session_id
        self.title_label.setText(f"{self.active_label}  [SSH] (live)")
        self.cmd_input.hide()
        self.run_btn.hide()
        self.interrupt_btn.setVisible(True)
        self.ssh_hint.setVisible(True)
        self.output.setReadOnly(False)
        self.output.on_key_data = self._send_terminal_input
        self.output.setFocus()
        self._ssh_session_active = True
        # Size the remote pty to the current window so full-screen apps use
        # the whole panel from the outset (not the 120x32 pty default).
        cols, rows = self.output.grid_size_for_viewport()
        self.output.resize_grid(cols, rows)
        self._terminal_io.submit_resize(self._session_id, cols, rows)
        self._terminal_io.submit_read(self._session_id)

    def _send_terminal_input(self, data):
        if self._session_id is not None:
            self._terminal_io.submit_write(self._session_id, data)

    def send_interrupt(self):
        if self.active_kind == "ssh" and self.output.on_key_data is not None:
            self._send_terminal_input("\x03")
            self.output.setFocus()

    def _clear_terminal(self):
        self.output.clear()
        # For a live SSH shell, ask it to redraw the prompt into the now
        # empty pane (Ctrl-L), so you don't end up staring at a blank
        # terminal with no prompt.
        if self.active_kind == "ssh" and self._ssh_session_active:
            self._send_terminal_input("\x0c")
            self.output.setFocus()

    def _drain_read_results(self):
        """Pull any read results the worker threads have queued and handle
        them here on the GUI thread. Each handled read also re-issues the
        next read, so this is what keeps the live SSH read loop turning."""
        while True:
            try:
                session_id, result = self._terminal_io.results.get_nowait()
            except queue.Empty:
                return
            if result is None:
                self._on_ssh_read_failed(session_id)
            else:
                self._on_ssh_read_done(session_id, result)

    def _on_ssh_read_done(self, session_id, result):
        if not self._ssh_session_active or session_id != self._session_id:
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

        self._terminal_io.submit_read(session_id)

    def _on_ssh_read_failed(self, session_id):
        if not self._ssh_session_active or session_id != self._session_id:
            return
        self._ssh_retry_timer.start(SSH_TERMINAL_POLL_MS)

    def _resume_ssh_read_loop(self):
        if self._ssh_session_active and self._session_id is not None:
            self._terminal_io.submit_read(self._session_id)

    def _on_ssh_write_failed(self, err):
        self.output.append_terminal_text(f"\n[write failed: {err}]\n")

    def _end_ssh_session(self, close_remote=True):
        was_ssh = self.active_kind == "ssh" and self._ssh_session_active
        session_id = self._session_id

        self._ssh_session_active = False
        self._session_id = None
        self._ssh_retry_timer.stop()
        self.output.on_key_data = None
        self.output.setReadOnly(True)
        self.cmd_input.show()
        self.run_btn.show()
        self.interrupt_btn.setVisible(False)
        self.cmd_input.setEnabled(False)
        self.run_btn.setEnabled(True)
        self.ssh_hint.setVisible(False)

        if was_ssh and close_remote and session_id is not None:
            try:
                api.close_terminal(session_id)
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
            self.on_close(self)
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

    The right-hand panel keeps file transfer (acting on the currently
    selected host) and the connect-a-new-SSH-host form.
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

        self.pending_file_task = None      # agent file transfer

        self._terminals = []               # list of open TerminalPopout windows

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
            # Remote Host Administration manages SSH connections themselves,
            # so it shows every host (agent, SSH, and merged) - unlike the
            # System Administration tools, which are agent-only.
            entries = api.list_merged_hosts(agent_only=False)
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
                "File Transfer below uses SSH."
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

        # Every double-click opens a NEW, independent session - including
        # additional shells to a host that already has one open. Number
        # repeat windows for the same host so their titles stay distinct.
        session_num = sum(
            1 for p in self._terminals if p.entry.get("label") == entry["label"]
        ) + 1

        popout = TerminalPopout(
            entry, on_close=self._on_terminal_closed, session_num=session_num
        )
        self._terminals.append(popout)
        popout.show()
        popout.raise_()
        popout.activateWindow()

    def _on_terminal_closed(self, popout):
        if popout in self._terminals:
            self._terminals.remove(popout)

    def _close_terminal_for(self, label):
        # Close every open window for this host (there may be several).
        for popout in [p for p in self._terminals if p.entry.get("label") == label]:
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
        for popout in list(self._terminals):
            popout.close()
        self._terminals.clear()
        super().closeEvent(event)
