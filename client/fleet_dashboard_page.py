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
    QScrollArea, QFrame, QProgressBar, QDialog, QMenu, QMessageBox,
)
from PySide6.QtGui import QPainter, QPen, QColor
from PySide6.QtCore import Qt, QThread, Signal, QRectF, QTimer

import time

from client import _fleet_status as fs
from client import api
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
        page="#F3F5F8" if light else "#1E1E1E",
        bg="#FFFFFF" if light else "#232a36",
        border="#D0D7DE" if light else "#3a4250",
        text="#1F2328" if light else "#EAEAEA",
        faint="#656D76" if light else "#9aa5b1",
        track="#EAEEF2" if light else "#2a2f3a",
        row="#F6F8FA" if light else "#2a313d",
        row_hover="#EFF2F5" if light else "#313947",
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
    """A label + colored bar + percentage row (disk/mem). The label sits in a
    fixed gutter; the percentage is bold and takes the bar's threshold color
    (green/amber/red) so it reads as a status at a glance, not muddy gray."""
    w = QWidget()
    row = QHBoxLayout(w)
    row.setContentsMargins(0, 3, 0, 3)
    row.setSpacing(10)
    lab = QLabel(label.upper())
    lab.setStyleSheet(f"border:none; background:transparent; color:{PAL['faint']};"
                      f" font-size:12px; font-weight:bold; letter-spacing:0.5px;")
    lab.setFixedWidth(40)
    row.addWidget(lab)
    bar = QProgressBar()
    bar.setRange(0, 100)
    bar.setTextVisible(False)
    bar.setFixedHeight(10)
    v = 0 if pct is None else max(0, min(100, int(pct)))
    bar.setValue(v)
    color = "#7a7a7a" if pct is None else ("#e06c6c" if v >= 90 else "#e0a83a" if v >= 75 else "#4ec07a")
    bar.setStyleSheet(
        f"QProgressBar{{background:{PAL['track']};border:none;border-radius:5px;}}"
        f"QProgressBar::chunk{{background:{color};border-radius:5px;}}")
    row.addWidget(bar, 1)
    val = QLabel("—" if pct is None else f"{v}%")
    val.setStyleSheet(f"border:none; background:transparent; color:{color};"
                      f" font-size:14px; font-weight:bold;")
    val.setFixedWidth(46)
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
def _hosts_menu(widget, hosts, on_open, header):
    """Pop a menu of hosts under `widget`; each entry opens that host's posture."""
    m = QMenu(widget)
    h0 = m.addAction(header)
    h0.setEnabled(False)
    m.addSeparator()
    for h in hosts:
        act = m.addAction(h.get("host") or h.get("id"))
        act.triggered.connect(lambda _=False, hh=h: on_open(hh.get("id"), hh.get("host")))
    m.exec(widget.mapToGlobal(widget.rect().bottomLeft()))


def _signal_chip(label, hosts, on_open=None):
    """A compliance-signal chip. When it has affected hosts, it's clickable and
    drops a menu of those hosts, each opening its posture (parity with the web)."""
    count = len(hosts)
    color = "#e0a83a" if count > 0 else "#4ec07a"
    clickable = count > 0 and on_open is not None
    if clickable:
        f = QPushButton()
        f.setCursor(Qt.PointingHandCursor)
        f.setStyleSheet(
            f"QPushButton{{border:1px solid {PAL['border']}; border-radius:8px; text-align:left;"
            f" background:{PAL['bg']};}} QPushButton:hover{{background:{PAL['row_hover']};}}")
    else:
        f = _card_frame()
    lay = QHBoxLayout(f)
    lay.setContentsMargins(10, 8, 10, 8)
    lay.addWidget(_dot(color))
    name = QLabel(label)
    name.setStyleSheet(f"border:none; background:transparent; color:{PAL['text']}; font-size:12px;")
    lay.addWidget(name, 1)
    cnt = QLabel(str(count))
    cnt.setStyleSheet(f"border:none; background:transparent; font-weight:bold; color:{color};")
    lay.addWidget(cnt)
    if clickable:
        n = count
        f.clicked.connect(lambda: _hosts_menu(
            f, hosts, on_open, f"{n} host{'' if n == 1 else 's'} — open posture:"))
    return f


