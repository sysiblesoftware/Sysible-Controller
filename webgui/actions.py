"""
The action registry: the single bridge between the React UI and the
desktop client's cmd_* shell-command builders.

Each Action ties together:
  * name    - stable id used in the URL (/api/tool/<name>) and by the SPA
  * tool    - which dashboard tool/tile it belongs to (groups the UI)
  * label   - human label for the button/form
  * kind    - dispatch kind passed through to run_on_entry (mostly
              "command"; some result-heavy reads use a distinct kind so
              the controller can cache/route them, matching the desktop)
  * params  - ordered list of Param (name/label/type/default/required)
              the SPA renders a form from, and the server validates
  * build   - callable(params: dict) -> str : returns the exact shell
              string by delegating to the matching cmd_* builder

To extend toward full desktop parity you ADD Action entries here that
point at cmd_* functions that already exist in client/_api_*.py. You do
not write any new dispatch or shell logic - that already exists and is
shared with the desktop app, so the two stay in lockstep.

This file intentionally seeds only a representative slice across three
tools (fleet run-command, service management, user & group). It proves
the pattern end-to-end; the remaining tiles are the same shape.
"""
from dataclasses import dataclass, field
from typing import Callable

from client import api  # cmd_* builders are re-exported on client.api


@dataclass
class Param:
    name: str
    label: str
    type: str = "text"          # text | password | number | select | checkbox
    default: object = ""
    required: bool = True
    options: list = field(default_factory=list)   # for type == "select"
    help: str = ""


@dataclass
class Action:
    name: str
    tool: str
    label: str
    build: Callable[[dict], str]
    kind: str = "command"
    params: list = field(default_factory=list)
    description: str = ""
    danger: bool = False        # UI confirms before running (delete, etc.)


# ----------------------------------------------------------------------
# Small helpers so build= callables stay one-liners and coerce types the
# HTML form hands us (everything arrives as strings/None).
# ----------------------------------------------------------------------
def _s(params, key, default=""):
    v = params.get(key, default)
    return default if v is None else str(v)


def _i(params, key, default=0):
    v = params.get(key, default)
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _b(params, key, default=False):
    v = params.get(key, default)
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("1", "true", "yes", "on")


# ----------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------
_ACTIONS: dict[str, Action] = {}


def _register(action: Action):
    _ACTIONS[action.name] = action


# ---- Tool: Run Command (the fleet primitive) -------------------------
_register(Action(
    name="run_command",
    tool="Run Command",
    label="Run shell command",
    description="Run an arbitrary shell command on every selected host.",
    params=[Param("command", "Command", help="e.g. uname -a")],
    build=lambda p: _s(p, "command"),
))

# ---- Tool: Service Management ----------------------------------------
_register(Action(
    name="svc_list",
    tool="Service Management",
    label="List services",
    description="List all systemd services and their state.",
    params=[],
    build=lambda p: api.cmd_list_services(),
))
_register(Action(
    name="svc_list_running",
    tool="Service Management",
    label="List running services",
    params=[],
    build=lambda p: api.cmd_list_running_services(),
))
_register(Action(
    name="svc_status",
    tool="Service Management",
    label="Service status",
    params=[Param("name", "Service name", help="e.g. sshd")],
    build=lambda p: api.cmd_service_status(_s(p, "name")),
))
_register(Action(
    name="svc_start",
    tool="Service Management",
    label="Start service",
    params=[Param("name", "Service name")],
    build=lambda p: api.cmd_service_start(_s(p, "name")),
))
_register(Action(
    name="svc_stop",
    tool="Service Management",
    label="Stop service",
    danger=True,
    params=[Param("name", "Service name")],
    build=lambda p: api.cmd_service_stop(_s(p, "name")),
))
_register(Action(
    name="svc_restart",
    tool="Service Management",
    label="Restart service",
    params=[Param("name", "Service name")],
    build=lambda p: api.cmd_service_restart(_s(p, "name")),
))

# ---- Tool: User & Group Administration -------------------------------
_register(Action(
    name="user_create",
    tool="User & Group Administration",
    label="Create user",
    params=[
        Param("username", "Username"),
        Param("password", "Password", type="password", required=False),
        Param("shell", "Shell", default="/bin/bash", required=False),
    ],
    build=lambda p: api.cmd_create_user(_s(p, "username"), _s(p, "password"),
                                        _s(p, "shell", "/bin/bash") or "/bin/bash"),
))
_register(Action(
    name="user_delete",
    tool="User & Group Administration",
    label="Delete user",
    danger=True,
    params=[Param("username", "Username")],
    build=lambda p: api.cmd_delete_user(_s(p, "username")),
))
_register(Action(
    name="user_lock",
    tool="User & Group Administration",
    label="Lock user",
    params=[Param("username", "Username")],
    build=lambda p: api.cmd_lock_user(_s(p, "username")),
))
_register(Action(
    name="user_unlock",
    tool="User & Group Administration",
    label="Unlock user",
    params=[Param("username", "Username")],
    build=lambda p: api.cmd_unlock_user(_s(p, "username")),
))
_register(Action(
    name="user_set_shell",
    tool="User & Group Administration",
    label="Set shell",
    params=[Param("username", "Username"), Param("shell", "Shell", default="/bin/bash")],
    build=lambda p: api.cmd_set_user_shell(_s(p, "username"), _s(p, "shell", "/bin/bash")),
))
_register(Action(
    name="group_members",
    tool="User & Group Administration",
    label="List groups & members",
    params=[],
    build=lambda p: api.cmd_list_groups_with_members(),
))


# ----------------------------------------------------------------------
# Public API used by server.py
# ----------------------------------------------------------------------
def get(name: str):
    return _ACTIONS.get(name)


def catalog():
    """Group actions by tool for the SPA, serializing Param to plain
    dicts. The build= callable is intentionally not serialized."""
    by_tool: dict[str, list] = {}
    for a in _ACTIONS.values():
        by_tool.setdefault(a.tool, []).append({
            "name": a.name,
            "label": a.label,
            "description": a.description,
            "danger": a.danger,
            "params": [
                {
                    "name": pr.name, "label": pr.label, "type": pr.type,
                    "default": pr.default, "required": pr.required,
                    "options": pr.options, "help": pr.help,
                }
                for pr in a.params
            ],
        })
    return [{"tool": tool, "actions": acts} for tool, acts in by_tool.items()]
