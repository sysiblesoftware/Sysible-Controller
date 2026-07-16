import re

from pydantic import BaseModel, field_validator

# Admin console usernames flow into OS-user contexts on managed hosts (runuser -u /
# sudo -u <name>) and into shell status messages, so they must be a strict, shell-safe
# charset — never arbitrary text. This blocks every shell metacharacter ($ ` ' " ; | & <
# > ( ) space newline …) at ingest, so a crafted username can never reach a command
# builder. Letters/digits plus . _ @ - (no leading '-'), 1–64 chars.
_ADMIN_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._@-]*$")


def _validate_admin_name(v: str) -> str:
    s = (v or "").strip()
    if not s or len(s) > 64 or not _ADMIN_NAME_RE.match(s):
        raise ValueError(
            "username must be 1-64 characters using letters, digits, and . _ @ - "
            "(no leading '-', no spaces or shell metacharacters)")
    return s


class SetControllerConfigRequest(BaseModel):
    hostname: str = ""
    ip: str = ""
    address_mode: str = "hostname"  # "hostname", "ip", or "all" (every detected local IP, with failover) - which agent bundles use
    port: int = 9000


class SetLicenseKeyRequest(BaseModel):
    license_key: str = ""


class SetPortalCredentialsRequest(BaseModel):
    username: str
    password: str
    # Required (and checked server-side) whenever credentials already
    # exist - only optional for the very first time they're set, when
    # there's nothing yet to confirm against.
    current_password: str = ""


class RemovePortalCredentialsRequest(BaseModel):
    # Required - removing the login outright is at least as sensitive
    # as resetting it, so it gets the same current-password check
    # SetPortalCredentialsRequest enforces once credentials exist.
    current_password: str


class SetPortalPortRequest(BaseModel):
    port: int


class AdminLoginRequest(BaseModel):
    username: str
    password: str


class ChangeAdminCredentialsRequest(BaseModel):
    username: str
    current_password: str
    new_username: str
    new_password: str

    @field_validator("new_username")
    @classmethod
    def _v_new_username(cls, v):
        # Empty = "keep the current username" (the route falls back to `username`);
        # any non-empty rename target must pass the strict charset check.
        return "" if not (v or "").strip() else _validate_admin_name(v)


class AddAdministratorRequest(BaseModel):
    username: str
    password: str
    role: str = "sysadmin"  # 'superuser' or 'sysadmin'
    actor: str = ""  # username of the administrator performing the add, for the audit log

    @field_validator("username")
    @classmethod
    def _v_username(cls, v):
        return _validate_admin_name(v)


class AdminSetupRequest(BaseModel):
    # First-run creation of the very first administrator (no default
    # account exists). Only honoured while the administrators table is
    # still empty - see POST /admin/setup.
    username: str
    password: str

    @field_validator("username")
    @classmethod
    def _v_username(cls, v):
        return _validate_admin_name(v)


class ForcePasswordChangeRequest(BaseModel):
    username: str
    current_password: str
    new_password: str


class ResetAdministratorPasswordRequest(BaseModel):
    # Superuser-initiated reset of ANOTHER administrator's password.
    # No current password required - the caller is a superuser. The target
    # is required to change it on their next login.
    new_password: str
    actor: str = ""  # username of the superuser performing the reset, for the audit log


class SetSudoConnectRequest(BaseModel):
    # Superuser-initiated grant/revoke of an administrator's access to the
    # Sysible Connect terminal's "Send sudo password" button.
    allowed: bool
    actor: str = ""  # username of the superuser making the change, for the audit log


class SetRoleRequest(BaseModel):
    # Superuser-initiated promote/demote of an administrator's role.
    role: str        # 'superuser' | 'sysadmin' | 'auditor'
    actor: str = ""  # username of the superuser making the change, for the audit log