def _count_row(key, lbl, hosts, on_open=None):
    """A verdict tally row (e.g. '1 offline'). Clickable when there are hosts —
    drops a menu of them so you can see/open which ones (parity with the web)."""
    count = len(hosts)
    clickable = count > 0 and on_open is not None
    if clickable:
        w = QPushButton()
        w.setCursor(Qt.PointingHandCursor)
        w.setStyleSheet("QPushButton{border:none; background:transparent; text-align:left; padding:1px 0;}")
    else:
        w = QWidget()
        w.setStyleSheet("background:transparent;")
    r = QHBoxLayout(w)
    r.setContentsMargins(0, 0, 0, 0)
    r.addWidget(_dot(VERDICT.get(key, "#7a7a7a")))
    t = QLabel(f"{count} {lbl}")
    t.setStyleSheet(f"border:none; background:transparent; color:{PAL['text']}; font-size:13px;")
    r.addWidget(t)
    r.addStretch()
    if clickable:
        w.clicked.connect(lambda: _hosts_menu(w, hosts, on_open, f"{count} {lbl} — open posture:"))
    return w


# ---------------------------------------------------------------------------
# Per-environment card (collapsible)
# ---------------------------------------------------------------------------
def _restart_unit_on_host(host_id, unit):
    """Resolve the host entry and restart one systemd unit on it AS THE OPERATOR
    (so it's attributed/logged and respects RBAC — unlike the dashboard's
    tokenless read sweep). Polls an agent task to completion. Returns
    {'ok': bool, 'detail': str}. Runs off the GUI thread (see _Worker)."""
    import time
    entry = None
    for e in api.list_merged_hosts():
        if e.get("id") == host_id:
            entry = e
            break
    if entry is None:
        return {"ok": False, "detail": "host not found"}
    out = api.run_on_entry(entry, api.cmd_service_restart(unit),
                           description=f"Restart unit {unit}")
    if out.get("error"):
        return {"ok": False, "detail": out["error"]}
    if out.get("sync"):
        detail = (out.get("stdout") or "").strip() or (out.get("stderr") or "").strip()
        return {"ok": out.get("code") == 0, "detail": detail}
    tid = out.get("task_id")
    if tid is None:
        return {"ok": False, "detail": "failed to queue task"}
    deadline = time.time() + 30
    while time.time() < deadline:
        r = api.poll_entry_result(entry, tid)
        if r is not None:
            detail = (r.get("stdout") or "").strip() or (r.get("stderr") or "").strip()
            return {"ok": r.get("code") == 0, "detail": detail}
        time.sleep(1.0)
    return {"ok": False, "detail": "timed out waiting for host to report back"}


class EnvCard(QFrame):
    def __init__(self, group, posture_loaded, on_open_host, on_restart_unit=None):
        super().__init__()
        self.setStyleSheet(f"QFrame{{border:1px solid {PAL['border']}; border-radius:8px; background:{PAL['bg']};}}")
        self._on_open_host = on_open_host
        self._on_restart_unit = on_restart_unit
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
        units = h.get("units") or []
        if hid and units and self._on_restart_unit is not None:
            btn.setContextMenuPolicy(Qt.CustomContextMenu)
            btn.customContextMenuRequested.connect(
                lambda _pos, b=btn, hh=h: self._unit_menu(b, hh))
            btn.setToolTip("Left-click for posture detail · right-click to restart a failed unit")
        return btn

    def _unit_menu(self, btn, h):
        """Right-click menu on a host with failed units: one 'Restart <unit>'
        entry per crashed unit, dispatched as the operator."""
        units = h.get("units") or []
        if not units or self._on_restart_unit is None:
            return
        menu = QMenu(btn)
        hdr = menu.addAction("Restart a failed unit:")
        hdr.setEnabled(False)
        menu.addSeparator()
        hid = h.get("id")
        label = h.get("host") or hid
        for u in units:
            act = menu.addAction(f"Restart   {u}")
            act.triggered.connect(
                lambda _=False, unit=u: self._on_restart_unit(hid, label, unit))
        menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))


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


