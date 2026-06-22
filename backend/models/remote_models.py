from pydantic import BaseModel


class AddHostRequest(BaseModel):
    name: str
    ip: str
    user: str = "root"
    environment: str = ""


class ExecRequest(BaseModel):
    cmd: str


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
