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
    actor: str = ""  # username of the administrator performing the add, for the audit log


class ForcePasswordChangeRequest(BaseModel):
    username: str
    current_password: str
    new_password: str
