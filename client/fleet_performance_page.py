"""Fleet Performance — desktop GUI parity with the web console's Performance
view. Environment-first time-series charts (CPU/memory/swap/disk/network/IO/
load/processes) drawn with QPainter (no chart-library dependency); click an
environment to drill into its hosts, then a host for all its metrics plus a
live current-detail snapshot (per-core CPU, memory breakdown, per-mount disk,
per-interface network, top processes).

Series data: client.api.get_metrics_timeseries(window); the per-host snapshot:
client.api.get_host_snapshot(id). Fetches run on a background QThread.
Reuses the Fleet dashboard's theme palette, worker, and small widgets.
"""
import math

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QScrollArea, QProgressBar, QDialog,
)
from PySide6.QtGui import QPainter, QPen, QColor, QPointF, QBrush
from PySide6.QtCore import Qt, QRectF

from client import api, theme
from client.branding import make_page_header
from client.fleet_dashboard_page import PAL, _refresh_pal, _Worker, _dot, _card_frame

ENV_COLORS = ["#5b9bd5", "#e0a83a", "#4ec07a", "#b07ad0", "#e06c6c", "#46c5c5", "#d98c5f", "#8a8af0"]
HOST_COLORS = ["#6fb1e0", "#f2c14e", "#67d39b", "#c98fe0", "#ef8a8a", "#5fd3d3", "#eaa06f", "#a0a0f7", "#bdd35a", "#e08fb8"]
WINDOWS = [("1h", 3600), ("6h", 21600), ("24h", 86400)]


def _cpu(s):
    c = s.get("cpu")
    if c is not None:
        return c
    l1, cores = s.get("load1"), s.get("cores")
    return (l1 / cores * 100) if (l1 is not None and cores) else None


# (key, label, kind, valueOf) — kind drives the y-axis formatting.
METRICS = [
    ("cpu", "CPU", "pct", _cpu),
    ("mem", "Memory", "pct", lambda s: s.get("mem")),
    ("swap", "Swap", "pct", lambda s: s.get("swap")),
    ("disk", "Disk usage", "pct", lambda s: s.get("disk")),
    ("net_rx", "Network in", "bytes", lambda s: s.get("net_rx")),
    ("net_tx", "Network out", "bytes", lambda s: s.get("net_tx")),
    ("io_r", "Disk read", "bytes", lambda s: s.get("io_r")),
    ("io_w", "Disk write", "bytes", lambda s: s.get("io_w")),
    ("load1", "Load (1m)", "num", lambda s: s.get("load1")),
    ("procs", "Processes", "num", lambda s: s.get("procs")),
]


def _fmt_bytes(v):
    if v is None or not _finite(v):
        return "—"
    u = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    n = float(v)
    while n >= 1024 and i < len(u) - 1:
        n /= 1024
        i += 1
    return f"{round(n)} {u[i]}" if (n >= 100 or i == 0) else f"{n:.1f} {u[i]}"


def _finite(v):
    try:
        return v == v and v not in (float("inf"), float("-inf"))
    except Exception:
        return False


def _fmt_metric(kind, v, rate=False):
    if v is None or not _finite(v):
        return "—"
    if kind == "pct":
        return f"{round(v)}%"
    if kind == "bytes":
        return _fmt_bytes(v) + ("/s" if rate else "")
    return f"{round(v * 10) / 10}"


def _nice_ceil(v):
    if not v or v <= 0:
        return 1
    p = 10 ** math.floor(math.log10(v))
    n = v / p
    m = 1 if n <= 1 else 2 if n <= 2 else 5 if n <= 5 else 10
    return m * p


def _fmt_axis(kind, v):
    if kind == "pct":
        return f"{round(v)}%"
    if kind == "bytes":
        return _fmt_bytes(v)
    return str(round(v * 10) / 10)


def _fmt_clock(tsec):
    import datetime
    return datetime.datetime.fromtimestamp(tsec).strftime("%H:%M")


