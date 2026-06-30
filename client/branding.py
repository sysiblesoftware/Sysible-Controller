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
from PySide6.QtWidgets import QHBoxLayout, QLabel, QFrame

PROJECT_ROOT = Path(__file__).resolve().parent.parent

LOGO_PATH = PROJECT_ROOT / "sysible_logo.png"


HEADER_BAR_COLOR = "#3C4B64"  # same navy as the primary buttons


def make_page_header(title_text, font_size=18, logo_height=28):
    """A navy banner (logo + centered white title) for the top of a popout
    page — a single in-app "title bar" that breaks the page up from the gray
    content below and stays consistent across every window. (The OS window
    title bar above it is drawn by the window manager and can't be recolored
    from the app.) Matches the navy of the primary buttons in both themes.

    Returns a QWidget — hand it to the page's outer layout with `addWidget`
    (e.g. `main.addWidget(make_page_header("Service Management"))`).

    Same isNull() guard as before: if LOGO_PATH is missing/unreadable, the
    banner silently falls back to a title-only look.
    """
    bar = QFrame()
    bar.setObjectName("pageHeaderBar")
    # Scope the navy + white-text to this banner only (object-name selector) so
    # it overrides the theme's default QLabel color and isn't affected by it.
    bar.setStyleSheet(
        f"QFrame#pageHeaderBar{{background:{HEADER_BAR_COLOR}; border:none; border-radius:8px;}}"
        f"QFrame#pageHeaderBar QLabel{{background:transparent; color:#FFFFFF;}}")
    row = QHBoxLayout(bar)
    row.setContentsMargins(16, 11, 16, 11)
    row.setSpacing(12)

    logo_pixmap = QPixmap(str(LOGO_PATH))
    if not logo_pixmap.isNull():
        logo_label = QLabel()
        logo_label.setPixmap(
            logo_pixmap.scaledToHeight(logo_height, Qt.SmoothTransformation)
        )
        row.addWidget(logo_label)

    title = QLabel(title_text)
    title.setAlignment(Qt.AlignCenter)
    title.setStyleSheet(f"background:transparent; color:#FFFFFF; font-size:{font_size}px; font-weight:bold;")
    row.addWidget(title, 1)

    return bar


def center_on_screen(widget):
    """Center `widget` on the screen it's about to appear on (falling back to
    the primary screen). Parentless top-level windows otherwise open at the
    window manager's default spot - often the top-left corner - which looks
    broken for a startup login/setup dialog. Call from the widget's showEvent
    so its final size is known."""
    from PySide6.QtGui import QGuiApplication

    screen = widget.screen() or QGuiApplication.primaryScreen()
    if screen is None:
        return
    geo = screen.availableGeometry()
    frame = widget.frameGeometry()
    frame.moveCenter(geo.center())
    widget.move(frame.topLeft())
