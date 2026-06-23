"""App-wide theming: the dark/light QSS stylesheets, the matching
QPalette for native chrome QSS doesn't reach (scrollbars, popups,
menus), the category-accent colors DashboardCard's icon badges use,
and the small pub/sub bit that lets already-open windows re-skin
themselves the instant the mode is toggled rather than needing the
app restarted.

Persisted via QSettings rather than e.g. a flat file in the project
root, since this is a per-user desktop preference (like window
geometry would be), not server/install state - no reason for it to
live alongside version.py or be backed up with the rest of the
controller's data.
"""

from PySide6.QtCore import QSettings
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

_SETTINGS_ORG = "Sysible"
_SETTINGS_APP = "Controller"
_MODE_KEY = "appearance/mode"

ENTERPRISE_THEME = """
QWidget {
    background-color: #1E1E1E;
    color: #EAEAEA;
    font-size: 10pt;
}

QLabel {
    color: #EAEAEA;
}

QLineEdit {
    background-color: #353535;
    border: 1px solid #505050;
    padding: 5px;
    border-radius: 4px;
    color: white;
}

QListWidget {
    background-color: #2B2B2B;
    border: 1px solid #505050;
    color: white;
}

QTableWidget {
    background-color: #2B2B2B;
    border: 1px solid #505050;
    color: white;
    gridline-color: #3A3A3A;
}

QHeaderView::section {
    background-color: #2B2B2B;
    color: #9aa5b1;
    border: 1px solid #3A3A3A;
    padding: 4px;
}

QPushButton {
    background-color: #3C4B64;
    color: white;
    border: 1px solid #506080;
    border-radius: 5px;
    padding: 6px;
}

QPushButton:hover {
    background-color: #4C6285;
}

QPushButton:pressed {
    background-color: #23395D;
}

QGroupBox {
    border: 1px solid #505050;
    margin-top: 10px;
    padding-top: 10px;
}

QGroupBox::title {
    color: white;
}

QCheckBox {
    spacing: 8px;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border: 1px solid #8a93a0;
    border-radius: 4px;
    background: rgba(127,127,127,0.15);
}
QCheckBox::indicator:hover {
    border-color: #3ac95a;
}
QCheckBox::indicator:checked {
    background: #3ac95a;
    border-color: #2fae4b;
}
QCheckBox::indicator:disabled {
    border-color: #555;
    background: rgba(127,127,127,0.08);
}

/* Host-list (and other checkable list) checkboxes. These are item-view
   indicators, not QCheckBox, so they need their own rule - without it
   they fall back to the near-invisible default. A bright orange border
   makes them readable against the dark row background. */
QListWidget::indicator, QListView::indicator, QTreeView::indicator {
    width: 16px;
    height: 16px;
    border: 2px solid #f5a623;
    border-radius: 4px;
    background: rgba(127,127,127,0.20);
}
QListWidget::indicator:checked, QListView::indicator:checked, QTreeView::indicator:checked {
    background: #3ac95a;
    border-color: #f5a623;
}

QScrollBar:vertical {
    background: #1E1E1E;
    width: 13px;
    margin: 0px;
}

QScrollBar::handle:vertical {
    background: #5A5A5A;
    min-height: 28px;
    border-radius: 5px;
}

QScrollBar::handle:vertical:hover {
    background: #6E6E6E;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
    background: none;
}

QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: none;
}

QScrollBar:horizontal {
    background: #1E1E1E;
    height: 13px;
    margin: 0px;
}

QScrollBar::handle:horizontal {
    background: #5A5A5A;
    min-width: 28px;
    border-radius: 5px;
}

QScrollBar::handle:horizontal:hover {
    background: #6E6E6E;
}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0px;
    background: none;
}

QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    background: none;
}
"""