def _bucket_average(samples, value_of, t0, t1, buckets=80):
    span = max(1, t1 - t0)
    w = span / buckets
    total = [0.0] * buckets
    cnt = [0] * buckets
    for s in samples:
        v = value_of(s)
        if v is None or not _finite(v):
            continue
        i = int((s["t"] - t0) / w)
        i = 0 if i < 0 else buckets - 1 if i >= buckets else i
        total[i] += v
        cnt[i] += 1
    return [(t0 + (i + 0.5) * w, total[i] / cnt[i]) for i in range(buckets) if cnt[i] > 0]


# ---------------------------------------------------------------------------
# Inline line chart (QPainter)
# ---------------------------------------------------------------------------
class LineChart(QWidget):
    """Inline multi-line chart. Interactive: hover shows a crosshair + per-series
    values, and dragging horizontally selects a time range to zoom into (the
    owner wires `on_zoom`)."""

    def __init__(self, kind):
        super().__init__()
        self._kind = kind
        self._series = []
        self._t0 = 0
        self._t1 = 1
        self._hover_x = None
        self._sel = None          # (x0, x1) in widget px during a drag
        self.on_zoom = None       # callback(t0, t1) set by the page
        self.setMinimumHeight(190)
        self.setMouseTracking(True)

    def set_data(self, series, t0, t1):
        self._series, self._t0, self._t1 = series, t0, t1
        self.update()

    _PADL, _PADR, _PADT, _PADB = 56, 14, 10, 24

    def _plot(self):
        W, H = self.width(), self.height()
        return W, H, W - self._PADL - self._PADR, H - self._PADT - self._PADB

    def _ymax(self):
        mx = 0.0
        for s in self._series:
            for _t, v in s["points"]:
                if v > mx:
                    mx = v
        if self._kind == "pct":
            return max(100, math.ceil(mx / 25) * 25)
        if self._kind == "bytes":
            return max(1024, _nice_ceil(mx or 1))
        return _nice_ceil(mx or 1)

    def _x_to_t(self, x):
        _W, _H, plotW, _plotH = self._plot()
        if plotW <= 0:
            return None
        frac = max(0.0, min(1.0, (x - self._PADL) / plotW))
        return self._t0 + frac * (self._t1 - self._t0)

    # ---- mouse: hover crosshair + drag-to-zoom ----
    @staticmethod
    def _ex(e):
        return e.position().x() if hasattr(e, "position") else e.x()

    def mouseMoveEvent(self, e):
        self._hover_x = self._ex(e)
        if self._sel is not None:
            self._sel = (self._sel[0], self._hover_x)
        self.update()

    def leaveEvent(self, _e):
        self._hover_x = None
        self._sel = None
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            x = self._ex(e)
            self._sel = (x, x)

    def mouseReleaseEvent(self, e):
        if self._sel is not None:
            a, b = self._sel
            self._sel = None
            if abs(b - a) > 6 and self.on_zoom:
                ta, tb = self._x_to_t(min(a, b)), self._x_to_t(max(a, b))
                if ta is not None and tb is not None and tb - ta > 1:
                    self.on_zoom(ta, tb)
            self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        W, H, plotW, plotH = self._plot()
        padL, padR, padT, padB = self._PADL, self._PADR, self._PADT, self._PADB
        if plotW <= 0 or plotH <= 0:
            p.end()
            return
        ymax = self._ymax()

        fnt = p.font()
        fnt.setPointSize(8)
        p.setFont(fnt)
        for f in (0, 0.25, 0.5, 0.75, 1):
            yy = padT + (1 - f) * plotH
            p.setPen(QPen(QColor(PAL["border"]), 1))
            p.drawLine(int(padL), int(yy), int(W - padR), int(yy))
            p.setPen(QColor(PAL["faint"]))
            p.drawText(QRectF(0, yy - 8, padL - 6, 16), Qt.AlignRight | Qt.AlignVCenter,
                       _fmt_axis(self._kind, ymax * f))
        span = max(1, self._t1 - self._t0)
        for i, frac in enumerate((0.0, 0.5, 1.0)):
            xx = padL + frac * plotW
            align = Qt.AlignLeft if i == 0 else Qt.AlignRight if i == 2 else Qt.AlignHCenter
            p.setPen(QColor(PAL["faint"]))
            p.drawText(QRectF(xx - 42, H - 18, 84, 16), align | Qt.AlignVCenter,
                       _fmt_clock(self._t0 + span * frac))

        def fx(t):
            return padL + ((t - self._t0) / span) * plotW

        def fy(v):
            return padT + (1 - max(0, min(v, ymax)) / ymax) * plotH

        # Clip series to the plot rect so a zoomed range doesn't draw over axes.
        p.save()
        p.setClipRect(QRectF(padL, padT, plotW, plotH))
        any_pts = False
        for s in self._series:
            pts = s["points"]
            if not pts:
                continue
            any_pts = True
            pen = QPen(QColor(s["color"]))
            pen.setWidth(2)
            pen.setJoinStyle(Qt.RoundJoin)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            qp = [QPointF(fx(t), fy(v)) for t, v in pts]
            for a, b in zip(qp, qp[1:]):
                p.drawLine(a, b)
        p.restore()

        # Drag selection band.
        if self._sel is not None:
            a = max(padL, min(W - padR, self._sel[0]))
            b = max(padL, min(W - padR, self._sel[1]))
            p.setPen(QPen(QColor("#4ea1ff"), 1))
            p.setBrush(QBrush(QColor(78, 161, 255, 40)))
            p.drawRect(QRectF(min(a, b), padT, abs(b - a), plotH))

        # Hover crosshair + per-series markers + tooltip.
        if self._hover_x is not None and any_pts and self._sel is None \
                and padL <= self._hover_x <= W - padR:
            hx = self._hover_x
            ht = self._x_to_t(hx)
            pen = QPen(QColor(PAL["faint"]), 1)
            pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.drawLine(int(hx), int(padT), int(hx), int(padT + plotH))
            entries = []
            for s in self._series:
                if not s["points"]:
                    continue
                best = min(s["points"], key=lambda pt: abs(pt[0] - ht))
                p.setBrush(QColor(s["color"]))
                p.setPen(QPen(QColor(PAL["bg"]), 1))
                p.drawEllipse(QPointF(fx(best[0]), fy(best[1])), 3.5, 3.5)
                entries.append((s.get("label", ""), s["color"], best[1]))
            self._draw_tooltip(p, hx, padT, W, padL, padR, ht, entries)
        if not any_pts:
            p.setPen(QColor(PAL["faint"]))
            p.drawText(self.rect(), Qt.AlignCenter, "no samples in this window")
        p.end()

    def _draw_tooltip(self, p, hx, padT, W, padL, padR, ht, entries):
        fm = p.fontMetrics()
        lines = [_fmt_clock(ht)] + [
            f"{lbl}: {_fmt_metric(self._kind, v, self._kind == 'bytes')}" for lbl, _c, v in entries]
        tw = max(fm.horizontalAdvance(t) for t in lines) + 18
        th = len(lines) * (fm.height() + 2) + 8
        tx = hx + 12 if hx < (padL + (W - padL - padR) / 2) else hx - 12 - tw
        tx = max(padL, min(W - padR - tw, tx))
        ty = padT + 6
        p.setPen(QPen(QColor(PAL["border"]), 1))
        p.setBrush(QColor(PAL["bg"]))
        p.drawRoundedRect(QRectF(tx, ty, tw, th), 6, 6)
        yy = ty + 6 + fm.ascent()
        p.setPen(QColor(PAL["faint"]))
        p.drawText(QPointF(tx + 9, yy), lines[0])
        yy += fm.height() + 2
        for lbl, color, v in entries:
            p.setPen(QColor(color))
            p.drawText(QPointF(tx + 9, yy), f"{lbl}: {_fmt_metric(self._kind, v, self._kind == 'bytes')}")
            yy += fm.height() + 2


