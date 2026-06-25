from typing import Optional

from pydantic import BaseModel


class AddHostRequest(BaseModel):
    name: str
    ip: str
    user: str = "root"
    environment: str = ""


class ExecRequest(BaseModel):
    cmd: str
    description: Optional[str] = None  # human label for the activity log
    log: bool = True  # False for background/internal reads (e.g. user-list sync)


class EnrollSSHRequest(BaseModel):
    name: str
    ip: str
    username: str = "root"
    password: str
    environment: str = ""


class TerminalWriteRequest(BaseModel):
    data: str


class TerminalResizeRequest(BaseModel):
    cols: int
    rows: int