# Light counterpart to ENTERPRISE_THEME, same selectors throughout so
# nothing silently falls back to unstyled Qt defaults in this mode -
# every rule above has a match below.
DAYLIGHT_THEME = """
QWidget {
    background-color: #FAFBFC;
    color: #1F2430;
    font-size: 10pt;
}

QLabel {
    color: #1F2430;
}

QLineEdit {
    background-color: #FFFFFF;
    border: 1px solid #D8DCE2;
    padding: 5px;
    border-radius: 4px;
    color: #1F2430;
}

QListWidget {
    background-color: #FFFFFF;
    border: 1px solid #D8DCE2;
    color: #1F2430;
}

QTableWidget {
    background-color: #FFFFFF;
    border: 1px solid #D8DCE2;
    color: #1F2430;
    gridline-color: #E6E8EB;
}

QHeaderView::section {
    background-color: #F1F2F4;
    color: #5B6472;
    border: 1px solid #E3E6EA;
    padding: 4px;
}

QPushButton {
    background-color: #2F6FED;
    color: white;
    border: 1px solid #2557C7;
    border-radius: 5px;
    padding: 6px;
}

QPushButton:hover {
    background-color: #4C84F0;
}

QPushButton:pressed {
    background-color: #2557C7;
}

QGroupBox {
    border: 1px solid #D8DCE2;
    margin-top: 10px;
    padding-top: 10px;
}

QGroupBox::title {
    color: #1F2430;
}

QCheckBox {
    spacing: 8px;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border: 1px solid #9aa3b0;
    border-radius: 4px;
    background: #ffffff;
}
QCheckBox::indicator:hover {
    border-color: #2fae4b;
}
QCheckBox::indicator:checked {
    background: #3ac95a;
    border-color: #2fae4b;
}
QCheckBox::indicator:disabled {
    border-color: #cbd0d8;
    background: #eef0f3;
}

/* Host-list (and other checkable list) checkboxes - see the dark-theme
   note above. Orange border so the boxes stand out in the host list. */
QListWidget::indicator, QListView::indicator, QTreeView::indicator {
    width: 16px;
    height: 16px;
    border: 2px solid #e08900;
    border-radius: 4px;
    background: #ffffff;
}
QListWidget::indicator:checked, QListView::indicator:checked, QTreeView::indicator:checked {
    background: #3ac95a;
    border-color: #e08900;
}

QScrollBar:vertical {
    background: #E9EBEE;
    width: 13px;
    margin: 0px;
}

QScrollBar::handle:vertical {
    background: #B0B6C0;
    min-height: 28px;
    border-radius: 5px;
}

QScrollBar::handle:vertical:hover {
    background: #939AA6;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
    background: none;
}

QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: none;
}

QScrollBar:horizontal {
    background: #E9EBEE;
    height: 13px;
    margin: 0px;
}

QScrollBar::handle:horizontal {
    background: #B0B6C0;
    min-width: 28px;
    border-radius: 5px;
}

QScrollBar::handle:horizontal:hover {
    background: #939AA6;
}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0px;
    background: none;
}

QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    background: none;
}
"""

# Shared status-message colors, for the small "Synced 3/3 hosts." /
# "Could not load hosts: ..." style labels scattered across the
# System Administration pages. Without these every such label stays
# the same neutral gray (or whatever it was last set to) regardless
# of whether the thing it's reporting on actually succeeded or
# failed - these give "a host just got enrolled" / "users just got
# fetched from a sync" the same kind of color-coded success/error cue
# the health-check verdicts and account-status labels already have.
# Saturated enough to read fine on both ENTERPRISE_THEME's dark
# backgrounds and DAYLIGHT_THEME's light ones, so they're left mode-
# independent rather than added to CATEGORY_COLORS below.
STATUS_NEUTRAL_COLOR = "#999"
STATUS_SUCCESS_COLOR = "#3ac95a"
STATUS_ERROR_COLOR = "#ff5c5c"
STATUS_WARNING_COLOR = "#f5a623"