def _chart_card(metric_label, now_value=None, on_expand=None):
    """A boxed chart cell: title (+ optional 'now' value) over a LineChart. The
    title is a button that enlarges the chart into its own window for analysis."""
    card = _card_frame()
    lay = QVBoxLayout(card)
    lay.setContentsMargins(12, 8, 12, 8)
    lay.setSpacing(4)
    head = QHBoxLayout()
    if on_expand:
        t = QPushButton(f"{metric_label}  ⤢")
        t.setCursor(Qt.PointingHandCursor)
        t.setStyleSheet("QPushButton{border:none; background:transparent; text-align:left;"
                        f" color:{PAL['text']}; font-weight:bold; font-size:13px; padding:0;}}")
        t.setToolTip("Expand for analysis")
        t.clicked.connect(on_expand)
    else:
        t = QLabel(metric_label)
        t.setStyleSheet(f"border:none; background:transparent; color:{PAL['text']}; font-weight:bold; font-size:13px;")
    head.addWidget(t)
    head.addStretch()
    if now_value is not None:
        nv = QLabel(f"now {now_value}")
        nv.setStyleSheet(f"border:none; background:transparent; color:{PAL['faint']}; font-size:12px;")
        head.addWidget(nv)
    lay.addLayout(head)
    return card, lay