def name_for(key):
    return key.replace("_", " ").title()


def _to_int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _pct_status(v):
    n = _to_int(v)
    if n is None:
        return None
    return "bad" if n >= 90 else "warn" if n >= 75 else "good"


# Pass/warn/fail per posture key — ported from the web console's HostDetail
# evalStatus so the desktop colors the same signals. "good"/"warn"/"bad"/None.
def _metric_status(full_key, value):
    s = ("" if value is None else str(value)).strip().lower()
    if full_key == "reboot.required":
        return "bad" if s == "1" else "good"
    if full_key == "fw.active":
        return "good" if s == "1" else "bad"
    if full_key == "mac.selinux":
        return "good" if s == "enforcing" else "warn" if s in ("permissive", "disabled") else None
    if full_key == "mac.apparmor":
        return "good" if s == "enabled" else "warn" if s == "disabled" else None
    if full_key == "sec.auditd":
        return "good" if s == "active" else None
    if full_key == "sec.fail2ban":
        return "good" if s == "active" else None if s == "absent" else "warn"
    if full_key == "sec.fips":
        return "good" if s == "1" else None
    if full_key == "sec.usb_storage":
        return "good" if s == "blocked" else None
    if full_key == "ssh.permit_root_login":
        return "bad" if s == "yes" else "good" if s == "no" else ("warn" if s else None)
    if full_key == "ssh.password_auth":
        return "warn" if s == "yes" else "good" if s == "no" else None
    if full_key in ("ssh.weak_ciphers", "ssh.weak_macs", "ssh.weak_kex"):
        return "bad" if s else "good"
    if full_key == "users.uid0_count":
        n = _to_int(s); return "bad" if (n or 0) > 1 else "good"
    if full_key == "users.empty_pw_count":
        n = _to_int(s); return "bad" if (n or 0) > 0 else "good"
    if full_key in ("users.dup_uid", "users.dup_gid"):
        return "bad" if s else "good"
    if full_key == "users.pw_complexity":
        return "good" if s == "configured" else "warn"
    if full_key in ("fs.disk_pct", "fs.inode_pct", "hw.mem_pct", "hw.swap_pct"):
        return _pct_status(s)
    if full_key == "time.synced":
        return "good" if s in ("yes", "true", "1") else "bad" if s in ("no", "false", "0") else None
    if full_key == "cert.expiring_30d":
        n = _to_int(s); return "warn" if (n or 0) > 0 else "good"
    if full_key == "cert.nearest_days":
        n = _to_int(s); return ("warn" if n < 30 else "good") if n is not None else None
    if full_key in ("svc.failed_count", "svc.zombies", "perf.oom", "cont.docker_privileged"):
        n = _to_int(s); return "warn" if (n or 0) > 0 else "good"
    if full_key == "hw.smart":
        return "good" if s == "ok" else "bad" if s == "failing" else None
    return None


def _fmt_value(full_key, value):
    if value in ("", None):
        return "—"
    if full_key in ("fw.active", "reboot.required", "net.ip_forward",
                    "log.remote_forward", "net.ipv6_disabled"):
        return "yes" if str(value) == "1" else "no" if str(value) == "0" else str(value)
    if full_key in ("fs.disk_pct", "fs.inode_pct", "hw.mem_pct", "hw.swap_pct"):
        return f"{value}%"
    return str(value)


_STATUS_COLOR = {"bad": "#e06c6c", "warn": "#e0a83a", "good": "#4ec07a"}


