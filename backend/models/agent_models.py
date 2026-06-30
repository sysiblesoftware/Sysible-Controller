from typing import Any, Dict, Optional

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
    # Short hash of the agent's own agent.py, so the controller knows which hosts
    # run the current agent (drives the web console's Update-agents progress).
    # Older agents omit it.
    agent_version: Optional[str] = None
    # Optional performance sample (load/cpu/mem/swap/disk/net/io/procs). Sent by
    # newer agents at most once per SYSIBLE_METRICS_INTERVAL, not on every
    # heartbeat; older agents omit it (or send only load1/cores/mem/disk). See
    # host_agent/agent.py's _sample_metrics().
    metrics: Optional[Dict[str, Any]] = None
    # Optional rich detail snapshot (per-core CPU, memory breakdown, per-interface
    # network, per-mount disk, top processes) for the per-host drill-down. Latest
    # only - overwritten each interval. See host_agent/agent.py's _sample_snapshot().
    snapshot: Optional[Dict[str, Any]] = None


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
    become_password: Optional[str] = None  # held in RAM only, never persisted


class TaskResultRequest(BaseModel):
    host_id: str
    agent_secret: str
    task_id: int
    result: str
