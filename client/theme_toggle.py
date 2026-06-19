from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QPushButton

from client import theme

# Plain Unicode glyphs rather than a qtawesome icon - unlike
# DashboardCard's badge (client/dashboard_card.py), this control has
# to always work even if qtawesome isn't installed, since it's the
# only way back out of light mode if something about the icon font
# ever goes wrong.
_MOON = "\U0001F319"
_SUN = "☀"


class ThemeToggle(QFrame):
    """Small pill-shaped dark/light mode switch shown in the
    dashboard header (client/home.py). Persists the choice via
    theme.set_theme_mode(), which re-applies the app-wide stylesheet
    and notifies every live DashboardCard (and this toggle itself in
    any other already-open window) to re-skin immediately - no
    restart needed."""

    def __init__(self):
        super().__init__()

        self.setObjectName("themeToggle")
        self.setFixedHeight(28)

        row = QHBoxLayout(self)
        row.setContentsMargins(3, 3, 3, 3)
        row.setSpacing(2)

        self.dark_btn = QPushButton(_MOON)
        self.light_btn = QPushButton(_SUN)
        for btn in (self.dark_btn, self.light_btn):
            btn.setFixedSize(24, 22)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFlat(True)
            row.addWidget(btn)

        self.dark_btn.clicked.connect(lambda: theme.set_theme_mode("dark"))
        self.light_btn.clicked.connect(lambda: theme.set_theme_mode("light"))

        theme.add_theme_listener(self._sync)
        self._sync()

    def _sync(self):
        mode = theme.get_theme_mode()

        if mode == "light":
            pill_bg, active_bg = "#F1F2F4", "#FFFFFF"
            active_color, inactive_color = "#1F2430", "#9AA0A8"
        else:
            pill_bg, active_bg = "#1C2024", "#2A2F35"
            active_color, inactive_color = "#E7E9EC", "#6B7280"

        self.setStyleSheet(
            f"QFrame#themeToggle {{ background-color: {pill_bg}; "
            "border-radius: 14px; }"
        )

        for btn, active in (
            (self.dark_btn, mode == "dark"),
            (self.light_btn, mode == "light"),
        ):
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {active_bg if active else 'transparent'};
                    color: {active_color if active else inactive_color};
                    border: none;
                    border-radius: 11px;
                    font-size: 12px;
                }}
            """)