class _FocusChartDialog(QDialog):
    """A single metric blown up into its own window for analysis: a large chart
    with the same hover tooltip + drag-to-zoom (zoom is dialog-local)."""

    def __init__(self, title, series, t0, t1, kind, parent=None):
        super().__init__(parent)
        _refresh_pal()
        self.setWindowTitle(title)
        self.resize(1000, 560)
        self.setStyleSheet(f"QDialog{{background:{PAL['page']};}}")
        self._series, self._base = series, (t0, t1)
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        lab = QLabel(f"<b>{title}</b>")
        lab.setStyleSheet(f"color:{PAL['text']};")
        top.addWidget(lab)
        top.addStretch()
        self._reset = QPushButton("Reset zoom")
        self._reset.setEnabled(False)
        self._reset.clicked.connect(self._reset_zoom)
        top.addWidget(self._reset)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        top.addWidget(close)
        v.addLayout(top)
        hint = QLabel("Hover for values · drag across the chart to zoom")
        hint.setStyleSheet(f"color:{PAL['faint']}; font-size:11px;")
        v.addWidget(hint)
        self._chart = LineChart(kind)
        self._chart.setMinimumHeight(430)
        self._chart.set_data(series, t0, t1)
        self._chart.on_zoom = self._zoom
        v.addWidget(self._chart, 1)

    def _zoom(self, a, b):
        self._chart.set_data(self._series, a, b)
        self._reset.setEnabled(True)

    def _reset_zoom(self):
        self._chart.set_data(self._series, *self._base)
        self._reset.setEnabled(False)


def _stat(label, value):
    card = _card_frame()
    lay = QVBoxLayout(card)
    lay.setContentsMargins(10, 6, 10, 6)
    lay.setSpacing(0)
    lab = QLabel(label)
    lab.setStyleSheet(f"border:none; background:transparent; color:{PAL['faint']}; font-size:11px;")
    val = QLabel(value)
    val.setStyleSheet(f"border:none; background:transparent; color:{PAL['text']}; font-size:17px; font-weight:bold;")
    lay.addWidget(lab)
    lay.addWidget(val)
    return card


def _legend_chip(label, color, on_click):
    b = QPushButton()
    b.setCursor(Qt.PointingHandCursor)
    b.setStyleSheet(
        f"QPushButton{{border:1px solid {PAL['border']}; border-radius:6px; background:{PAL['bg']};"
        f" color:{PAL['text']}; padding:4px 10px; text-align:left;}}"
        f"QPushButton:hover{{background:{PAL['row_hover']};}}")
    row = QHBoxLayout(b)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(6)
    row.addWidget(_dot(color, 10))
    lab = QLabel(label)
    lab.setStyleSheet("border:none; background:transparent;")
    row.addWidget(lab)
    if on_click:
        b.clicked.connect(on_click)
    return b