# Posture finding → remediation, mirroring the web console's HostDetail. Either
# reboot right here, or open the desktop tool that fixes it. Only surfaced on a
# warn/bad row.
_POSTURE_ACTIONS = {
    "reboot.required": {"run": "reboot"},
    "cert.expiring_30d": {"tool": "Certificate Management"},
    "cert.nearest_days": {"tool": "Certificate Management"},
    "fw.active": {"tool": "Firewall Administration"},
    "mac.selinux": {"tool": "Security Administration"},
    "mac.apparmor": {"tool": "Security Administration"},
    "sec.auditd": {"tool": "Security Administration"},
    "sec.fail2ban": {"tool": "Security Administration"},
    "ssh.permit_root_login": {"tool": "Security Administration"},
    "ssh.password_auth": {"tool": "Security Administration"},
    "ssh.weak_ciphers": {"tool": "Security Administration"},
    "ssh.weak_macs": {"tool": "Security Administration"},
    "ssh.weak_kex": {"tool": "Security Administration"},
    "time.synced": {"tool": "Time Synchronization"},
    "svc.failed_count": {"tool": "Quick System Actions"},
    "fs.disk_pct": {"tool": "Storage Administration"},
    "fs.inode_pct": {"tool": "Storage Administration"},
    "users.empty_pw_count": {"tool": "User & Group Administration"},
    "users.uid0_count": {"tool": "User & Group Administration"},
    "users.pw_complexity": {"tool": "Environmental Policies"},
}

_TOOL_PAGES = {
    "Security Administration": ("client.security_administration_page", "SecurityAdministrationPage"),
    "Firewall Administration": ("client.firewall_administration_page", "FirewallAdministrationPage"),
    "Storage Administration": ("client.storage_administration_page", "StorageAdministrationPage"),
    "Time Synchronization": ("client.time_synchronization_page", "TimeSynchronizationPage"),
    "Certificate Management": ("client.certificate_management_page", "CertificateManagementPage"),
    "Quick System Actions": ("client.quick_system_actions_page", "QuickSystemActionsPage"),
    "User & Group Administration": ("client.user_group_administration_page", "UserGroupAdministrationPage"),
    "Environmental Policies": ("client.environmental_policies_page", "EnvironmentalPoliciesPage"),
}
_OPEN_TOOL_WINDOWS = []  # keep opened tool windows alive (not GC'd)


def _open_tool_window(name):
    """Open a desktop tool page as its own window — mirrors how the app launches
    tools. Lazy import avoids import cycles."""
    import importlib
    spec = _TOOL_PAGES.get(name)
    if not spec:
        return
    mod = importlib.import_module(spec[0])
    win = getattr(mod, spec[1])()
    _OPEN_TOOL_WINDOWS.append(win)
    win.show()
    win.raise_()


def _reboot_host(host_id):
    """Reboot one host as the operator (attributed/logged). Returns
    {'ok': bool, 'detail': str}. Runs off the GUI thread (see _Worker)."""
    entry = None
    for e in api.list_merged_hosts():
        if e.get("id") == host_id:
            entry = e
            break
    if entry is None:
        return {"ok": False, "detail": "host not found"}
    out = api.run_on_entry(entry, api.cmd_reboot_host(), description="Reboot host")
    if out.get("error"):
        return {"ok": False, "detail": out["error"]}
    return {"ok": True, "detail": (out.get("stdout") or "").strip()}


