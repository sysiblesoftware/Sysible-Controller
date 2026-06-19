from PySide6.QtCore import Qt
from PySide6.QtWidgets import QVBoxLayout, QLabel, QFrame

from client import theme

try:
    import qtawesome as qta
except ImportError:
    # qtawesome is an optional dependency (requirements.txt) purely
    # for the colored icon badge below - if it's missing for any
    # reason, fall back to the old text-only card instead of crashing
    # the whole dashboard over a missing icon font.
    qta = None


class DashboardCard(QFrame):
    """Clickable card for one dashboard/menu entry - an icon badge,
    bold title, and a short description, styled to match the current
    theme (see client/theme.py) rather than a plain QPushButton, so a
    grid of these reads as a set of distinct tools instead of a stack
    of identical gray bars.

    Shared between client/home.py (the top-level dashboard) and any
    page that wants its own sub-menu of tiles (e.g.
    client/system_administration_page.py) - pulled out of home.py so
    the two don't end up in a circular import.

    `icon` is a qtawesome icon name (e.g. "fa5s.server"); `color` is a
    key into theme.CATEGORY_COLORS. Both are optional - a card created
    without them just skips the badge, so existing call sites don't
    break - but every current call site passes both.
    """

    def __init__(self, title, description, on_click, icon=None, color="slate"):
        super().__init__()

        self._on_click = on_click
        self._icon_name = icon
        self._color_key = color

        self.setObjectName("dashboardCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(112)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(8)

        self.icon_badge = QLabel()
        self.icon_badge.setFixedSize(34, 34)
        self.icon_badge.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.icon_badge)

        self.title_label = QLabel(title)
        self.title_label.setWordWrap(True)

        self.desc_label = QLabel(description)
        self.desc_label.setWordWrap(True)

        layout.addWidget(self.title_label)
        layout.addWidget(self.desc_label)
        layout.addStretch()

        self._apply_theme()
        theme.add_theme_listener(self._apply_theme)

    def _apply_theme(self):
        """Recompute every color this card uses for the current mode.
        Called once at construction and again from client/theme.py
        every time the user flips the dark/light toggle, so a card
        that's already on screen re-skins itself immediately."""
        mode = theme.get_theme_mode()
        badge_bg, icon_color = theme.get_category_colors(self._color_key, mode)

        if mode == "light":
            card_bg, card_border = "#FFFFFF", "#E6E8EB"
            hover_bg, hover_border = "#F3F6FB", "#C7D4E8"
            title_color, desc_color = "#1F2430", "#6B7280"
        else:
            card_bg, card_border = "#1B1E22", "#2A2E33"
            hover_bg, hover_border = "#26313F", "#3D5066"
            title_color, desc_color = "#E7E9EC", "#8B929B"

        self.setStyleSheet(f"""
            QFrame#dashboardCard {{
                background-color: {card_bg};
                border: 1px solid {card_border};
                border-radius: 8px;
            }}
            QFrame#dashboardCard:hover {{
                background-color: {hover_bg};
                border: 1px solid {hover_border};
            }}
            QFrame#dashboardCard QLabel {{
                background: transparent;
                border: none;
            }}
        """)

        # A widget's own stylesheet outranks its parent's for
        # properties it sets itself, so this badge background/radius
        # wins over the QFrame#dashboardCard QLabel {background:
        # transparent} rule just above for this one label.
        self.icon_badge.setStyleSheet(
            f"background-color: {badge_bg}; border-radius: 8px;"
        )
        if self._icon_name and qta is not None:
            try:
                icon = qta.icon(self._icon_name, color=icon_color)
                self.icon_badge.setPixmap(icon.pixmap(18, 18))
            except Exception:
                self.icon_badge.clear()
        else:
            self.icon_badge.clear()

        self.title_label.setStyleSheet(
            f"font-size:14px; font-weight:bold; color:{title_color}; "
            "background:transparent; border:none;"
        )
        self.desc_label.setStyleSheet(
            f"font-size:10.5px; color:{desc_color}; "
            "background:transparent; border:none;"
        )

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._on_click:
            self._on_click()
        super().mousePressEvent(event)
