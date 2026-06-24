from typing import Optional

from pydantic import BaseModel


class EnrollRequest(BaseModel):
    token: str
    host_id: str
    hostname: Optional[str] = None
    platform: Optional[str] = None
    kernel: Optional[str] = None
    ip: Optional[str] = None


class HeartbeatRequest(BaseModel):
    host_id: str
    agent_secret: str
    ip: Optional[str] = None
    hostname: Optional[str] = None


class SelfDisenrollRequest(BaseModel):
    """Body for POST /agents/{host_id}/disenroll - the agent-authenticated
    counterpart to the admin-only DELETE /agents/{host_id}. Lets the
    disenroll_agent.sh script (in the agent bundle) remove its own
    enrollment using the same host_id+agent_secret it already has on
    disk, instead of needing the controller's API key."""
    host_id: str
    agent_secret: str


class TaskCreateRequest(BaseModel):
    command: str
    kind: str = "command"
    description: Optional[str] = None  # human label for the activity log


class TaskResultRequest(BaseModel):
    host_id: str
    agent_secret: str
    task_id: int
    result: str
