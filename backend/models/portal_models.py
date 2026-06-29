from pydantic import BaseModel


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


class AddAdministratorRequest(BaseModel):
    username: str
    password: str
    role: str = "sysadmin"  # 'superuser' or 'sysadmin'
    actor: str = ""  # username of the administrator performing the add, for the audit log


class AdminSetupRequest(BaseModel):
    # First-run creation of the very first administrator (no default
    # account exists). Only honoured while the administrators table is
    # still empty - see POST /admin/setup.
    username: str
    password: str


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
