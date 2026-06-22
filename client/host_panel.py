"""
Shared "Target Hosts" left-column panel used by every System
Administration tool (#352).

These pages used to stack a short, height-capped horizontal strip (a
row of buttons above a QListWidget capped to roughly 48-160px tall)
above the rest of the page's content. That was fine with a handful of
hosts, but became unusable once a fleet grew past whatever fit in
~6 visible rows - the rest just disappeared into a tiny scrollbar.

build_host_panel() instead returns a fixed-width QWidget meant to sit
as the left item in a QHBoxLayout that spans the page's full height,
with the QListWidget set to expand into whatever vertical space is
available instead of being capped, so far more hosts are visible (and
the rest just scroll normally) without shrinking the rest of the
panel.
"""

from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSizePolicy

PANEL_WIDTH = 268


def build_host_panel(title, host_list, button_rows, extra_widgets=None, width=PANEL_WIDTH):
    """
    title: heading text for the panel, e.g. "Target Hosts (agent + SSH)".
    host_list: the page's already-constructed QListWidget - resized in
        place (no fixed/capped height, expanding vertical size policy)
        and placed into the returned panel.
    button_rows: list of lists of QPushButton - each inner list becomes
        one horizontal row under the title. Short rows (2-3 buttons)
        fit this narrow column far better than one long row of 5+.
    extra_widgets: optional list of widgets (e.g. a status label) added
        below the host list, in order.
    width: fixed column width. Defaults to PANEL_WIDTH; pages that show
        extra per-row detail (e.g. Remote Administration lists each
        host's IP next to it) can pass a wider value.
    Returns a QWidget ready to be the left-hand item in a QHBoxLayout.
    """
    panel = QWidget()
    panel.setFixedWidth(width)
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(0, 0, 6, 8)
    layout.setSpacing(6)

    title_label = QLabel(title)
    title_label.setStyleSheet("font-weight: bold;")
    title_label.setWordWrap(True)
    layout.addWidget(title_label)

    # Each row of buttons shares its width evenly and is allowed to
    # shrink its own internal padding, so labels like "Refresh Hosts"
    # are no longer clipped at the edges in this narrow fixed-width
    # column. Tighter, uniform spacing also reads less cluttered than
    # the default per-button size hints did.
    for row in button_rows:
        row_layout = QHBoxLayout()
        row_layout.setSpacing(6)
        row_layout.setContentsMargins(0, 0, 0, 0)
        for btn in row:
            # The stock "Refresh Hosts" label is the one button text wide
            # enough to clip inside this fixed-width column once it has to
            # share a row with Select All / Deselect All. Normalising it to
            # "Refresh" here keeps every page's host panel fitting cleanly
            # without each page having to special-case its own label.
            if btn.text() == "Refresh Hosts":
                btn.setText("Refresh")
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            btn.setMinimumWidth(0)
            btn.setMinimumHeight(28)
            # Trim the default horizontal padding so the text uses the
            # button's full width instead of being squeezed/clipped.
            # (None of the buttons routed through here carry their own
            # stylesheet, so a plain set is safe.)
            btn.setStyleSheet("QPushButton { padding: 3px 6px; }")
            row_layout.addWidget(btn)
        layout.addLayout(row_layout)

    host_list.setMinimumHeight(0)
    host_list.setMaximumHeight(16777215)
    host_list.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
    layout.addWidget(host_list, 1)

    for widget in (extra_widgets or []):
        layout.addWidget(widget)

    return panel