class FleetPerformancePage(QWidget):
    def __init__(self):
        super().__init__()
        _refresh_pal()
        self.setWindowTitle("Fleet Performance")
        self.resize(1180, 880)
        self._window = 3600
        self._data = {"hosts": [], "now": 0}
        self._sel_env = None
        self._sel_host = None
        self._snapshot = None
        self._worker = None
        self._snap_worker = None
        self._zoom = None            # (t0, t1) drag-zoom shared by all charts
        self._focus_dialogs = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(make_page_header("Fleet Performance"))

        body = QVBoxLayout()
        body.setContentsMargins(20, 12, 20, 16)
        body.setSpacing(10)
        outer.addLayout(body)

        # Controls row: breadcrumb + window selector + refresh.
        ctl = QHBoxLayout()
        self._crumb = QHBoxLayout()
        ctl.addLayout(self._crumb)
        ctl.addStretch()
        for lbl, secs in WINDOWS:
            b = QPushButton(lbl)
            b.setCheckable(True)
            b.setFixedWidth(48)
            b.clicked.connect(lambda _=False, s=secs: self._set_window(s))
            setattr(self, f"_win_{secs}", b)
            ctl.addWidget(b)
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self.refresh)
        ctl.addWidget(self._refresh_btn)
        body.addLayout(ctl)

        self._status = QLabel("Loading metrics…")
        self._status.setStyleSheet(f"color:{PAL['faint']};")
        body.addWidget(self._status)

        self._legend = QHBoxLayout()
        body.addLayout(self._legend)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet(f"QScrollArea{{background:{PAL['page']}; border:none;}}")
        self._content = QWidget()
        self._content.setStyleSheet(f"background:{PAL['page']};")
        self._content_lay = QVBoxLayout(self._content)
        self._content_lay.setSpacing(12)
        self._scroll.setWidget(self._content)
        body.addWidget(self._scroll, 1)

        self._sync_window_buttons()
        theme.add_theme_listener(self._on_theme)
        self.refresh()

    # ---- theme ----
    def _on_theme(self):
        _refresh_pal()
        self._status.setStyleSheet(f"color:{PAL['faint']};")
        self._scroll.setStyleSheet(f"QScrollArea{{background:{PAL['page']}; border:none;}}")
        self._content.setStyleSheet(f"background:{PAL['page']};")
        self._rebuild()

    # ---- data ----
    def _set_window(self, secs):
        self._window = secs
        self._zoom = None
        self._sync_window_buttons()
        self.refresh()

    def _sync_window_buttons(self):
        for _lbl, secs in WINDOWS:
            getattr(self, f"_win_{secs}").setChecked(secs == self._window)

    def refresh(self):
        self._status.setText("Loading metrics…")
        self._status.setVisible(True)
        self._refresh_btn.setEnabled(False)
        self._worker = _Worker(lambda: api.get_metrics_timeseries(self._window))
        self._worker.done.connect(self._on_data)
        self._worker.fail.connect(lambda m: (self._status.setText(f"Error: {m}"), self._refresh_btn.setEnabled(True)))
        self._worker.start()

    def _on_data(self, data):
        self._data = data or {"hosts": [], "now": 0}
        self._refresh_btn.setEnabled(True)
        self._status.setVisible(False)
        self._rebuild()

    # ---- helpers ----
    def _now(self):
        import time
        return self._data.get("now") or time.time()

    def _env_groups(self):
        g = {}
        for h in self._data.get("hosts", []):
            g.setdefault(h.get("environment") or "Unassigned", []).append(h)
        return g

    # ---- rendering ----
    def _rebuild(self):
        _clear_layout(self._crumb)
        _clear_layout(self._legend)
        _clear_layout(self._content_lay)

        now = self._now()
        t0, t1 = now - self._window, now
        vt0, vt1 = self._zoom if self._zoom else (t0, t1)
        hosts = self._data.get("hosts", [])
        groups = self._env_groups()
        env_names = sorted(groups.keys())
        env_color = {e: ENV_COLORS[i % len(ENV_COLORS)] for i, e in enumerate(env_names)}

        # breadcrumb
        if self._sel_env or self._sel_host:
            b = QPushButton("← All environments")
            b.clicked.connect(self._go_all)
            self._crumb.addWidget(b)
        if self._sel_host:
            b2 = QPushButton(f"← {self._sel_env or 'hosts'}")
            b2.clicked.connect(self._go_env)
            self._crumb.addWidget(b2)
        if self._zoom:
            bz = QPushButton(f"⤢ {_fmt_clock(vt0)}–{_fmt_clock(vt1)} · reset zoom")
            bz.clicked.connect(self._reset_zoom_page)
            self._crumb.addWidget(bz)
        else:
            hint = QLabel("drag a chart to zoom")
            hint.setStyleSheet(f"color:{PAL['faint']}; font-size:11px;")
            self._crumb.addWidget(hint)

        if not hosts:
            empty = QLabel("No performance samples yet. Agents report metrics on heartbeat — "
                           "data appears within a minute or two of an up-to-date agent. SSH-only hosts aren't sampled.")
            empty.setWordWrap(True)
            empty.setStyleSheet(f"color:{PAL['faint']}; padding:16px;")
            self._content_lay.addWidget(empty)
            self._content_lay.addStretch()
            return

        if self._sel_host:
            host = next((h for h in hosts if h.get("host_id") == self._sel_host), None)
            if host is None:
                self._sel_host = None
                self._rebuild()
                return
            self._render_host(host, vt0, vt1)
            return

        if self._sel_env:
            drill = sorted(groups.get(self._sel_env, []), key=lambda h: h.get("hostname", ""))
            host_color = {h["host_id"]: HOST_COLORS[i % len(HOST_COLORS)] for i, h in enumerate(drill)}
            for h in drill:
                self._legend.addWidget(_legend_chip(
                    h.get("hostname", h["host_id"]), host_color[h["host_id"]],
                    lambda _=False, hid=h["host_id"]: self._open_host(hid)))
            self._legend.addStretch()
            self._render_charts(lambda metric: [
                {"label": h.get("hostname"), "color": host_color[h["host_id"]],
                 "points": [(s["t"], metric[3](s)) for s in h["samples"]
                            if metric[3](s) is not None and vt0 <= s["t"] <= vt1]}
                for h in drill], vt0, vt1)
            return

        # overview: one averaged line per environment
        for e in env_names:
            self._legend.addWidget(_legend_chip(
                f"{e}  ({len(groups[e])})", env_color[e],
                lambda _=False, env=e: self._open_env(env)))
        self._legend.addStretch()
        self._render_charts(lambda metric: [
            {"label": e, "color": env_color[e],
             "points": _bucket_average([s for h in groups[e] for s in h["samples"]], metric[3], vt0, vt1)}
            for e in env_names], vt0, vt1)

    def _reset_zoom_page(self):
        self._zoom = None
        self._rebuild()

    def _render_charts(self, series_for, t0, t1):
        self._last_series_for, self._last_t0, self._last_t1 = series_for, t0, t1
        grid = QGridLayout()
        grid.setSpacing(12)
        for i, metric in enumerate(METRICS):
            card, lay = _chart_card(metric[1], on_expand=lambda _=False, m=metric: self._open_focus(m))
            chart = LineChart(metric[2])
            chart.set_data(series_for(metric), t0, t1)
            chart.on_zoom = self._chart_zoom
            lay.addWidget(chart)
            grid.addWidget(card, i // 2, i % 2)
        holder = QWidget()
        holder.setStyleSheet("background:transparent;")
        holder.setLayout(grid)
        self._content_lay.addWidget(holder)
        self._content_lay.addStretch()

    def _render_host(self, host, t0, t1):
        latest = host["samples"][-1] if host.get("samples") else None
        # current-value stat strip
        if latest:
            strip = QHBoxLayout()
            strip.setSpacing(8)
            for label, kind, fn, rate in [
                ("CPU", "pct", _cpu, False), ("Memory", "pct", lambda s: s.get("mem"), False),
                ("Swap", "pct", lambda s: s.get("swap"), False), ("Disk", "pct", lambda s: s.get("disk"), False),
                ("Load 1m", "num", lambda s: s.get("load1"), False),
                ("Net in", "bytes", lambda s: s.get("net_rx"), True), ("Net out", "bytes", lambda s: s.get("net_tx"), True),
                ("Processes", "num", lambda s: s.get("procs"), False),
            ]:
                strip.addWidget(_stat(label, _fmt_metric(kind, fn(latest), rate)))
            strip.addStretch()
            sw = QWidget()
            sw.setStyleSheet("background:transparent;")
            sw.setLayout(strip)
            self._content_lay.addWidget(sw)

        def series_for(metric):
            return [{"label": host.get("hostname"), "color": HOST_COLORS[0],
                     "points": [(s["t"], metric[3](s)) for s in host["samples"]
                                if metric[3](s) is not None and t0 <= s["t"] <= t1]}]
        self._last_series_for, self._last_t0, self._last_t1 = series_for, t0, t1
        grid = QGridLayout()
        grid.setSpacing(12)
        for i, metric in enumerate(METRICS):
            now_val = _fmt_metric(metric[2], metric[3](latest), metric[2] == "bytes") if latest else None
            card, lay = _chart_card(metric[1], now_val, on_expand=lambda _=False, m=metric: self._open_focus(m))
            chart = LineChart(metric[2])
            chart.set_data(series_for(metric), t0, t1)
            chart.on_zoom = self._chart_zoom
            lay.addWidget(chart)
            grid.addWidget(card, i // 2, i % 2)
        gh = QWidget()
        gh.setStyleSheet("background:transparent;")
        gh.setLayout(grid)
        self._content_lay.addWidget(gh)

        # current-detail snapshot (fetched lazily)
        self._snap_card = _card_frame()
        sl = QVBoxLayout(self._snap_card)
        sl.setContentsMargins(12, 10, 12, 10)
        title = QLabel("CURRENT DETAIL")
        title.setStyleSheet(f"border:none; background:transparent; color:{PAL['faint']}; font-size:11px; font-weight:bold;")
        sl.addWidget(title)
        self._snap_status = QLabel("Loading detail…")
        self._snap_status.setStyleSheet(f"border:none; background:transparent; color:{PAL['faint']};")
        sl.addWidget(self._snap_status)
        self._snap_body = QVBoxLayout()
        sl.addLayout(self._snap_body)
        self._content_lay.addWidget(self._snap_card)
        self._content_lay.addStretch()
        self._load_snapshot(host["host_id"])

    def _load_snapshot(self, host_id):
        self._snap_worker = _Worker(lambda: api.get_host_snapshot(host_id))
        self._snap_worker.done.connect(self._render_snapshot)
        self._snap_worker.fail.connect(lambda m: self._snap_status.setText(f"Error: {m}"))
        self._snap_worker.start()

    def _render_snapshot(self, data):
        snap = (data or {}).get("snapshot")
        _clear_layout(self._snap_body)
        if not snap:
            self._snap_status.setText("No detail snapshot yet (needs an up-to-date agent).")
            return
        self._snap_status.setVisible(False)
        cols = QHBoxLayout()
        cols.setSpacing(16)

        # per-core CPU
        percpu = snap.get("percpu") or []
        if percpu:
            box = QVBoxLayout()
            box.addWidget(_mini_title(f"Per-core CPU ({len(percpu)})"))
            for i, c in enumerate(percpu):
                r = QHBoxLayout()
                lab = QLabel(f"cpu{i}")
                lab.setStyleSheet(f"border:none; background:transparent; color:{PAL['faint']}; font-size:11px;")
                lab.setFixedWidth(40)
                r.addWidget(lab)
                bar = QProgressBar()
                bar.setRange(0, 100)
                bar.setTextVisible(False)
                bar.setFixedHeight(8)
                bar.setValue(int(c))
                col = "#e06c6c" if c >= 90 else "#e0a83a" if c >= 75 else "#4ec07a"
                bar.setStyleSheet(f"QProgressBar{{background:{PAL['track']};border:none;border-radius:4px;}}"
                                  f"QProgressBar::chunk{{background:{col};border-radius:4px;}}")
                r.addWidget(bar, 1)
                v = QLabel(f"{round(c)}%")
                v.setStyleSheet(f"border:none; background:transparent; color:{PAL['text']}; font-size:11px;")
                v.setFixedWidth(36)
                v.setAlignment(Qt.AlignRight)
                r.addWidget(v)
                box.addLayout(r)
            cols.addLayout(box, 1)

        # memory + top processes
        mem = snap.get("mem") or {}
        if mem:
            box = QVBoxLayout()
            box.addWidget(_mini_title("Memory"))
            for k, lbl in [("total_mb", "Total"), ("available_mb", "Available"), ("cached_mb", "Cached"),
                           ("swap_used_mb", "Swap used")]:
                box.addLayout(_kv(lbl, _mb(mem.get(k))))
            cols.addLayout(box, 1)

        self._snap_body.addLayout(cols)

        for key, title in [("top_cpu", "Top processes — CPU"), ("top_mem", "Top processes — memory")]:
            procs = snap.get(key) or []
            if not procs:
                continue
            self._snap_body.addWidget(_mini_title(title))
            for pr in procs:
                detail = (f"{pr.get('cpu')}% · {pr.get('mem_mb')} MB" if key == "top_cpu"
                          else f"{pr.get('mem_mb')} MB")
                self._snap_body.addLayout(_kv(f"{pr.get('name')} ({pr.get('pid')})", detail))

    # ---- navigation ----
    def _open_env(self, env):
        self._sel_env, self._sel_host, self._zoom = env, None, None
        self._rebuild()

    def _open_host(self, host_id):
        self._sel_host, self._zoom = host_id, None
        self._rebuild()

    def _go_all(self):
        self._sel_env = self._sel_host = None
        self._zoom = None
        self._rebuild()

    def _go_env(self):
        self._sel_host = None
        self._zoom = None
        self._rebuild()

    def _chart_zoom(self, t0, t1):
        self._zoom = (t0, t1)
        self._rebuild()

    def _open_focus(self, metric):
        sf = getattr(self, "_last_series_for", None)
        if not sf:
            return
        dlg = _FocusChartDialog(metric[1], sf(metric), self._last_t0, self._last_t1, metric[2], self)
        self._focus_dialogs.append(dlg)
        dlg.show()
        dlg.raise_()


def _mini_title(text):
    t = QLabel(text)
    t.setStyleSheet(f"border:none; background:transparent; color:{PAL['text']}; font-weight:bold; font-size:12px; margin-top:4px;")
    return t


def _kv(label, value):
    r = QHBoxLayout()
    r.setContentsMargins(0, 0, 0, 0)
    ln = QLabel(label)
    ln.setStyleSheet(f"border:none; background:transparent; color:{PAL['faint']}; font-size:12px;")
    r.addWidget(ln)
    r.addStretch()
    lv = QLabel(value)
    lv.setStyleSheet(f"border:none; background:transparent; color:{PAL['text']}; font-size:12px;")
    r.addWidget(lv)
    return r


def _mb(v):
    if v in (None, ""):
        return "—"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{v / 1024:.1f} GB" if v >= 1024 else f"{round(v)} MB"


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