# Secondary/"hint" text - the small explanatory line under a field or
# section header ("Changes apply the next time...", "Leave blank to
# keep the current value", etc.) found throughout the popout pages.
# Unlike the status colors above, this one *is* mode-dependent: it was
# always a mid-gray tuned for ENTERPRISE_THEME's dark background
# (#999/#888/#9aa5b1 are used interchangeably across older pages), and
# while that doesn't go invisible on DAYLIGHT_THEME's light
# background, it does come out noticeably low-contrast/washed-out
# there. HINT_COLOR_LIGHT is the equivalent gray re-tuned for a light
# background, matching the dashboard header subtitle's existing value.
HINT_COLOR_DARK = "#9aa5b1"
HINT_COLOR_LIGHT = "#6B7280"

# Category accent colors for DashboardCard's icon badge (see
# client/dashboard_card.py) - one hue per tile so the dashboard (and
# System Administration's sub-menu) reads as a set of distinct tools
# at a glance instead of a uniform stack. Each key holds a (badge_bg,
# icon_color) pair per mode - same hue family in both, just a dark
# tint + bright icon for dark mode vs a pastel tint + saturated icon
# for light mode, so a tile's accent reads as "the same color" across
# modes rather than flattening to one generic accent in light mode.
CATEGORY_COLORS = {
    "teal":   {"dark": ("#0F2B24", "#2BBD92"), "light": ("#D7F5EC", "#0F8F6C")},
    "slate":  {"dark": ("#1C2433", "#6F8FC7"), "light": ("#E3E8F0", "#44587A")},
    "purple": {"dark": ("#241F33", "#9B8DE0"), "light": ("#EDE7FB", "#6B46C1")},
    "coral":  {"dark": ("#2F1F18", "#E3805A"), "light": ("#FDE7DF", "#C1502E")},
    "amber":  {"dark": ("#2C2310", "#E0B04A"), "light": ("#FCF0D6", "#9A6B0A")},
    "green":  {"dark": ("#16301C", "#3AC95A"), "light": ("#DFF5E3", "#1F8A3B")},
    "rose":   {"dark": ("#33141F", "#E0608F"), "light": ("#FBE3EC", "#B13564")},
    "sky":    {"dark": ("#102A3B", "#4FA8E0"), "light": ("#DCEEFB", "#1C6FA0")},
    "indigo": {"dark": ("#1E2142", "#7C83E8"), "light": ("#E6E8FC", "#4A4FC4")},
    "copper": {"dark": ("#2E2014", "#C98A4B"), "light": ("#F7E8D9", "#9C5B1F")},
    "crimson": {"dark": ("#33161A", "#E0555A"), "light": ("#FBE2E3", "#C23B3F")},
    "graphite": {"dark": ("#20242A", "#9FB0BD"), "light": ("#E9EDF0", "#445564")},
}


def get_category_colors(key, mode=None):
    """(badge_bg, icon_color) for CATEGORY_COLORS key `key` in `mode`
    (or the current saved mode). Falls back to "slate" for an unknown
    key rather than raising, since this only ever drives cosmetics."""
    mode = mode or get_theme_mode()
    return CATEGORY_COLORS.get(key, CATEGORY_COLORS["slate"])[mode]


def get_hint_color(mode=None):
    """The secondary/"hint" text gray for `mode` (or the current saved
    mode)."""
    return HINT_COLOR_LIGHT if (mode or get_theme_mode()) == "light" else HINT_COLOR_DARK


def style_hint_label(label, extra=""):
    """Color `label` with the current mode's hint gray and keep it in
    sync if the mode changes later, via a closure registered with
    add_theme_listener(). Covers the common case - a one-line
    QLabel.setStyleSheet("color: <hint gray>;") for some explanatory
    text - in a single call instead of every page re-implementing its
    own theme-listener boilerplate just for that.

    `extra` is any additional CSS to keep alongside the color rule
    (e.g. "font-weight:bold;") for the handful of hint labels that
    also set something else.
    """
    def _update():
        label.setStyleSheet(f"color:{get_hint_color()};{extra}")

    _update()
    add_theme_listener(_update)


