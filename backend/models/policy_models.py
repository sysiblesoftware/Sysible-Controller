
from pydantic import BaseModel


class PasswordPolicyFields(BaseModel):
    """Same shape/convention as client/api.py's PASSWORD_POLICY_PRESETS -
    minlen/retry are plain counts, credit fields follow pwquality's
    negative-means-required convention (e.g. ucredit=-1 means "at
    least one uppercase letter required")."""
    minlen: int = 12
    retry: int = 3
    dcredit: int = -1
    ucredit: int = -1
    lcredit: int = -1
    ocredit: int = -1


class LockoutPolicyFields(BaseModel):
    deny: int = 5
    unlock_time: int = 900


class SudoPolicyFields(BaseModel):
    # Minutes a sudo timestamp stays valid before re-prompting for a
    # password (sudoers Defaults timestamp_timeout).
    timestamp_timeout: int = 15
    # False writes a NOPASSWD entry for the sudo/wheel group instead -
    # off by default since silently allowing passwordless root is not
    # a sane out-of-the-box default.
    require_password: bool = True


class SetEnvironmentalPolicyRequest(BaseModel):
    password: PasswordPolicyFields = PasswordPolicyFields()
    lockout: LockoutPolicyFields = LockoutPolicyFields()
    sudo: SudoPolicyFields = SudoPolicyFields()
    umask: str = "027"


class SetAdminPasswordPolicyRequest(BaseModel):
    minlen: int = 12
    dcredit: int = -1
    ucredit: int = -1
    lcredit: int = -1
    ocredit: int = -1
