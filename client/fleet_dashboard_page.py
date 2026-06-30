"""Fleet Health & Compliance — desktop GUI parity with the web console's merged
Fleet panel. One window showing, per environment: a health rollup (verdict, peak
disk/mem, problem signals) plus compliance ("N need attention" from the posture
sweep), expandable to per-host rows that drill into a full posture breakdown.
Plus the OK/Warning/Critical donut and the high-ticket compliance signal strip.

Theme-aware (light/dark via client.theme): all chrome colors come from a palette
that flips on theme change, so light mode is legible. Data comes from
client/_fleet_status.py (the same read-only, root-dispatched gather the browser
console uses); sweeps run on background QThreads so the UI never blocks.
"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QProgressBar, QDialog,
)
from PySide6.QtGui import QPainter, QPen, QColor
from PySide6.QtCore import Qt, QThread, Signal, QRectF

from client import _fleet_status as fs
from client import theme

# Verdict colors read the same in both themes (they're status signals).
VERDICT = {"OK": "#4ec07a", "WARNING": "#e0a83a", "CRITICAL": "#e06c6c", "OFFLINE": "#7a7a7a"}
_ORDER = {"CRITICAL": 0, "WARNING": 1, "OFFLINE": 2, "OK": 3}

# Theme palette — repopulated from the current mode by _refresh_pal(). Chrome
# (card/row backgrounds, borders, text) flips with light/dark; widgets read PAL
# at build time, and the page rebuilds on theme change.
PAL = {}


def _refresh_pal():
    light = theme.get_theme_mode() == "light"
    PAL.update(
        bg="#FFFFFF" if light else "#232a36",
        border="#D5DAE2" if light else "#3a4250",
        text="#1F2430" if light else "#EAEAEA",
        faint="#6B7280" if light else "#9aa5b1",
        track="#E3E8F0" if light else "#2a2f3a",
        row="#F5F7FA" if light else "#2a313d",
        row_hover="#E9EDF2" if light else "#313947",
        # Blue env-card header band — breaks the gray/white card list into
        # clearly delimited environments.
        header="#3F5C8C" if light else "#2C3E5F",
        header_text="#FFFFFF",
        header_faint="#C9D4E8" if light else "#9fb0cc",
    )


_refresh_pal()


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------
class _Worker(QThread):
    done = Signal(object)
    fail = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            self.done.emit(self._fn())
        except Exception as e:
            self.fail.emit(str(e))


# ---------------------------------------------------------------------------
# Small painted widgets
# ---------------------------------------------------------------------------
class Donut(QWidget):
    def __init__(self, size=120):
        super().__init__()
        self._segs = []
        self._size = size
        self.setFixedSize(size, size)

    def set_segments(self, segs):
        self._segs = segs
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        total = sum(v for v, _ in self._segs)
        stroke = max(12, self._size // 7)
        m = stroke / 2 + 2
        rect = QRectF(m, m, self._size - 2 * m, self._size - 2 * m)
        pen = QPen(QColor(PAL["track"]))
        pen.setWidth(stroke)
        pen.setCapStyle(Qt.FlatCap)
        p.setPen(pen)
        p.drawArc(rect, 0, 360 * 16)
        if total > 0:
            start = 90 * 16
            for v, c in self._segs:
                if v <= 0:
                    continue
                span = -int(round(360 * 16 * v / total))
                pen = QPen(QColor(c))
                pen.setWidth(stroke)
                pen.setCapStyle(Qt.FlatCap)
                p.setPen(pen)
                p.drawArc(rect, start, span)
                start += span
        p.setPen(QColor(PAL["text"]))
        f = p.font()
        f.setPointSize(max(12, int(self._size * 0.16)))
        f.setBold(True)
        p.setFont(f)
        p.drawText(rect, Qt.AlignCenter, str(total))
        p.end()


def _meter(label, pct):
    """A label + colored bar + percentage row (disk/mem)."""
    w = QWidget()
    row = QHBoxLayout(w)
    row.setContentsMargins(0, 1, 0, 1)
    row.setSpacing(8)
    lab = QLabel(label)
    lab.setStyleSheet(f"border:none; color:{PAL['faint']}; font-size:11px;")
    lab.setFixedWidth(34)
    row.addWidget(lab)
    bar = QProgressBar()
    bar.setRange(0, 100)
    bar.setTextVisible(False)
    bar.setFixedHeight(8)
    v = 0 if pct is None else max(0, min(100, int(pct)))
    bar.setValue(v)
    color = "#7a7a7a" if pct is None else ("#e06c6c" if v >= 90 else "#e0a83a" if v >= 75 else "#4ec07a")
    bar.setStyleSheet(
        f"QProgressBar{{background:{PAL['track']};border:none;border-radius:4px;}}"
        f"QProgressBar::chunk{{background:{color};border-radius:4px;}}")
    row.addWidget(bar, 1)
    val = QLabel("—" if pct is None else f"{v}%")
    val.setStyleSheet(f"border:none; color:{PAL['faint']}; font-size:11px;")
    val.setFixedWidth(36)
    val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    row.addWidget(val)
    return w


def _dot(color, size=9):
    d = QLabel()
    d.setFixedSize(size, size)
    d.setStyleSheet(f"border:none; background:{color}; border-radius:{size // 2}px;")
    return d


def _card_frame():
    f = QFrame()
    f.setStyleSheet(f"QFrame{{border:1px solid {PAL['border']}; border-radius:8px; background:{PAL['bg']};}}")
    return f


# ---------------------------------------------------------------------------
# Compliance signal chip
# ---------------------------------------------------------------------------
def _signal_chip(label, count):
    f = _card_frame()
    lay = QHBoxLayout(f)
    lay.setContentsMargins(10, 8, 10, 8)
    color = "#e0a83a" if count > 0 else "#4ec07a"
    lay.addWidget(_dot(color))
    name = QLabel(label)
    name.setStyleSheet(f"border:none; color:{PAL['text']}; font-size:12px;")
    lay.addWidget(name, 1)
    cnt = QLabel(str(count))
    cnt.setStyleSheet(f"border:none; font-weight:bold; color:{color};")
    lay.addWidget(cnt)
    return f


# ---------------------------------------------------------------------------
# Per-environment card (collapsible)
# ---------------------------------------------------------------------------
class EnvCard(QFrame):
    def __init__(self, group, posture_loaded, on_open_host):
        super().__init__()
        self.setStyleSheet(f"QFrame{{border:1px solid {PAL['border']}; border-radius:8px; background:{PAL['bg']};}}")
        self._on_open_host = on_open_host
        v = group["verdict"]
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Header — a blue band (clickable to expand). Its top corners are rounded
        # to match the card so the blue never bleeds past the card's edge.
        head = QPushButton()
        head.setCursor(Qt.PointingHandCursor)
        head.setStyleSheet(
            f"QPushButton{{border:none; background:{PAL['header']}; text-align:left;"
            f" padding:8px 10px; color:{PAL['header_text']};"
            f" border-top-left-radius:8px; border-top-right-radius:8px;}}")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(8)
        self._chev = QLabel("▶")
        self._chev.setStyleSheet(f"border:none; background:transparent; color:{PAL['header_faint']}; font-size:10px;")
        hl.addWidget(self._chev)
        hl.addWidget(_dot(VERDICT.get(v, "#7a7a7a")))
        name = QLabel(f"<b>{group['env']}</b>")
        name.setStyleSheet(f"border:none; background:transparent; color:{PAL['header_text']};")
        hl.addWidget(name)
        cnt = QLabel(f"{len(group['hosts'])} host{'' if len(group['hosts']) == 1 else 's'}")
        cnt.setStyleSheet(f"border:none; background:transparent; color:{PAL['header_faint']}; font-size:11px;")
        hl.addWidget(cnt)
        hl.addStretch()
        if posture_loaded:
            prob = group.get("problematic", 0)
            lim = group.get("limited", 0)
            txt = f"{prob} need attention" if prob else "all clear"
            if lim:
                txt += f"   ·   {lim} limited"
            att = QLabel(txt)
            att.setStyleSheet(
                f"border:none; background:transparent; font-size:11px; font-weight:bold;"
                f" color:{'#FFD27D' if prob else '#9BE8B8'};")
            hl.addWidget(att)
        head.clicked.connect(self._toggle)
        outer.addWidget(head)

        # always-visible body: meters + problem signals
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(10, 0, 10, 8)
        bl.setSpacing(2)
        bl.addWidget(_meter("disk", group.get("disk")))
        bl.addWidget(_meter("mem", group.get("mem")))
        sig = []
        if group.get("failed"):
            sig.append(f"{group['failed']} crashed service{'' if group['failed'] == 1 else 's'}")
        if group.get("oom"):
            sig.append(f"{group['oom']} OOM kill{'' if group['oom'] == 1 else 's'}")
        if group.get("degraded"):
            sig.append(f"{group['degraded']} degraded systemd")
        if sig:
            s = QLabel(" · ".join(sig))
            s.setStyleSheet("border:none; background:transparent; color:#e0a83a; font-size:12px;")
            bl.addWidget(s)
        outer.addWidget(body)

        # expandable host list
        self._hosts_box = QWidget()
        hb = QVBoxLayout(self._hosts_box)
        hb.setContentsMargins(10, 0, 10, 10)
        hb.setSpacing(6)
        for h in group["hosts"]:
            hb.addWidget(self._host_row(h))
        self._hosts_box.setVisible(False)
        outer.addWidget(self._hosts_box)
        if v != "OK" or group.get("problematic", 0) > 0:
            self._toggle()

    def _toggle(self):
        vis = not self._hosts_box.isVisible()
        self._hosts_box.setVisible(vis)
        self._chev.setText("▼" if vis else "▶")

    def _host_row(self, h):
        btn = QPushButton()
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(
            f"QPushButton{{border:1px solid {PAL['border']}; border-radius:8px; text-align:left;"
            f" padding:8px 10px; background:{PAL['row']}; color:{PAL['text']};}}"
            f"QPushButton:hover{{background:{PAL['row_hover']};}}")
        rl = QHBoxLayout(btn)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(8)
        verdict = (h.get("verdict") or "OK").upper()
        rl.addWidget(_dot(VERDICT.get(verdict, "#7a7a7a")))
        nm = QLabel(f"<b>{h.get('host')}</b>")
        nm.setStyleSheet(f"border:none; background:transparent; color:{PAL['text']};")
        rl.addWidget(nm)
        rl.addStretch()
        issues = h.get("issues")
        if issues:
            iss = QLabel(f"⚠ {issues}")
            iss.setStyleSheet("border:none; background:transparent; color:#e0a83a; font-weight:bold; font-size:11px;")
            rl.addWidget(iss)
        if h.get("limited"):
            lm = QLabel("limited")
            lm.setStyleSheet(f"border:none; background:transparent; color:{PAL['faint']}; font-size:11px;")
            rl.addWidget(lm)
        disk = h.get("disk")
        meta = QLabel(("offline" if verdict == "OFFLINE" else
                       (f"disk {disk}% · mem {h.get('mem')}%" if disk is not None else (h.get("error") or "no data"))))
        meta.setStyleSheet(f"border:none; background:transparent; color:{PAL['faint']}; font-size:11px;")
        rl.addWidget(meta)
        hid = h.get("id")
        if hid:
            btn.clicked.connect(lambda _=False, i=hid, lbl=h.get("host"): self._on_open_host(i, lbl))
        return btn


# ---------------------------------------------------------------------------
# Per-host posture drill-down dialog
# ---------------------------------------------------------------------------
_SECTIONS = [
    ("Operating System", ["os", "sub", "reboot"]),
    ("Security & Hardening", ["mac", "sec", "fw"]),
    ("Users & Accounts", ["users"]),
    ("SSH", ["ssh"]),
    ("Filesystem", ["fs", "mount"]),
    ("Time Synchronization", ["time"]),
    ("Logging", ["log"]),
    ("Networking", ["net"]),
    ("TLS Certificates", ["cert"]),
    ("Services", ["svc"]),
    ("Hardware", ["hw"]),
    ("Virtualization & Containers", ["virt", "cont"]),
    ("Identity & Directory", ["ad"]),
    ("Performance & Misc", ["perf", "misc"]),
]


class PostureDialog(QDialog):
    def __init__(self, host_id, label, parent=None):
        super().__init__(parent)
        _refresh_pal()
        self._host_id = host_id
        self.setWindowTitle(f"Posture — {label}")
        self.resize(900, 720)
        v = QVBoxLayout(self)
        topr = QHBoxLayout()
        self._title = QLabel(f"<b>{label}</b>")
        topr.addWidget(self._title)
        topr.addStretch()
        self._refresh = QPushButton("Refresh posture")
        self._refresh.clicked.connect(self._load)
        topr.addWidget(self._refresh)
        v.addLayout(topr)
        self._status = QLabel("Gathering posture…")
        self._status.setStyleSheet(f"color:{PAL['faint']};")
        v.addWidget(self._status)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._body = QWidget()
        self._grid = QGridLayout(self._body)
        self._grid.setSpacing(10)
        self._scroll.setWidget(self._body)
        v.addWidget(self._scroll, 1)
        self._worker = None
        self._load()

    def _load(self):
        self._status.setText("Gathering posture…")
        self._status.setStyleSheet(f"color:{PAL['faint']};")
        self._status.setVisible(True)
        self._refresh.setEnabled(False)
        self._worker = _Worker(lambda: fs.gather_host_posture(self._host_id))
        self._worker.done.connect(self._render)
        self._worker.fail.connect(lambda m: (self._status.setText(f"Error: {m}"), self._refresh.setEnabled(True)))
        self._worker.start()

    def _render(self, data):
        self._refresh.setEnabled(True)
        while self._grid.count():
            it = self._grid.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        posture = (data or {}).get("posture")
        if not posture:
            self._status.setText((data or {}).get("error") or "No posture data.")
            self._status.setVisible(True)
            return
        if (posture.get("meta") or {}).get("privileged") == "0":
            self._status.setText("⚠ Gathered without root — root-only checks may be blank; absence of findings is not a clean bill.")
            self._status.setStyleSheet("color:#e0a83a;")
            self._status.setVisible(True)
        else:
            self._status.setVisible(False)
        col_count = 2
        i = 0
        for title, cats in _SECTIONS:
            rows = []
            for cat in cats:
                for k, val in (posture.get(cat) or {}).items():
                    rows.append((name_for(k), val))
            if not rows:
                continue
            self._grid.addWidget(self._section(title, rows), i // col_count, i % col_count)
            i += 1

    def _section(self, title, rows):
        f = _card_frame()
        lay = QVBoxLayout(f)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(2)
        t = QLabel(title.upper())
        t.setStyleSheet(f"border:none; background:transparent; color:{PAL['faint']}; font-size:11px; font-weight:bold;")
        lay.addWidget(t)
        for name, val in rows:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            ln = QLabel(name)
            ln.setStyleSheet(f"border:none; background:transparent; color:{PAL['faint']}; font-size:12px;")
            row.addWidget(ln)
            row.addStretch()
            lv = QLabel("—" if val in ("", None) else str(val))
            lv.setStyleSheet(f"border:none; background:transparent; color:{PAL['text']}; font-size:12px;")
            lv.setWordWrap(True)
            lv.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            row.addWidget(lv)
            lay.addLayout(row)
        return f


def name_for(key):
    return key.replace("_", " ").title()


# ---------------------------------------------------------------------------
# Main page
# ---------------------------------------------------------------------------
class FleetDashboardPage(QWidget):
    def __init__(self):
        super().__init__()
        _refresh_pal()
        self.setWindowTitle("Fleet Health & Compliance")
        self.resize(1100, 860)
        self._health = []
        self._posture = []
        self._h_worker = None
        self._p_worker = None
        self._dialogs = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(12)

        head = QHBoxLayout()
        self._title = QLabel("Fleet Health & Compliance")
        head.addWidget(self._title)
        head.addStretch()
        self._scan_btn = QPushButton("Run posture scan")
        self._scan_btn.clicked.connect(self._load_posture)
        head.addWidget(self._scan_btn)
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self.refresh)
        head.addWidget(self._refresh_btn)
        outer.addLayout(head)

        self._status = QLabel("Gathering fleet health…")
        outer.addWidget(self._status)

        summary = QHBoxLayout()
        self._donut = Donut(120)
        summary.addWidget(self._donut)
        self._counts = QVBoxLayout()
        summary.addLayout(self._counts)
        summary.addSpacing(24)
        self._signals_box = QWidget()
        self._signals_grid = QGridLayout(self._signals_box)
        self._signals_grid.setSpacing(8)
        summary.addWidget(self._signals_box, 1)
        outer.addLayout(summary)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet("QScrollArea{border:none;}")
        self._envs_host = QWidget()
        self._envs_lay = QVBoxLayout(self._envs_host)
        self._envs_lay.setSpacing(10)
        self._envs_lay.addStretch()
        self._scroll.setWidget(self._envs_host)
        outer.addWidget(self._scroll, 1)

        self._apply_chrome()
        theme.add_theme_listener(self._on_theme)
        self.refresh()

    # ---- theme ----
    def _apply_chrome(self):
        self._title.setStyleSheet(f"font-size:20px; font-weight:bold; color:{PAL['text']};")
        self._status.setStyleSheet(f"color:{PAL['faint']};")

    def _on_theme(self):
        _refresh_pal()
        self._apply_chrome()
        self._rebuild()

    # ---- data loading ----
    def refresh(self):
        self._load_health()
        self._load_posture()

    def _load_health(self):
        self._status.setText("Gathering fleet health…")
        self._status.setVisible(True)
        self._refresh_btn.setEnabled(False)
        self._h_worker = _Worker(fs.gather_fleet_health)
        self._h_worker.done.connect(self._on_health)
        self._h_worker.fail.connect(self._fail)
        self._h_worker.start()

    def _load_posture(self):
        self._scan_btn.setEnabled(False)
        self._scan_btn.setText("Scanning…")
        self._p_worker = _Worker(fs.gather_fleet_posture)
        self._p_worker.done.connect(self._on_posture)
        self._p_worker.fail.connect(self._fail)
        self._p_worker.start()

    def _fail(self, msg):
        self._status.setText(f"Error: {msg}")
        self._status.setVisible(True)
        self._refresh_btn.setEnabled(True)
        self._scan_btn.setEnabled(True)
        self._scan_btn.setText("Run posture scan")

    def _on_health(self, hosts):
        self._health = hosts or []
        self._refresh_btn.setEnabled(True)
        self._status.setVisible(False)
        self._rebuild()

    def _on_posture(self, hosts):
        self._posture = hosts or []
        self._scan_btn.setEnabled(True)
        self._scan_btn.setText("Run posture scan")
        self._rebuild()

    # ---- rendering ----
    def _rebuild(self):
        counts = {"OK": 0, "WARNING": 0, "CRITICAL": 0, "OFFLINE": 0}
        for h in self._health:
            k = (h.get("verdict") or "OK").upper()
            counts[k] = counts.get(k, 0) + 1
        self._donut.set_segments([(counts["OK"], VERDICT["OK"]), (counts["WARNING"], VERDICT["WARNING"]),
                                  (counts["CRITICAL"], VERDICT["CRITICAL"]), (counts["OFFLINE"], VERDICT["OFFLINE"])])
        _clear_layout(self._counts)
        for key, lbl in (("OK", "OK"), ("WARNING", "warning"), ("CRITICAL", "critical"), ("OFFLINE", "offline")):
            r = QHBoxLayout()
            r.addWidget(_dot(VERDICT[key]))
            t = QLabel(f"{counts[key]} {lbl}")
            t.setStyleSheet(f"color:{PAL['text']}; font-size:13px;")
            r.addWidget(t)
            r.addStretch()
            self._counts.addLayout(r)

        post_by_id = {}
        for p in self._posture:
            issues = sum(1 for v in (p.get("flags") or {}).values() if v is True)
            post_by_id[p.get("id")] = {"issues": issues, "limited": bool(p.get("limited"))}

        _clear_grid(self._signals_grid)
        if self._posture:
            sigs = []
            for key, label in fs.SIGNAL_LABELS:
                n = sum(1 for p in self._posture if (p.get("flags") or {}).get(key) is True)
                sigs.append((label, n))
            sigs.append(("Disk usage critical (≥ 90%)", sum(1 for h in self._health if (h.get("disk") or 0) >= 90)))
            sigs.append(("Failed systemd units", sum(1 for h in self._health if (h.get("failed") or 0) > 0)))
            sigs.sort(key=lambda s: s[1], reverse=True)
            for idx, (label, n) in enumerate(sigs):
                self._signals_grid.addWidget(_signal_chip(label, n), idx // 2, idx % 2)

        groups = {}
        for h in self._health:
            env = h.get("environment") or "Unassigned"
            pe = post_by_id.get(h.get("id"), {})
            host = {**h, "issues": pe.get("issues"), "limited": pe.get("limited", False)}
            g = groups.setdefault(env, {"env": env, "hosts": [], "counts": dict(OK=0, WARNING=0, CRITICAL=0, OFFLINE=0),
                                        "disk": None, "mem": None, "failed": 0, "oom": 0, "degraded": 0,
                                        "problematic": 0, "limited": 0})
            g["hosts"].append(host)
            ver = (h.get("verdict") or "OK").upper()
            g["counts"][ver] = g["counts"].get(ver, 0) + 1
            if h.get("disk") is not None:
                g["disk"] = max(g["disk"] or 0, h["disk"])
            if h.get("mem") is not None:
                g["mem"] = max(g["mem"] or 0, h["mem"])
            g["failed"] += h.get("failed") or 0
            g["oom"] += h.get("oom") or 0
            if h.get("sysd") and h.get("sysd") not in ("running", "unknown"):
                g["degraded"] += 1
            if (host["issues"] or 0) > 0:
                g["problematic"] += 1
            if host["limited"]:
                g["limited"] += 1
        env_list = []
        for g in groups.values():
            c = g["counts"]
            g["verdict"] = "CRITICAL" if c["CRITICAL"] else "WARNING" if c["WARNING"] else "OK" if c["OK"] else "OFFLINE"
            g["hosts"].sort(key=lambda x: (_ORDER.get((x.get("verdict") or "OK").upper(), 9),
                                           -(x.get("issues") or 0), -(x.get("disk") or 0)))
            env_list.append(g)
        env_list.sort(key=lambda g: (_ORDER.get(g["verdict"], 9), -g["problematic"], g["env"]))

        _clear_layout(self._envs_lay)
        posture_loaded = bool(self._posture)
        if not env_list:
            empty = QLabel("No host data yet — click Refresh.")
            empty.setStyleSheet(f"color:{PAL['faint']}; padding:16px;")
            self._envs_lay.addWidget(empty)
        for g in env_list:
            self._envs_lay.addWidget(EnvCard(g, posture_loaded, self._open_host))
        self._envs_lay.addStretch()

    def _open_host(self, host_id, label):
        dlg = PostureDialog(host_id, label, self)
        self._dialogs.append(dlg)
        dlg.show()


def _clear_layout(layout):
    while layout.count():
        it = layout.takeAt(0)
        w = it.widget()
        if w:
            w.deleteLater()
        else:
            sub = it.layout()
            if sub:
                _clear_layout(sub)


def _clear_grid(grid):
    while grid.count():
        it = grid.takeAt(0)
        w = it.widget()
        if w:
            w.deleteLater()
