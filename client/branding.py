"""Shared branding constants and helpers - the Sysible logo file path,
plus a small header-row builder, used by client/main.py (taskbar/window
icon + tray icon), client/home.py (dashboard header),
client/admin_login_dialog.py (login screen header), and every popout
page's title row (via make_page_header below) so the whole app stays in
sync if the asset ever moves.

LOGO_PATH is the single place that filename is referenced - update it
here if the file is ever renamed or relocated, and every screen picks
up the change.
"""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel

PROJECT_ROOT = Path(__file__).resolve().parent.parent

LOGO_PATH = PROJECT_ROOT / "sysible_logo.png"


def make_page_header(title_text, font_size=18, logo_height=28):
    """Small-logo + title row for the top of a popout page, mirroring
    the dashboard (home.py) and login screen (admin_login_dialog.py)
    branding treatments so every window in the app - not just those
    two - carries the logo.

    Returns a QHBoxLayout ready to hand straight to the page's outer
    layout (e.g. `main.addLayout(make_page_header("Service Management"))`)
    in place of the old bare title QLabel.

    Same isNull() guard as home.py/admin_login_dialog.py: if LOGO_PATH
    is ever missing or unreadable, the row silently falls back to a
    title-only look instead of showing a broken-image icon.
    """
    row = QHBoxLayout()

    logo_pixmap = QPixmap(str(LOGO_PATH))
    if not logo_pixmap.isNull():
        logo_label = QLabel()
        logo_label.setPixmap(
            logo_pixmap.scaledToHeight(logo_height, Qt.SmoothTransformation)
        )
        row.addWidget(logo_label)

    title = QLabel(title_text)
    title.setAlignment(Qt.AlignCenter)
    title.setStyleSheet(f"font-size:{font_size}px; font-weight:bold;")
    row.addWidget(title, 1)

    return row
