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
from PySide6.QtWidgets import QSizePolicy


def shrink_tabwidget_to_current_page(tab_widget):
    """Makes `tab_widget` size itself to its currently visible page
    instead of the tallest page in the stack. Safe to call once, right
    after all of a QTabWidget's tabs have been added - it wires up
    `currentChanged` itself, so later tab switches keep tracking the
    new current page automatically."""

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

    tab_widget.currentChanged.connect(_apply)
    _apply(tab_widget.currentIndex())
