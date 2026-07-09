import re
from typing import Optional

from pydantic import BaseModel, field_validator

# A host record's `user`/`username` and `ip` flow straight into the SSH command
# line (`user@ip` as the ssh destination). A value starting with '-' (or carrying
# shell/option metacharacters) would be parsed by ssh as an option — e.g.
# `-oProxyCommand=...` runs code on THIS controller as root. Validate at ingest so
# such a record can never be stored. `name` is a host key used in URLs/paths.
_SSH_LOGIN_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")      # no leading '-'
_HOST_ADDR_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._:-]*)?$")   # IPv4/IPv6/hostname chars, no leading '-'
_HOST_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _validate(pattern, value, field):
    v = (value or "").strip()
    if not pattern.match(v):
        raise ValueError(f"invalid {field}: must match {pattern.pattern}")
    return v


class AddHostRequest(BaseModel):
    name: str
    ip: str
    user: str = "root"
    environment: str = ""

    @field_validator("name")
    @classmethod
    def _v_name(cls, v): return _validate(_HOST_NAME_RE, v, "name")

    @field_validator("ip")
    @classmethod
    def _v_ip(cls, v): return _validate(_HOST_ADDR_RE, v, "ip/host")

    @field_validator("user")
    @classmethod
    def _v_user(cls, v): return _validate(_SSH_LOGIN_RE, v, "user")


class ExecRequest(BaseModel):
    cmd: str
    description: Optional[str] = None  # human label for the activity log
    log: bool = True  # False for background/internal reads (e.g. user-list sync)
    become_password: Optional[str] = None  # admin's sudo password for password-sudo hosts (RAM only)


class EnrollSSHRequest(BaseModel):
    name: str
    ip: str
    username: str = "root"
    password: str
    environment: str = ""

    @field_validator("name")
    @classmethod
    def _v_name(cls, v): return _validate(_HOST_NAME_RE, v, "name")

    @field_validator("ip")
    @classmethod
    def _v_ip(cls, v): return _validate(_HOST_ADDR_RE, v, "ip/host")

    @field_validator("username")
    @classmethod
    def _v_username(cls, v): return _validate(_SSH_LOGIN_RE, v, "username")


class TerminalWriteRequest(BaseModel):
    data: str


class TerminalResizeRequest(BaseModel):
    cols: int
    rows: int
