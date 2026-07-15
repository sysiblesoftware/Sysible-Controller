"""Shared input validators for the dual-host command builders.

These small validators were copy-pasted, byte-for-byte, across several _api_*
modules (_validate_int_range in four, _validate_nonempty_line and
_validate_user_or_group in two each). They live here now so a rule change happens
in one place instead of drifting between copies. Imports nothing from client/, so
any _api_* module can import it without circular-import risk.
"""
import re

_SAFE_USERGROUP_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


def validate_int_range(value, lo: int, hi: int, label: str) -> int:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a whole number.")
    if not (lo <= n <= hi):
        raise ValueError(f"{label} must be between {lo} and {hi}.")
    return n


def validate_nonempty_line(value: str, label: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{label} is required.")
    if "\n" in value or "\r" in value:
        raise ValueError(f"{label} cannot span multiple lines.")
    return value


def validate_user_or_group(name: str, label: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError(f"{label} is required.")
    if not _SAFE_USERGROUP_RE.match(name):
        raise ValueError(f"{label} doesn't look like a valid Linux user/group name.")
    return name