class PostureDialog(QDialog):
    def __init__(self, host_id, label, parent=None):
        super().__init__(parent)
        _refresh_pal()
        self._host_id = host_id
        self.setWindowTitle(f"Posture — {label}")
        self.resize(900, 720)
        # Force the dialog + scroll backgrounds to the theme page color so dark
        # mode doesn't fall back to a white viewport.
        self.setStyleSheet(f"QDialog{{background:{PAL['page']};}}")
        v = QVBoxLayout(self)
        topr = QHBoxLayout()
        self._title = QLabel(f"<b>{label}</b>")
        self._title.setStyleSheet(f"color:{PAL['text']};")
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
        self._scroll.setStyleSheet(f"QScrollArea{{background:{PAL['page']}; border:none;}}")
        self._body = QWidget()
        self._body.setStyleSheet(f"background:{PAL['page']};")
        self._grid = QGridLayout(self._body)
        self._grid.setSpacing(10)
        self._scroll.setWidget(self._body)
        v.addWidget(self._scroll, 1)
        self._worker = None
        self._last_posture = None
        self._reboot_timer = None
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
        self._last_posture = posture
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
                    rows.append((f"{cat}.{k}", name_for(k), val))
            if not rows:
                continue
            self._grid.addWidget(self._section(title, rows), i // col_count, i % col_count)
            i += 1

    def _section(self, title, rows):
        f = _card_frame()
        lay = QVBoxLayout(f)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(3)
        t = QLabel(title.upper())
        t.setStyleSheet(f"border:none; background:transparent; color:{PAL['faint']}; font-size:11px; font-weight:bold;")
        lay.addWidget(t)
        for full_key, name, val in rows:
            status = _metric_status(full_key, val)
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)
            ln = QLabel(name)
            ln.setStyleSheet(f"border:none; background:transparent; color:{PAL['faint']}; font-size:12px;")
            row.addWidget(ln)
            row.addStretch()
            # A colored status dot for any evaluated metric (green/amber/red).
            if status in _STATUS_COLOR:
                row.addWidget(_dot(_STATUS_COLOR[status], 8))
            lv = QLabel(_fmt_value(full_key, val))
            color = _STATUS_COLOR.get(status, PAL["text"])
            bold = "font-weight:bold;" if status in ("bad", "warn") else ""
            lv.setStyleSheet(f"border:none; background:transparent; color:{color}; font-size:12px; {bold}")
            lv.setWordWrap(True)
            lv.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            row.addWidget(lv)
            lay.addLayout(row)
            # Remediation on a warn/bad row: reboot here, or open the fixing tool.
            action = _POSTURE_ACTIONS.get(full_key) if status in ("bad", "warn") else None
            if action:
                arow = QHBoxLayout()
                arow.setContentsMargins(0, 0, 0, 2)
                arow.addStretch()
                if action.get("run") == "reboot":
                    btn = QPushButton("Reboot host")
                    btn.clicked.connect(self._reboot)
                else:
                    tool = action["tool"]
                    btn = QPushButton(f"Fix in {tool} →")
                    btn.clicked.connect(lambda _=False, t=tool: _open_tool_window(t))
                btn.setStyleSheet("font-size:11px; padding:2px 8px;")
                arow.addWidget(btn)
                lay.addLayout(arow)
        return f

    # ---- reboot from the posture dialog, with auto-refresh on the way back ----
    def _reboot(self):
        if QMessageBox.question(
                self, "Reboot host", "Reboot this host now?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self._reboot_base = ((self._last_posture or {}).get("os") or {}).get("boot_epoch")
        self._status.setVisible(True)
        self._status.setStyleSheet(f"color:{PAL['faint']};")
        self._status.setText("Reboot requested…")
        self._reboot_worker = _Worker(lambda: _reboot_host(self._host_id))
        self._reboot_worker.done.connect(self._after_reboot_request)
        self._reboot_worker.fail.connect(
            lambda m: self._status.setText(f"Reboot failed: {m}"))
        self._reboot_worker.start()

    def _after_reboot_request(self, res):
        if not res.get("ok"):
            self._status.setStyleSheet("color:#e06c6c;")
            self._status.setText(f"Reboot failed: {res.get('detail') or 'see host'}")
            return
        self._status.setText("Reboot requested — waiting for the host to check back in; "
                             "this refreshes automatically once it's back.")
        self._reboot_deadline = time.time() + 300
        if getattr(self, "_reboot_timer", None):
            self._reboot_timer.stop()
        self._reboot_timer = QTimer(self)
        self._reboot_timer.setInterval(10000)
        self._reboot_timer.timeout.connect(self._poll_reboot)
        self._reboot_timer.start()

    def _poll_reboot(self):
        if time.time() > getattr(self, "_reboot_deadline", 0):
            self._reboot_timer.stop()
            self._status.setText("Reboot requested — the host hasn't reported back yet. "
                                 "Use Refresh posture once it's up.")
            return
        w = _Worker(lambda: fs.gather_host_posture(self._host_id))
        self._reboot_poll_worker = w
        w.done.connect(self._check_reboot_result)
        w.start()

    def _check_reboot_result(self, data):
        posture = (data or {}).get("posture")
        if not posture:
            return
        os_ = posture.get("os") or {}
        boot = os_.get("boot_epoch")
        up = _to_int(os_.get("uptime_s"))
        reboot_req = (posture.get("reboot") or {}).get("required")
        base = getattr(self, "_reboot_base", None)
        if (base and boot and boot != base) or reboot_req == "0" or (up is not None and up < 600):
            if getattr(self, "_reboot_timer", None):
                self._reboot_timer.stop()
            self._render(data)


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
        by_verdict = {}
        for h in self._health:
            by_verdict.setdefault((h.get("verdict") or "OK").upper(), []).append(
                {"id": h.get("id"), "host": h.get("host")})
        for key, lbl in (("OK", "OK"), ("WARNING", "warning"), ("CRITICAL", "critical"), ("OFFLINE", "offline")):
            self._counts.addWidget(_count_row(key, lbl, by_verdict.get(key, []), self._open_host))

        post_by_id = {}
        for p in self._posture:
            issues = sum(1 for v in (p.get("flags") or {}).values() if v is True)
            post_by_id[p.get("id")] = {"issues": issues, "limited": bool(p.get("limited"))}

        _clear_grid(self._signals_grid)
        if self._posture:
            id_to_host = {h.get("id"): h.get("host") for h in self._health}

            def hosts_for_flag(key):
                out = []
                for p in self._posture:
                    if (p.get("flags") or {}).get(key) is True:
                        hid = p.get("id")
                        out.append({"id": hid, "host": id_to_host.get(hid) or p.get("host") or hid})
                return out

            sigs = [(label, hosts_for_flag(key)) for key, label in fs.SIGNAL_LABELS]
            sigs.append(("Disk usage critical (≥ 90%)",
                         [{"id": h.get("id"), "host": h.get("host")}
                          for h in self._health if (h.get("disk") or 0) >= 90]))
            sigs.append(("Failed systemd units",
                         [{"id": h.get("id"), "host": h.get("host")}
                          for h in self._health if (h.get("failed") or 0) > 0]))
            sigs.sort(key=lambda s: len(s[1]), reverse=True)
            for idx, (label, hosts) in enumerate(sigs):
                self._signals_grid.addWidget(_signal_chip(label, hosts, self._open_host), idx // 2, idx % 2)

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
            self._envs_lay.addWidget(EnvCard(g, posture_loaded, self._open_host, self._restart_unit))
        self._envs_lay.addStretch()

    def _restart_unit(self, host_id, label, unit):
        """Restart one failed unit on one host, from the dashboard right-click.
        Confirms, dispatches off the GUI thread as the operator, then reports.
        Kept on the (long-lived) page so an auto-refresh rebuilding the cards
        can't destroy the worker or the dialog parent mid-run."""
        if QMessageBox.question(
                self, "Restart unit", f"Restart '{unit}' on {label}?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        if not hasattr(self, "_restart_workers"):
            self._restart_workers = []
        w = _Worker(lambda i=host_id, u=unit: _restart_unit_on_host(i, u))
        self._restart_workers.append(w)
        w.done.connect(lambda res, u=unit, l=label: self._restart_done(l, u, res))
        w.fail.connect(lambda err, u=unit, l=label: QMessageBox.warning(
            self, "Restart failed", f"{u} on {l}:\n\n{err}"))
        w.finished.connect(lambda w=w: self._restart_workers.remove(w)
                           if w in self._restart_workers else None)
        w.start()

    def _restart_done(self, label, unit, res):
        detail = (res.get("detail") or "").strip()
        if res.get("ok"):
            QMessageBox.information(
                self, "Unit restarted",
                f"Restarted {unit} on {label}." + (f"\n\n{detail}" if detail else ""))
        else:
            QMessageBox.warning(
                self, "Restart reported a problem",
                f"{unit} on {label} did not restart cleanly." + (f"\n\n{detail}" if detail else ""))

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
