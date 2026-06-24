"""
Shared collapse/expand behavior for the environment-grouped host
checklists used across System Administration (Service Management,
Cron & Systemd Timers, System Health & Logs, User & Group
Administration, Host Enrollment). Every one of those pages builds its
QListWidget the same way: a bold, non-checkable header row per
environment followed by that environment's host rows, rebuilt from
scratch on every refresh. Re-solving "click a header to hide/show its
rows, and remember that across a refresh" five separate times would
just be five more copies of the same bug to fix later - this module
gives all of them the same behavior from one place.

Usage in a page that builds its list via a per-environment header
helper (e.g. `_add_host_header`):

    from client.collapsible_groups import (
        make_group_header_item, apply_collapse_state,
        get_collapsed_groups, connect_group_toggle,
        add_collapse_expand_buttons,
    )

    # once, in __init__, after creating the list widget:
    connect_group_toggle(self.host_list)
    hosts_header.addWidget(...)  # wire up the two buttons this returns

    # in _add_host_header(self, text):
    item = make_group_header_item(text, collapsed=text in self._collapsed_envs)
    self.host_list.addItem(item)

    # in load_hosts(), right before clearing the list:
    self._collapsed_envs = get_collapsed_groups(self.host_list)
    ...
    # right after all headers/rows have been (re)added:
    apply_collapse_state(self.host_list)
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QListWidgetItem, QPushButton

# Custom data roles, offset well clear of Qt.UserRole (which the host
# pages already use to stash the host/agent dict on non-header rows).
COLLAPSE_STATE_ROLE = Qt.UserRole + 100
HEADER_LABEL_ROLE = Qt.UserRole + 101

_COLLAPSED_ARROW = "▶"  # ▶
_EXPANDED_ARROW = "▼"  # ▼


def _arrow(collapsed):
    return _COLLAPSED_ARROW if collapsed else _EXPANDED_ARROW


def make_group_header_item(env_label, collapsed=False):
    """Build the bold, non-checkable header row for one environment
    group. Carries its own collapsed/expanded state and an arrow
    prefix so the state is visible without anything else changing."""
    item = QListWidgetItem(f"{_arrow(collapsed)} {env_label.upper()}")
    # Enabled (so it still receives clicks) but not selectable and not
    # checkable - it's a section divider, not a host row.
    item.setFlags(Qt.ItemIsEnabled)

    font = item.font()
    font.setBold(True)
    item.setFont(font)

    item.setData(COLLAPSE_STATE_ROLE, collapsed)
    item.setData(HEADER_LABEL_ROLE, env_label)
    return item


def is_group_header(item):
    return item is not None and item.data(HEADER_LABEL_ROLE) is not None


def get_collapsed_groups(list_widget):
    """Snapshot of which environment names are currently collapsed.
    Call this before clear()-ing and rebuilding the list on a refresh,
    so the rebuilt headers can be told to restore the same state -
    otherwise every refresh would silently re-expand everything."""
    collapsed = set()
    for i in range(list_widget.count()):
        item = list_widget.item(i)
        if is_group_header(item) and item.data(COLLAPSE_STATE_ROLE):
            collapsed.add(item.data(HEADER_LABEL_ROLE))
    return collapsed


def apply_collapse_state(list_widget):
    """Hide/show each host row based on its group's header state. Call
    this once after (re)building the full list of headers + rows."""
    current_collapsed = False
    for i in range(list_widget.count()):
        item = list_widget.item(i)
        if is_group_header(item):
            current_collapsed = bool(item.data(COLLAPSE_STATE_ROLE))
            continue
        item.setHidden(current_collapsed)


def toggle_group(list_widget, header_item):
    """Flip one environment header between collapsed/expanded and
    hide/show the rows that belong to it."""
    collapsed = not header_item.data(COLLAPSE_STATE_ROLE)
    header_item.setData(COLLAPSE_STATE_ROLE, collapsed)
    label = header_item.data(HEADER_LABEL_ROLE)
    header_item.setText(f"{_arrow(collapsed)} {label.upper()}")
    apply_collapse_state(list_widget)


def set_all_groups_collapsed(list_widget, collapsed):
    """Used by the page-level "Collapse All" / "Expand All" buttons."""
    for i in range(list_widget.count()):
        item = list_widget.item(i)
        if is_group_header(item):
            item.setData(COLLAPSE_STATE_ROLE, collapsed)
            item.setText(f"{_arrow(collapsed)} {item.data(HEADER_LABEL_ROLE).upper()}")
    apply_collapse_state(list_widget)


def connect_group_toggle(list_widget):
    """Wire up click-to-toggle on header rows. Call once per list
    widget, in the page's __init__."""

    def _handle(item):
        if is_group_header(item):
            toggle_group(list_widget, item)

    list_widget.itemClicked.connect(_handle)


def add_collapse_expand_buttons(list_widget):
    """Builds a small 'Collapse All' / 'Expand All' button pair wired
    up to the given list widget. Returns the two buttons so the caller
    can drop them into whichever header row layout fits the page -
    every affected page already has a QHBoxLayout above its list for
    Refresh/Select All/Deselect All, this is meant to join that row."""
    collapse_btn = QPushButton("Collapse All")
    expand_btn = QPushButton("Expand All")
    collapse_btn.clicked.connect(lambda: set_all_groups_collapsed(list_widget, True))
    expand_btn.clicked.connect(lambda: set_all_groups_collapsed(list_widget, False))
    return collapse_btn, expand_btn
