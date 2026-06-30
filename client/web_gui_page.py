"""
Browser Access (Web GUI) management page.

The Web GUI is a separate browser-based front end to the controller (see
webgui/), run as its own process. This page is the dashboard's control
panel for it: start/stop the service, see whether it's healthy, get the
URL to open from a Windows (or any) machine, and run diagnostics that
explain why it won't start (front end not built, deps missing, etc.) -
all without needing a shell on the controller.

Mirrors client/webserver_portal_page.py, which does the same for the
Webserver Portal.
"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QTextEdit, QApplication,
)
from PySide6.QtCore import Qt

from client import api, theme
from client.branding import make_page_header


class WebGuiPage(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Browser Access (Web GUI)")
        self.resize(640, 640)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(10)

        layout.addWidget(make_page_header("Browser Access (Web GUI)"))

        intro = QLabel(
            "Runs a browser-based version of this controller so Windows and other "
            "machines can manage your fleet without the desktop app. Start it here, "
            "then open the URL below from any machine that can reach this controller."
        )
        theme.style_hint_label(intro)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # ---- status ----
        self.status_label = QLabel("Status: unknown")
        self.status_label.setStyleSheet("font-size:16px;font-weight:bold;")
        layout.addWidget(self.status_label)

        self.url_label = QLabel("")
        self.url_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.url_label.setWordWrap(True)
        layout.addWidget(self.url_label)

        buttons = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        self.start_btn = QPushButton("Start Web GUI")
        self.start_btn.clicked.connect(self.start_service)
        self.stop_btn = QPushButton("Stop Web GUI")
        self.stop_btn.clicked.connect(self.stop_service)
        self.copy_btn = QPushButton("Copy URL")
        self.copy_btn.clicked.connect(self.copy_url)
        self.install_btn = QPushButton("Install Dependencies")
        self.install_btn.setToolTip(
            "Install the Python dependencies and build the browser front end. "
            "Use this if the installer didn't set them up (see Diagnostics below).")
        self.install_btn.clicked.connect(self.install_dependencies)
        for b in (self.refresh_btn, self.start_btn, self.stop_btn, self.copy_btn,
                  self.install_btn):
            buttons.addWidget(b)
        buttons.addStretch()
        layout.addLayout(buttons)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color:#2e3742;")
        layout.addWidget(line)

        # ---- diagnostics ----
        diag_label = QLabel("Diagnostics")
        diag_label.setStyleSheet("font-size:15px;font-weight:bold;")
        layout.addWidget(diag_label)

        diag_hint = QLabel(
            "Checks that must pass before the Web GUI can start. If the front end "
            "isn't built, run the installer (install_sysible.sh) or, on the "
            "controller: cd webgui/frontend && npm install && npm run build."
        )
        theme.style_hint_label(diag_hint)
        diag_hint.setWordWrap(True)
        layout.addWidget(diag_hint)

        self.diag_view = QTextEdit()
        self.diag_view.setReadOnly(True)
        self.diag_view.setStyleSheet("font-family: monospace;")
        layout.addWidget(self.diag_view, 1)

        self._urls = []
        self.refresh()

    # ------------------------------------------------------------------
    def _set_status(self, running, port, scheme):
        if running:
            self.status_label.setText("Status: RUNNING")
            self.status_label.setStyleSheet(
                "font-size:16px;font-weight:bold;color:#2ea043;")
        else:
            self.status_label.setText("Status: STOPPED")
            self.status_label.setStyleSheet(
                "font-size:16px;font-weight:bold;color:#d9544d;")
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)

        if running and port:
            try:
                ips = api.get_local_ips()
            except Exception:
                ips = []
            self._urls = [f"{scheme}://{ip}:{port}/" for ip in ips] or \
                         [f"{scheme}://<controller-ip>:{port}/"]
            self.url_label.setText(
                "Open from a browser:\n  " + "\n  ".join(self._urls)
                + ("\n\nNote: this uses the controller's self-signed certificate, so "
                   "the browser will warn the first time - that's expected on a LAN."
                   if scheme == "https" else
                   "\n\nWarning: serving plain HTTP - your admin password is sent in the "
                   "clear. Generate the controller cert (install_sysible.sh) for HTTPS.")
            )
            self.copy_btn.setEnabled(True)
        else:
            self._urls = []
            self.url_label.setText("Start the Web GUI to get its access URL.")
            self.copy_btn.setEnabled(False)

    def _render_diagnostics(self, diag):
        lines = []
        for c in diag.get("checks", []):
            mark = "[OK]  " if c["ok"] else "[!!]  "
            lines.append(f"{mark}{c['name']}: {c['detail']}")
        self.diag_view.setPlainText("\n".join(lines))

    def refresh(self):
        try:
            st = api.get_webgui_status()
            self._set_status(st.get("running"), st.get("port"), st.get("scheme", "http"))
        except Exception as e:
            self.status_label.setText("Status: controller unreachable")
            self.status_label.setStyleSheet("font-size:16px;font-weight:bold;color:#f5a623;")
            self.url_label.setText(str(e))
        try:
            self._render_diagnostics(api.get_webgui_diagnostics())
        except Exception as e:
            self.diag_view.setPlainText(f"Could not load diagnostics: {e}")

    def start_service(self):
        self.start_btn.setEnabled(False)
        self.status_label.setText("Status: starting…")
        # The first start may build the front end / install deps - say so,
        # since it can take a minute or two before the service is up.
        self.diag_view.setPlainText(
            "Starting the Web GUI. If the front end isn't built yet this will "
            "install dependencies and build it first — that can take a minute or "
            "two on the first run. Please wait…")
        QApplication.processEvents()
        try:
            result = api.start_webgui()
        except Exception as e:
            self.diag_view.setPlainText(f"Start failed: {e}")
            self.refresh()
            return
        if result.get("error"):
            self.diag_view.setPlainText(result["error"])
        self.refresh()

    def install_dependencies(self):
        self.install_btn.setEnabled(False)
        self.install_btn.setText("Installing…")
        self.diag_view.setPlainText(
            "Installing dependencies and building the front end — this can take a "
            "minute or two (npm install is the slow part). Please wait…")
        QApplication.processEvents()
        try:
            result = api.install_webgui_dependencies()
        except Exception as e:
            self.diag_view.setPlainText(
                f"Install failed: {e}\n\nIf this timed out, the build may still be "
                "running on the controller — click Refresh shortly.")
            self.install_btn.setEnabled(True)
            self.install_btn.setText("Install Dependencies")
            return

        lines = ["Installed:" if result.get("ok") else "Finished with problems:"]
        for s in result.get("steps", []):
            lines.append(f"\n[{'OK' if s['ok'] else '!!'}] {s['name']}")
            if s.get("output"):
                lines.append(s["output"])
        self.diag_view.setPlainText("\n".join(lines))
        self.install_btn.setEnabled(True)
        self.install_btn.setText("Install Dependencies")
        # Re-read status/diagnostics (don't clobber the install log just shown).
        try:
            st = api.get_webgui_status()
            self._set_status(st.get("running"), st.get("port"), st.get("scheme", "http"))
        except Exception:
            pass

    def stop_service(self):
        try:
            api.stop_webgui()
        except Exception as e:
            self.diag_view.setPlainText(f"Stop failed: {e}")
        self.refresh()

    def copy_url(self):
        if self._urls:
            QApplication.clipboard().setText(self._urls[0])