def get_theme_mode():
    """"dark" or "light" - whatever was last passed to
    set_theme_mode(), defaulting to "dark" (the look the app has
    always had) the first time it's ever run."""
    return QSettings(_SETTINGS_ORG, _SETTINGS_APP).value(_MODE_KEY, "dark")


def get_stylesheet(mode=None):
    return DAYLIGHT_THEME if (mode or get_theme_mode()) == "light" else ENTERPRISE_THEME


def build_palette(mode):
    """QPalette matching `mode`, for the native Qt chrome QSS doesn't
    reach (scrollbar tracks, combo-box popups, message boxes, etc.) -
    mirrors apply_dark_palette()'s old hardcoded values for "dark";
    "light" is the new counterpart."""
    palette = QPalette()

    if mode == "light":
        palette.setColor(QPalette.Window, QColor(250, 251, 252))
        palette.setColor(QPalette.Base, QColor(255, 255, 255))
        palette.setColor(QPalette.Text, QColor(31, 36, 48))
        palette.setColor(QPalette.WindowText, QColor(31, 36, 48))
        palette.setColor(QPalette.Button, QColor(241, 242, 244))
        palette.setColor(QPalette.ButtonText, QColor(31, 36, 48))
        # Mid/Dark/Midlight are what native (un-stylesheeted) widgets -
        # e.g. file dialogs, message boxes - derive scrollbar grooves,
        # sunken borders and bevels from. Left unset they default to a
        # near-white gray that all but disappears against Window/Base
        # above, which is the "whites bleed into each other" scrollbar
        # bug; setting them explicitly keeps light mode legible there.
        palette.setColor(QPalette.Midlight, QColor(233, 235, 238))
        palette.setColor(QPalette.Mid, QColor(176, 182, 192))
        palette.setColor(QPalette.Dark, QColor(120, 126, 136))
    else:
        palette.setColor(QPalette.Window, QColor(30, 30, 30))
        palette.setColor(QPalette.Base, QColor(25, 25, 25))
        palette.setColor(QPalette.Text, QColor(230, 230, 230))
        palette.setColor(QPalette.WindowText, QColor(230, 230, 230))
        palette.setColor(QPalette.Button, QColor(45, 45, 45))
        palette.setColor(QPalette.ButtonText, QColor(230, 230, 230))
        palette.setColor(QPalette.Midlight, QColor(60, 60, 60))
        palette.setColor(QPalette.Mid, QColor(90, 90, 90))
        palette.setColor(QPalette.Dark, QColor(15, 15, 15))

    return palette


def apply_theme(app, mode=None):
    """Apply (or re-apply) the palette + stylesheet for `mode` (or the
    saved mode if not given) to `app`. Called once at startup
    (client/main.py) and again every time set_theme_mode() below
    toggles the mode at runtime."""
    mode = mode or get_theme_mode()
    app.setPalette(build_palette(mode))
    app.setStyleSheet(get_stylesheet(mode))


# ---- live re-skinning for widgets that bake mode-specific colors
# into their own per-instance stylesheet instead of relying purely on
# the app-level QSS above (DashboardCard's icon badge + card colors,
# ThemeToggle's pill) ----
_theme_listeners = []


def add_theme_listener(callback):
    """Register a zero-arg callback to run every time the mode
    changes, so a widget can recompute its own colors immediately
    instead of needing the app restarted to pick up the new mode."""
    _theme_listeners.append(callback)


def set_theme_mode(mode):
    """Persist `mode` ("dark"/"light"), re-apply it to the running
    QApplication if there is one, and notify every registered
    listener - this is the one function anything (the dashboard's
    ThemeToggle, in practice) calls to actually switch modes."""
    assert mode in ("dark", "light"), mode

    QSettings(_SETTINGS_ORG, _SETTINGS_APP).setValue(_MODE_KEY, mode)

    app = QApplication.instance()
    if app is not None:
        apply_theme(app, mode)

    for callback in list(_theme_listeners):
        callback()
