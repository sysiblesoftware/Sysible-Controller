from typing import Optional

from pydantic import BaseModel


class CreateUserRequest(BaseModel):
    username: str
    shell: str = "/bin/bash"
    password: Optional[str] = None


class SetPasswordRequest(BaseModel):
    password: str
