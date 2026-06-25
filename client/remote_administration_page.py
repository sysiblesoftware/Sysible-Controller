import os
import queue
import re
import time
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QLineEdit, QTextEdit, QComboBox, QInputDialog, QMessageBox,
    QApplication, QFileDialog, QFrame, QGroupBox, QMenu, QDialog,
)
from PySide6.QtCore import Qt, QTimer, QObject, Signal, QThread
from PySide6.QtGui import (
    QFont, QFontMetrics, QKeySequence, QTextCursor, QColor, QTextCharFormat,
    QPixmap, QPainter, QIcon,
)

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
_CURSOR_START = (
    QTextCursor.MoveOperation.Start
    if hasattr(QTextCursor, "MoveOperation")
    else QTextCursor.Start
)
_HAS_MOVEOP = hasattr(QTextCursor, "MoveOperation")
_SOB = QTextCursor.MoveOperation.StartOfBlock if _HAS_MOVEOP else QTextCursor.StartOfBlock
_EOB = QTextCursor.MoveOperation.EndOfBlock if _HAS_MOVEOP else QTextCursor.EndOfBlock
_MOVE_RIGHT = QTextCursor.MoveOperation.Right if _HAS_MOVEOP else QTextCursor.Right
_KEEP_ANCHOR = (
    QTextCursor.MoveMode.KeepAnchor
    if hasattr(QTextCursor, "MoveMode")
    else QTextCursor.KeepAnchor
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
from client.theme import (
    STATUS_NEUTRAL_COLOR, STATUS_SUCCESS_COLOR, STATUS_ERROR_COLOR, STATUS_WARNING_COLOR,
)
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
                elif kind == "el" and params in ("", "0"):
                    # Erase line from cursor to end - the shell uses this
                    # (with \r) to redraw the current line in place.
                    e = QTextCursor(cursor)
                    e.movePosition(_EOB, _KEEP_ANCHOR)
                    e.removeSelectedText()
                i += consumed
                continue
            if ch == "\r":
                # Carriage return: back to the start of the current line so
                # what follows OVERWRITES it (not appends) - this is what
                # stops a redrawn shell prompt from showing up twice.
                cursor.movePosition(_SOB)
                i += 1
                continue
            if ch == "\n":
                cursor.movePosition(_CURSOR_END)
                cursor.insertText("\n", self._char_format)
                i += 1
                continue
            if ch in ("\x08", "\x7f"):
                cursor.deletePreviousChar()
                i += 1
                continue
            j = i
            while j < n and text[j] not in ("\x1b", "\r", "\n", "\x08", "\x7f"):
                j += 1
            run = text[i:j]
            # Overwrite any existing characters on the line under the cursor
            # (replace mode), so an in-place redraw replaces rather than
            # duplicates. Past the end of the line this is a plain append.
            end = QTextCursor(cursor)
            end.movePosition(_EOB)
            ahead = end.position() - cursor.position()
            if ahead > 0:
                cursor.movePosition(_MOVE_RIGHT, _KEEP_ANCHOR, min(len(run), ahead))
                cursor.removeSelectedText()
            self._insert_run(cursor, run)
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
        # Put the visible caret where the application's cursor actually is
        # (vi's editing line, less's position, ...). Without this, setHtml
        # leaves the caret/scroll at the end of the grid, so the view jumps
        # to the blank bottom rows and you can't see the line you're typing.
        # Each rendered row is `columns` chars plus one <br> separator.
        try:
            pos = screen.cursor.y * (screen.columns + 1) + screen.cursor.x
            cur = self.textCursor()
            cur.setPosition(min(pos, len(self.toPlainText())))
            self.setTextCursor(cur)
        except Exception:
            pass
        self.setUpdatesEnabled(True)
        self.ensureCursorVisible()

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
            if final == "K":
                return consumed, "el", params   # erase in line
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
# HOST CHECK-IN / PING
# =====================================================================
# An agent heartbeats every SYSIBLE_POLL_INTERVAL seconds (default 1.5)
# when idle, so a host that hasn't checked in within this window is
# treated as offline. Generous enough to ride out a busy cycle or a
# brief blip without false alarms.
CHECKIN_ONLINE_SECONDS = 20

CHECKIN_COLOR_ONLINE = STATUS_SUCCESS_COLOR
CHECKIN_COLOR_OFFLINE = STATUS_ERROR_COLOR


def _ago(seconds):
    """Compact 'time since' label: '3s', '4m', '2h', '1d'."""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _probe_host(entry, last_seen, now):
    """Reachability for one host entry. Agent side: judged by heartbeat
    age (no traffic to the host). SSH side: a live, timed `true` over the
    existing SSH path. A merged host reports both and counts as online if
    either path answers. Returns {"state","detail","color"}."""
    kind = entry["kind"]
    parts = []  # (ok: bool, detail: str)

    agent_id = None
    if kind == "agent":
        agent_id = entry.get("id")
    elif kind == "merged":
        agent_id = (entry.get("agent_entry") or {}).get("id")
    if agent_id is not None:
        ls = last_seen.get(agent_id)
        if ls:
            age = now - ls
            parts.append((age <= CHECKIN_ONLINE_SECONDS, f"agent {_ago(age)} ago"))
        else:
            parts.append((False, "agent: no heartbeat"))

    ssh_entry = None
    if kind == "ssh":
        ssh_entry = entry
    elif kind == "merged":
        ssh_entry = entry.get("ssh_entry")
    if ssh_entry is not None and ssh_entry.get("id") is not None:
        t0 = time.time()
        try:
            result = api.exec_remote(ssh_entry["id"], "true")
            ms = int((time.time() - t0) * 1000)
            ok = result.get("code") == 0
            parts.append((ok, f"SSH {ms} ms" if ok else "SSH unreachable"))
        except Exception:
            parts.append((False, "SSH unreachable"))

    if not parts:
        return {"state": "unknown", "detail": "not reachable", "color": CHECKIN_COLOR_OFFLINE}

    online = any(ok for ok, _ in parts)
    return {
        "state": "online" if online else "offline",
        "detail": " · ".join(d for _, d in parts),
        "color": CHECKIN_COLOR_ONLINE if online else CHECKIN_COLOR_OFFLINE,
    }


class _CheckInWorker(QThread):
    """Runs the per-host check-in off the GUI thread (SSH connection tests
    can each take a moment) and emits one results dict, label -> status,
    when finished."""

    done = Signal(dict)

    def __init__(self, entries):
        super().__init__()
        self._entries = entries

    def run(self):
        try:
            agents = api.get_agents()
        except Exception:
            agents = []
        last_seen = {a.get("host_id"): a.get("last_seen") for a in agents}
        now = time.time()
        results = {}
        for entry in self._entries:
            results[entry["label"]] = _probe_host(entry, last_seen, now)
        self.done.emit(results)


class _FleetCommandWorker(QThread):
    """Runs one command across every given host off the GUI thread and emits
    a list of per-host {label, ok, output}. Agent tasks are polled to
    completion (bounded) - for reboot/power-off/agent-restart the host often
    goes down before reporting, which surfaces as a clear 'timed out' note
    rather than a hang."""

    done = Signal(list)

    def __init__(self, entries, command, kind="command", poll_seconds=25):
        super().__init__()
        self._entries = entries
        self._command = command
        self._kind = kind
        self._poll_seconds = poll_seconds

    @staticmethod
    def _normalize(label, r):
        code = r.get("code")
        out = (r.get("stdout", "") or "")
        if r.get("stderr"):
            out = (out + "\n" + r["stderr"]).strip()
        ok = (code == 0) if code is not None else (not r.get("stderr"))
        return {"label": label, "ok": ok, "output": out.strip() or "(no output)"}

    def run(self):
        results = []
        for entry in self._entries:
            label = entry["label"]
            try:
                outcome = api.run_on_entry(entry, self._command, kind=self._kind)
            except Exception as e:
                results.append({"label": label, "ok": False, "output": str(e)})
                continue
            if outcome.get("error"):
                results.append({"label": label, "ok": False, "output": outcome["error"]})
                continue
            if outcome.get("sync"):
                results.append(self._normalize(label, outcome))
                continue
            task_id = outcome.get("task_id")
            polled = None
            deadline = time.time() + self._poll_seconds
            while time.time() < deadline:
                polled = api.poll_entry_result(entry, task_id)
                if polled is not None:
                    break
                time.sleep(1.0)
            if polled is None:
                results.append({"label": label, "ok": False,
                                "output": "timed out waiting for the agent "
                                          "(expected if the host is rebooting / the agent is restarting)"})
            else:
                results.append(self._normalize(label, polled))
        self.done.emit(results)


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
        # ---- terminal toolbar (file transfer + view tools) ----
        toolbar = QHBoxLayout()
        btn_upload = QPushButton("Upload…")
        btn_upload.setToolTip("Send a local file to this host over SSH.")
        btn_upload.clicked.connect(self.upload_to_host)
        btn_download = QPushButton("Download…")
        btn_download.setToolTip("Fetch a file from this host over SSH.")
        btn_download.clicked.connect(self.download_from_host)
        btn_find = QPushButton("Find…")
        btn_find.setToolTip("Search the terminal output.")
        btn_find.clicked.connect(self.find_in_output)
        btn_save = QPushButton("Save Output…")
        btn_save.setToolTip("Save the terminal's text to a file.")
        btn_save.clicked.connect(self.save_output)
        btn_sudo = QPushButton("Send sudo password")
        btn_sudo.setToolTip(
            "Type your stored sudo password at a password prompt (so you don't need "
            "passwordless sudo). The password is stored encrypted on this machine; "
            "click to set it the first time.")
        btn_sudo.clicked.connect(self.send_sudo_password)
        btn_font_dec = QPushButton("A-")
        btn_font_dec.setMaximumWidth(36)
        btn_font_dec.clicked.connect(lambda: self.adjust_font(-1))
        btn_font_inc = QPushButton("A+")
        btn_font_inc.setMaximumWidth(36)
        btn_font_inc.clicked.connect(lambda: self.adjust_font(1))
        for b in (btn_upload, btn_download, btn_find, btn_save, btn_sudo):
            toolbar.addWidget(b)
        toolbar.addStretch()
        toolbar.addWidget(QLabel("Font:"))
        self.font_combo = QComboBox()
        self.font_combo.setToolTip("Terminal font family")
        self._font_families = [
            "DejaVu Sans Mono", "Liberation Mono", "Menlo", "Consolas",
            "Ubuntu Mono", "Courier New", "Monospace",
        ]
        self.font_combo.addItems(self._font_families)
        self.font_combo.setMaximumWidth(150)
        # Connect AFTER populating so building the list doesn't fire a change.
        self.font_combo.currentTextChanged.connect(self.set_font_family)
        toolbar.addWidget(self.font_combo)
        toolbar.addWidget(btn_font_dec)
        toolbar.addWidget(btn_font_inc)
        layout.addLayout(toolbar)

        layout.addWidget(self.output, 1)

        self.ssh_hint = QLabel(
            "SSH terminal is live - click into the panel above and type "
            "directly (sudo prompts, vim, Ctrl+C, arrow keys all work)."
        )
        theme.style_hint_label(self.ssh_hint)
        self.ssh_hint.setVisible(False)
        layout.addWidget(self.ssh_hint)

        # If the VT emulator library isn't importable, full-screen apps
        # (vim/top/nano/less) silently degrade to garbled append output.
        # Make that visible instead of leaving it a mystery.
        if pyte is None:
            pyte_warning = QLabel(
                "⚠ Full-screen apps (vim, top, nano, less) need the ‘pyte’ package, which "
                "isn’t installed in the controller’s Python environment — until it is, they "
                "will render incorrectly. Fix: install it into the controller venv and "
                "restart, e.g.  /opt/sysible/venv/bin/pip install pyte  then  "
                "sudo sysible_controller restart."
            )
            pyte_warning.setWordWrap(True)
            pyte_warning.setStyleSheet(f"color: {STATUS_WARNING_COLOR}; font-weight: bold;")
            layout.addWidget(pyte_warning)

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

    def send_sudo_password(self):
        """Type the operator's stored sudo password at a password prompt -
        an alternative to passwordless sudo for hardened environments. The
        password is stored encrypted on this machine (become_credentials);
        the first click prompts to set it. Sent followed by Enter."""
        from client import become_credentials
        from PySide6.QtWidgets import QInputDialog, QLineEdit

        host = self.entry.get("label", "")
        password = become_credentials.get_password(host)

        if not password:
            if not become_credentials.encryption_available():
                QMessageBox.warning(
                    self, "Unavailable",
                    "The encryption library isn't available, so a sudo password can't "
                    "be stored securely on this machine.")
                return
            text, ok = QInputDialog.getText(
                self, "Set sudo password",
                f"Sudo password for this session (stored encrypted on this machine; "
                f"used for {host or 'this host'} and as the fleet default):",
                QLineEdit.Password)
            if not ok or not text:
                return
            scope_global = QMessageBox.question(
                self, "Scope",
                "Use this password for ALL hosts (fleet default)?\n\n"
                "Yes = store as the global default.\nNo = store for this host only.",
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes
            become_credentials.set_password(text, host="*" if scope_global else host)
            password = text

        if self._session_id is None:
            QMessageBox.information(self, "No terminal", "Open a terminal first.")
            return
        self._send_terminal_input(password + "\n")
        self.output.setFocus()

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

    # ---------------- terminal toolbar features ----------------
    def _require_ssh(self):
        if self.active_kind != "ssh":
            QMessageBox.information(
                self, "SSH connection required",
                "File transfer uses the host's SSH connection. Switch the Connection "
                "picker to SSH (or enroll this host over SSH) to transfer files.")
            return False
        return True

    def upload_to_host(self):
        if not self._require_ssh():
            return
        local, _ = QFileDialog.getOpenFileName(self, "Select a file to upload")
        if not local:
            return
        remote, ok = QInputDialog.getText(
            self, "Upload", "Remote destination path (file or directory):",
            text=f"/tmp/{os.path.basename(local)}")
        if not ok or not remote.strip():
            return
        try:
            api.upload_file_ssh(self.active_id, local, remote.strip())
            self.output.append_terminal_text(
                f"\n[uploaded {os.path.basename(local)} -> {remote.strip()}]\n")
        except Exception as e:
            QMessageBox.critical(self, "Upload failed", str(e))

    def download_from_host(self):
        if not self._require_ssh():
            return
        remote, ok = QInputDialog.getText(self, "Download", "Remote file path to fetch:")
        if not ok or not remote.strip():
            return
        local, _ = QFileDialog.getSaveFileName(
            self, "Save downloaded file as", os.path.basename(remote.strip()))
        if not local:
            return
        try:
            api.download_file_ssh(self.active_id, remote.strip(), local)
            self.output.append_terminal_text(f"\n[downloaded {remote.strip()} -> {local}]\n")
        except Exception as e:
            QMessageBox.critical(self, "Download failed", str(e))

    def find_in_output(self):
        text, ok = QInputDialog.getText(self, "Find in output", "Search for:")
        if not ok or not text:
            return
        # Search forward from the current cursor; wrap to the top if not found.
        if not self.output.find(text):
            cur = self.output.textCursor()
            cur.movePosition(_CURSOR_START)
            self.output.setTextCursor(cur)
            if not self.output.find(text):
                QMessageBox.information(self, "Not found", f"'{text}' was not found in the output.")

    def save_output(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save terminal output", "terminal-output.txt")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.output.toPlainText())
        except Exception as e:
            QMessageBox.critical(self, "Save failed", str(e))

    def _apply_terminal_font(self, font):
        """Apply `font` to the terminal output and, for a live SSH grid,
        re-measure the cell size and resize the remote pty/grid to match."""
        self.output.setFont(font)
        if getattr(self, "cmd_input", None) is not None:
            self.cmd_input.setFont(font)
        if self._ssh_session_active and self._session_id is not None:
            cols, rows = self.output.grid_size_for_viewport()
            self.output.resize_grid(cols, rows)
            self._terminal_io.submit_resize(self._session_id, cols, rows)

    def adjust_font(self, delta):
        font = self.output.font()
        size = max(7, min(28, font.pointSize() + delta))
        font.setPointSize(size)
        self._apply_terminal_font(font)

    def set_font_family(self, family):
        if not family:
            return
        font = self.output.font()
        font.setFamily(family)
        self._apply_terminal_font(font)

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
            # Elevate typed commands the same way the fleet tools do: on a
            # password-sudo host, attach the operator's stored sudo password so
            # a privileged command escalates instead of bouncing off `sudo -n`.
            task_ids = api.queue_command_on_hosts(
                [self.active_id], cmd,
                become_password=api.become_password_for_host(self.active_id))
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

        self.setWindowTitle("Sysible Connect")
        self.resize(1180, 760)

        self.environments = []
        self._collapsed_envs = set()

        self._checkin_status = {}           # label -> {"state","detail","color"}
        self._dot_cache = {}                # color -> QIcon
        self._checkin_worker = None

        self.active_entry = None
        self.active_kind = None
        self.active_id = None
        self.active_label = None

        self.pending_file_task = None      # agent file transfer

        self._terminals = []               # list of open TerminalPopout windows

        main = QVBoxLayout()
        self.setLayout(main)

        main.addLayout(make_page_header("Sysible Connect"))

        body = QHBoxLayout()

        # ---------------- LEFT: host list (consistent column) ----------------
        self.host_list = QListWidget()
        connect_group_toggle(self.host_list)
        self.host_list.itemSelectionChanged.connect(self.on_host_selected)
        self.host_list.itemDoubleClicked.connect(self.open_terminal_for_item)
        # Right-click a host to assign it to an environment.
        self.host_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.host_list.customContextMenuRequested.connect(self._host_context_menu)

        btn_refresh = QPushButton("Refresh")
        btn_refresh.clicked.connect(self.load_hosts)
        btn_collapse_all, btn_expand_all = add_collapse_expand_buttons(self.host_list)
        self.btn_checkin = QPushButton("Check In / Ping")
        self.btn_checkin.setToolTip(
            "Check every host: agent hosts by their last heartbeat, SSH hosts by a "
            "live connection test. A colored dot and the result appear next to each host."
        )
        self.btn_checkin.clicked.connect(self.check_in_hosts)
        # Fleet-wide actions - these act on the whole fleet, not the host list
        # itself, so they live in the right-hand panel (Fleet Actions box),
        # not stacked on top of the list. Created here, placed in
        # _build_content_panel().
        self.btn_script = QPushButton("Run Script on All Hosts…")
        self.btn_script.setToolTip("Run an ad-hoc command or multi-line script across checked hosts.")
        self.btn_script.clicked.connect(self.open_script_runner)

        self.btn_rdp = QPushButton("RDP To A Windows Host…")
        self.btn_rdp.setToolTip("Open a Remote Desktop (RDP) session to a Windows host by address, "
                                "or right-click an enrolled host to RDP to it.")
        self.btn_rdp.clicked.connect(lambda: self._open_rdp(""))

        # Fleet power / agent control - act on every host in the list.
        self.btn_restart_agent = QPushButton("Restart Agent on All Hosts")
        self.btn_restart_agent.setToolTip("Restart the Sysible agent service on every agent host "
                                          "(detached; each host reconnects shortly).")
        self.btn_restart_agent.clicked.connect(
            lambda: self._fleet_action(api.cmd_restart_agent(), "restart the agent on", danger=False))
        self.btn_reboot_all = QPushButton("Reboot All Hosts")
        self.btn_reboot_all.setToolTip("Reboot every host in the list.")
        self.btn_reboot_all.clicked.connect(
            lambda: self._fleet_action(api.cmd_reboot_host(), "REBOOT", danger=True))
        self.btn_poweroff_all = QPushButton("Power Off All Hosts")
        self.btn_poweroff_all.setToolTip("Shut down and power off every host in the list.")
        self.btn_poweroff_all.clicked.connect(
            lambda: self._fleet_action(api.cmd_poweroff_host(), "POWER OFF", danger=True))
        for b in (self.btn_reboot_all, self.btn_poweroff_all):
            b.setStyleSheet("color:#f0c0bc;")

        self.checkin_label = QLabel("")
        theme.style_hint_label(self.checkin_label)

        # Compact list-management toolbar that sits BELOW the list (via
        # extra_widgets) so the panel reads as "just the host list" with its
        # own controls underneath, instead of a stack of buttons on top.
        list_tools = QWidget()
        lt = QVBoxLayout(list_tools)
        lt.setContentsMargins(0, 0, 0, 0)
        lt.setSpacing(6)
        for pair in ([btn_refresh, self.btn_checkin], [btn_collapse_all, btn_expand_all]):
            row = QHBoxLayout()
            row.setSpacing(6)
            row.setContentsMargins(0, 0, 0, 0)
            for b in pair:
                b.setMinimumHeight(28)
                row.addWidget(b)
            lt.addLayout(row)

        host_panel = build_host_panel(
            "Managed Hosts (agent + SSH)",
            self.host_list,
            [],  # no buttons stacked above the list
            extra_widgets=[list_tools, self.checkin_label],
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
        col.setSpacing(12)

        # ---- selected host ----
        sel_box = QGroupBox("Selected Host")
        sel = QVBoxLayout(sel_box)

        self.active_host_label = QLabel("(no host selected)")
        self.active_host_label.setStyleSheet("font-weight: bold;")
        sel.addWidget(self.active_host_label)

        self.connection_label = QLabel("")
        self.connection_label.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        sel.addWidget(self.connection_label)

        open_hint = QLabel("Double-click a host in the list to open its terminal in a new window.")
        theme.style_hint_label(open_hint)
        sel.addWidget(open_hint)

        col.addWidget(sel_box)

        # ---- fleet actions (act on the whole fleet / checked hosts) ----
        fleet_box = QGroupBox("Fleet Actions (all hosts)")
        fleet = QVBoxLayout(fleet_box)
        fleet.addWidget(self.btn_script)
        fleet.addWidget(self.btn_restart_agent)
        power_row = QHBoxLayout()
        power_row.addWidget(self.btn_reboot_all)
        power_row.addWidget(self.btn_poweroff_all)
        fleet.addLayout(power_row)
        col.addWidget(fleet_box)

        # ---- file transfer ----
        file_box = QGroupBox("File Transfer (selected host)")
        col_file = QVBoxLayout(file_box)

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
        col_file.addLayout(upload_row)

        download_row = QHBoxLayout()
        self.download_remote_path = QLineEdit()
        self.download_remote_path.setPlaceholderText("Remote file path to fetch")
        self.download_btn = QPushButton("Download...")
        self.download_btn.clicked.connect(self.download_file)
        download_row.addWidget(self.download_remote_path, 3)
        download_row.addWidget(self.download_btn)
        col_file.addLayout(download_row)

        self.file_status = QLabel(
            f"Agent-host transfers are limited to ~{api.AGENT_FILE_TRANSFER_LIMIT_BYTES // 1000} KB; "
            "SSH hosts have no such limit."
        )
        self.file_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        self.file_status.setWordWrap(True)
        col_file.addWidget(self.file_status)
        col.addWidget(file_box)

        # ---- connect a new SSH host ----
        enroll_box = QGroupBox("SSH to a New Host (Not Yet Joined)")
        col_enroll = QVBoxLayout(enroll_box)

        enroll_hint = QLabel(
            "Only needed once per host. The password installs the controller key, then is "
            "discarded - after that the host appears in the list and connects with no password."
        )
        theme.style_hint_label(enroll_hint)
        col_enroll.addWidget(enroll_hint)

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
        col_enroll.addLayout(enroll_row)

        connect_buttons = QHBoxLayout()
        enroll_btn = QPushButton("Connect Host")
        enroll_btn.clicked.connect(self.connect_host)
        show_key_btn = QPushButton("Show Controller Public Key")
        show_key_btn.clicked.connect(self.show_controller_key)
        connect_buttons.addWidget(enroll_btn)
        connect_buttons.addWidget(show_key_btn)
        connect_buttons.addStretch()
        col_enroll.addLayout(connect_buttons)

        self.enroll_status = QLabel()
        self.enroll_status.setStyleSheet(f"color: {STATUS_NEUTRAL_COLOR};")
        self.enroll_status.setWordWrap(True)
        col_enroll.addWidget(self.enroll_status)
        col.addWidget(enroll_box)

        # ---- RDP to a Windows host (a different way to reach a new box,
        # so it sits right under "SSH to a New Host") ----
        rdp_box = QGroupBox("RDP To A Windows Host")
        rdp_v = QVBoxLayout(rdp_box)
        rdp_hint = QLabel(
            "Open a graphical Remote Desktop session to a Windows host by address "
            "(or right-click an enrolled host in the list to RDP to it)."
        )
        theme.style_hint_label(rdp_hint)
        rdp_hint.setWordWrap(True)
        rdp_v.addWidget(rdp_hint)
        rdp_row = QHBoxLayout()
        rdp_row.addWidget(self.btn_rdp)
        rdp_row.addStretch()
        rdp_v.addLayout(rdp_row)
        col.addWidget(rdp_box)

        col.addStretch()

        # ---- remove host (de-emphasized, at the very bottom) ----
        # A destructive, infrequent action, so it sits out of the way under
        # everything else rather than up next to the host details - a small,
        # quiet link-style button aligned to the right.
        remove_row = QHBoxLayout()
        remove_row.addStretch()
        self.remove_host_btn = QPushButton("Remove selected host")
        self.remove_host_btn.setToolTip(
            "Remove the selected host from Sysible (drops its agent and/or SSH connection).")
        self.remove_host_btn.setCursor(Qt.PointingHandCursor)
        self.remove_host_btn.setFlat(True)
        self.remove_host_btn.setStyleSheet(
            "QPushButton{color:#9aa5b1; border:none; padding:4px 6px; font-size:11px;"
            "text-decoration:underline;}"
            "QPushButton:hover{color:#f0c0bc;}"
        )
        self.remove_host_btn.clicked.connect(self.delete_host)
        remove_row.addWidget(self.remove_host_btn)
        col.addLayout(remove_row)

        return panel

    # =====================================================
    # HOSTS
    # =====================================================
    def open_script_runner(self):
        """Open the 'run an ad-hoc command/script across checked hosts' tool
        as a child window of Sysible Connect."""
        from client.automation_page import AutomationPage
        if getattr(self, "_script_window", None) is None:
            self._script_window = AutomationPage()
        self._script_window.show()
        self._script_window.raise_()
        self._script_window.activateWindow()

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

        # Saved Windows / RDP hosts - double-click to RDP straight in (1-click
        # when the password was remembered). These live on this workstation
        # (rdp_credentials), not in the enrolled fleet.
        from client import rdp_credentials
        try:
            rdp_hosts = rdp_credentials.list_hosts()
        except Exception:
            rdp_hosts = []
        if rdp_hosts:
            self._add_host_header("Windows Hosts (RDP)")
            for name in rdp_hosts:
                self._add_rdp_row(name)

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
        status = self._checkin_status.get(entry["label"])
        if status:
            text += f"   —  {status['detail']}"
        item = QListWidgetItem(text)
        if status:
            item.setIcon(self._dot_icon(status["color"]))
        item.setData(Qt.UserRole, entry)
        self.host_list.addItem(item)

    def _add_rdp_row(self, name):
        """A saved Windows/RDP target row. Double-click connects (1-click when
        a password is stored). Marked kind='rdp' so it's skipped by fleet
        actions and check-in, which only apply to enrolled agent/SSH hosts."""
        item = QListWidgetItem(f"    {name}  [RDP]")
        item.setData(Qt.UserRole, {"kind": "rdp", "label": name, "id": name, "type_text": "RDP"})
        self.host_list.addItem(item)

    @staticmethod
    def _is_rdp(entry):
        return bool(entry) and entry.get("kind") == "rdp"

    def _rdp_connect_saved(self, host):
        """1-click RDP to a saved Windows host: launch straight away with the
        remembered credentials; if no password was stored, open the dialog
        prefilled so it can be entered."""
        from client import rdp_credentials, rdp_launcher
        saved = rdp_credentials.load(host) or {}
        if not saved.get("password"):
            self._open_rdp(host)
            return
        screen_size = None
        try:
            scr = QApplication.primaryScreen()
            if scr is not None:
                g = scr.availableGeometry()
                dpr = scr.devicePixelRatio() or 1.0
                screen_size = f"{int(round(g.width() * dpr))}x{int(round(g.height() * dpr))}"
        except Exception:
            screen_size = None
        ok, msg = rdp_launcher.launch(
            host, saved.get("username", ""), saved.get("domain", ""),
            saved.get("password", ""), "dynamic", screen_size=screen_size)
        if not ok:
            QMessageBox.critical(self, "RDP connection failed", msg)

    def _forget_rdp_host(self, name):
        from client import rdp_credentials
        if QMessageBox.question(
                self, "Forget host",
                f"Remove the saved RDP details for '{name}'?",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        rdp_credentials.forget(name)
        self.load_hosts()

    def _dot_icon(self, color):
        """A small filled circle in `color`, cached per color, used as a
        host row's status dot after a check-in."""
        icon = self._dot_cache.get(color)
        if icon is None:
            pix = QPixmap(12, 12)
            pix.fill(Qt.transparent)
            p = QPainter(pix)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(color))
            p.drawEllipse(1, 1, 10, 10)
            p.end()
            icon = QIcon(pix)
            self._dot_cache[color] = icon
        return icon

    # =====================================================
    # CHECK-IN / PING
    # =====================================================
    # ---------------- fleet power / agent control ----------------
    def _fleet_action(self, command, verb, danger=True):
        """Run `command` on every host in the list after confirming. `verb`
        is shown in the prompt (e.g. 'REBOOT', 'restart the agent on')."""
        if getattr(self, "_fleet_worker", None) is not None and self._fleet_worker.isRunning():
            QMessageBox.information(self, "Busy", "A fleet action is already running.")
            return
        entries = []
        for i in range(self.host_list.count()):
            entry = self.host_list.item(i).data(Qt.UserRole)
            if entry is not None and not self._is_rdp(entry):
                entries.append(entry)
        if not entries:
            QMessageBox.information(self, "No hosts", "There are no hosts in the list.")
            return

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning if danger else QMessageBox.Question)
        box.setWindowTitle("Confirm fleet action")
        box.setText(f"This will {verb} all {len(entries)} host(s) in the list.")
        if danger:
            box.setInformativeText("This is disruptive and affects every listed host. Continue?")
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        if box.exec() != QMessageBox.Yes:
            return

        for b in (self.btn_restart_agent, self.btn_reboot_all, self.btn_poweroff_all):
            b.setEnabled(False)
        self.checkin_label.setText(f"Dispatching to {len(entries)} host(s)…")
        self._fleet_worker = _FleetCommandWorker(entries, command)
        self._fleet_worker.done.connect(self._on_fleet_done)
        self._fleet_worker.start()

    def _on_fleet_done(self, results):
        for b in (self.btn_restart_agent, self.btn_reboot_all, self.btn_poweroff_all):
            b.setEnabled(True)
        ok = sum(1 for r in results if r["ok"])
        self.checkin_label.setText(f"Done: {ok}/{len(results)} host(s) succeeded.")

        dlg = QDialog(self)
        dlg.setWindowTitle("Fleet action results")
        dlg.resize(560, 420)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(f"{ok} of {len(results)} host(s) reported success."))
        view = QTextEdit()
        view.setReadOnly(True)
        view.setStyleSheet("font-family: monospace;")
        lines = []
        for r in results:
            mark = "[ OK ]" if r["ok"] else "[FAIL]"
            lines.append(f"{mark}  {r['label']}\n        " + r["output"].replace("\n", "\n        "))
        view.setPlainText("\n\n".join(lines))
        v.addWidget(view)
        close = QPushButton("Close")
        close.clicked.connect(dlg.accept)
        v.addWidget(close)
        dlg.exec()

    def check_in_hosts(self):
        """Probe every host in the list and show a status dot next to each:
        agent hosts are judged by how recently they last checked in
        (heartbeat); SSH hosts by a live, timed connection test. Runs in a
        background thread so the GUI stays responsive."""
        if self._checkin_worker is not None and self._checkin_worker.isRunning():
            return
        entries = []
        for i in range(self.host_list.count()):
            entry = self.host_list.item(i).data(Qt.UserRole)
            if entry is not None and not self._is_rdp(entry):
                entries.append(entry)
        if not entries:
            self.checkin_label.setText("No hosts to check.")
            return
        self.btn_checkin.setEnabled(False)
        self.checkin_label.setText(f"Checking {len(entries)} host(s)…")
        self._checkin_worker = _CheckInWorker(entries)
        self._checkin_worker.done.connect(self._on_checkin_done)
        self._checkin_worker.start()

    def _on_checkin_done(self, results):
        self._checkin_status = results
        online = sum(1 for s in results.values() if s["state"] == "online")
        self.checkin_label.setText(f"{online} of {len(results)} host(s) online")
        self.btn_checkin.setEnabled(True)
        self._checkin_worker = None
        # Redraw rows so each picks up its new dot + detail.
        self.load_hosts()

    # =====================================================
    # RIGHT-CLICK: ASSIGN ENVIRONMENT
    # =====================================================
    def _host_context_menu(self, pos):
        item = self.host_list.itemAt(pos)
        if item is None:
            return
        entry = item.data(Qt.UserRole)
        if not entry:
            return  # environment header row, not a host

        # Saved Windows/RDP host: connect or forget; no environment assignment.
        if self._is_rdp(entry):
            menu = QMenu(self)
            menu.addAction(f"Connect to “{entry['label']}” (RDP)").triggered.connect(
                lambda _checked=False, n=entry["label"]: self._rdp_connect_saved(n))
            menu.addAction(f"Edit / reconnect “{entry['label']}”…").triggered.connect(
                lambda _checked=False, n=entry["label"]: self._open_rdp(n))
            menu.addSeparator()
            menu.addAction(f"Forget “{entry['label']}”").triggered.connect(
                lambda _checked=False, n=entry["label"]: self._forget_rdp_host(n))
            menu.exec(self.host_list.viewport().mapToGlobal(pos))
            return

        current = entry.get("environment") or ""

        menu = QMenu(self)
        act_rdp = menu.addAction(f"Open RDP to “{entry['label']}”…")
        act_rdp.triggered.connect(
            lambda _checked=False, e=entry: self._open_rdp(self._rdp_default_host(e))
        )
        menu.addSeparator()
        sub = menu.addMenu(f"Assign “{entry['label']}” to environment")
        for env in self.environments:
            act = sub.addAction(env)
            act.setCheckable(True)
            act.setChecked(env == current)
            act.triggered.connect(
                lambda _checked=False, e=entry, n=env: self._assign_environment(e, n)
            )
        sub.addSeparator()
        act_unassigned = sub.addAction("Unassigned")
        act_unassigned.setCheckable(True)
        act_unassigned.setChecked(current == "")
        act_unassigned.triggered.connect(
            lambda _checked=False, e=entry: self._assign_environment(e, "")
        )
        sub.addSeparator()
        act_new = sub.addAction("New environment…")
        act_new.triggered.connect(
            lambda _checked=False, e=entry: self._assign_new_environment(e)
        )
        menu.exec(self.host_list.viewport().mapToGlobal(pos))

    @staticmethod
    def _rdp_default_host(entry):
        """Best-guess RDP target for an enrolled host: its address (IP/host),
        falling back to its label. The dialog lets the operator edit it."""
        if entry.get("kind") == "merged":
            sub = entry.get("agent_entry") or entry.get("ssh_entry") or {}
            addr = sub.get("address", "")
        else:
            addr = entry.get("address", "")
        addr = (addr or "").split()[0] if addr else ""
        if "@" in addr:
            addr = addr.split("@", 1)[1]
        return addr or entry.get("label", "")

    def _open_rdp(self, host):
        from client.rdp_dialog import RdpConnectDialog
        RdpConnectDialog(host=host, parent=self).exec()

    def _assign_new_environment(self, entry):
        name, ok = QInputDialog.getText(self, "New environment", "Environment name:")
        name = (name or "").strip()
        if not ok or not name:
            return
        try:
            self.environments = api.create_environment(name) or self.environments
        except Exception as e:
            QMessageBox.warning(self, "Environment", f"Could not create environment: {e}")
            return
        self._assign_environment(entry, name)

    def _assign_environment(self, entry, env):
        """Set (or clear, when env == "") the environment for a host. A
        merged host is updated on both its agent and SSH sides so the two
        don't drift apart."""
        try:
            kind = entry["kind"]
            if kind == "merged":
                agent = entry.get("agent_entry") or {}
                ssh = entry.get("ssh_entry") or {}
                if agent.get("id"):
                    api.set_agent_environment(agent["id"], env)
                if ssh.get("id"):
                    api.set_host_environment(ssh["id"], env)
            elif kind == "agent":
                api.set_agent_environment(entry["id"], env)
            else:  # ssh
                api.set_host_environment(entry["id"], env)
        except Exception as e:
            QMessageBox.warning(self, "Environment", f"Could not set environment: {e}")
            return
        self.load_hosts()

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
        self.active_host_label.setText("(no host selected)")
        self.connection_label.setText("")

    def on_host_selected(self):
        entry = self._selected_entry()
        if entry is None:
            return  # header row

        # Saved Windows/RDP host: no terminal/file-transfer; just show how to
        # connect. Don't run it through the agent/SSH "underlying" logic.
        if self._is_rdp(entry):
            self.active_entry = entry
            self.active_kind = "rdp"
            self.active_id = entry["label"]
            self.active_label = entry["label"]
            self.active_host_label.setText(f"{entry['label']}  [Saved RDP host]")
            self.connection_label.setText(
                "Double-click to open a Remote Desktop session (right-click to forget).")
            return

        self.active_entry = entry
        underlying = _default_underlying(entry)
        self.active_kind = underlying["kind"]
        self.active_id = underlying["id"]
        self.active_label = entry["label"]

        self.active_host_label.setText(f"{entry['label']}  [{entry['type_text']}]")

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

        # A saved Windows host: double-click connects over RDP, not a terminal.
        if self._is_rdp(entry):
            self._rdp_connect_saved(entry["label"])
            return

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

        # Don't let the same machine be enrolled twice. The controller enforces
        # this too (409), but checking here gives an immediate, clear message
        # instead of a round-trip failure.
        existing = self._managed_host_at_ip(ip)
        if existing and existing != name:
            self.enroll_status.setStyleSheet(f"color: {STATUS_ERROR_COLOR};")
            self.enroll_status.setText(
                f"{ip} is already managed as '{existing}'. Remove that host first "
                "if you want to re-enroll it.")
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

    def _managed_host_at_ip(self, ip):
        """Name of an already-managed host (agent or SSH) at `ip`, or None -
        used to block enrolling the same machine twice."""
        ip = (ip or "").strip()
        if not ip:
            return None
        try:
            for entry in api.list_merged_hosts(agent_only=False):
                if _row_ip(entry) == ip:
                    return entry.get("label")
        except Exception:
            pass
        return None

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
