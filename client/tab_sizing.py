"""
Shared fix for the "dead space above a small results panel" layout bug
on every System Administration page that puts a QTabWidget of *action*
sub-tabs (e.g. Security Administration's SELinux / SSH / Audit &&
Logins / Updates && Policy / Hardening && Scans) directly above a
results/output QTabWidget.

Root cause: Qt's QStackedWidget (which QTabWidget uses internally to
hold its pages) sizes itself to the TALLEST page in the stack by
default, not the currently visible one - this is intentional upstream
so the widget doesn't visibly resize as the user switches tabs. But it
means a short tab (e.g. "Hardening & Scans", a couple of rows) still
reserves enough height to fit the tallest tab in the same QTabWidget
(e.g. "SELinux", which has five stacked sections), leaving a permanent
block of empty space below the short tab's actual content - and
because that space is "spent" by the action-tabs widget, the results
panel below it (even with an explicit stretch=1 in the page's main
layout) never gets to grow into it.

The fix: give every *inactive* page an Ignored vertical size policy
(excluding it from the stack's max-height computation) and the current
page Preferred, re-applied every time the current tab changes. Pages
with Ignored policy still report a sizeHint, but the layout system is
told to disregard it, so the QTabWidget's height tracks only whichever
page is actually showing - and the leftover vertical space then goes
to the results panel below, same as it would if the action tabs were a
single fixed-height widget instead of a multi-page one.

Usage, right after every addTab() call on an action-tabs QTabWidget:

    action_tabs = QTabWidget()
    action_tabs.addTab(self._build_one_tab(), "One")
    action_tabs.addTab(self._build_another_tab(), "Another")
    ...
    shrink_tabwidget_to_current_page(action_tabs)
    main.addWidget(action_tabs)
"""
from PySide6.QtWidgets import QSizePolicy, QScrollArea


def shrink_tabwidget_to_current_page(tab_widget, cap_height=False):
    """Makes `tab_widget` size itself to its currently visible page
    instead of the tallest page in the stack. Safe to call once, right
    after all of a QTabWidget's tabs have been added - it wires up
    `currentChanged` itself, so later tab switches keep tracking the
    new current page automatically.

    cap_height=True additionally caps the widget's maximum height to the
    current page's preferred height (plus the tab bar). Use this for an
    *action* tabs widget that sits above a stretchy results panel: the
    size-policy trick alone sometimes still leaves the action tabs taller
    than the visible page (a big block of dead space below short tabs),
    and the hard cap removes it so the results panel claims that space.
    Do NOT use it on a results tabs widget that is supposed to grow."""
    if cap_height:
        pol = tab_widget.sizePolicy()
        pol.setVerticalPolicy(QSizePolicy.Maximum)
        tab_widget.setSizePolicy(pol)

    def _apply(index):
        for i in range(tab_widget.count()):
            page = tab_widget.widget(i)
            if page is None:
                continue
            policy = page.sizePolicy()
            policy.setVerticalPolicy(
                QSizePolicy.Preferred if i == index else QSizePolicy.Ignored
            )
            page.setSizePolicy(policy)
        tab_widget.updateGeometry()
        if cap_height:
            page = tab_widget.widget(index)
            if page is not None:
                # When a page is a QScrollArea (some pages wrap each tab in
                # one as a small-window safety net), its own sizeHint is a
                # generic default, not the content height - so measure the
                # widget *inside* it instead. Otherwise the cap can't hug a
                # short tab and leaves a big block of dead space below it.
                measure = page
                extra = 0
                if isinstance(page, QScrollArea) and page.widget() is not None:
                    measure = page.widget()
                    extra = 2 * page.frameWidth()
                hint = measure.sizeHint().height()
                if hint > 0:
                    bar = tab_widget.tabBar().sizeHint().height()
                    # Small margin so word-wrapped hints aren't clipped.
                    tab_widget.setMaximumHeight(hint + bar + extra + 24)

    tab_widget.currentChanged.connect(_apply)
    _apply(tab_widget.currentIndex())
