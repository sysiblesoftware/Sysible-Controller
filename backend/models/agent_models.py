import json
import os
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, field_validator

# Size caps for the agent-facing channel. A managed host runs the agent, and a
# compromised/malicious agent could otherwise POST arbitrarily large payloads
# every heartbeat (metrics/measurements dicts, task results, PTY output)
# to bloat the DB / the integrity state file or drive controller RSS to OOM.
# These bound each field; string fields use Pydantic's max_length, dict fields a
# validator on the serialized size. Generous enough for real payloads, small
# enough to make flooding ineffective. The large ones are env-tunable.
_ID_MAX = 128
_HOSTNAME_MAX = 256
_SHORT_MAX = 512


def _int_env(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


import re as _re
# An agent-reported `ip` becomes the SSH target of the auto-registered SSH mirror
# host. It's already placed after `--` in the ssh argv (so it can't be read as an
# option) and stored via parameterized SQL, but validate at ingest as defence in
# depth so an option-looking value (`-oProxyCommand=…`) can never be persisted.
# Lenient: any real IPv4/IPv6/hostname passes; only a leading '-' or shell/space/
# control character is rejected. Empty/None is allowed (older agents omit it).
_AGENT_ADDR_RE = _re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._:-]*)?$")


def _validate_agent_addr(cls, v):
    if v is None or v == "":
        return v
    if not _AGENT_ADDR_RE.match(v):
        raise ValueError("ip must be a plain host address (no leading '-' or metacharacters)")
    return v


def _validate_agent_hostname(cls, v):
    # The agent-reported hostname is written into the activity feed and used to
    # reconcile SSH-mirror records. Reject CONTROL characters (newline/CR/NUL, the
    # 0x1e record separator, other C0/C1) so a hostile/compromised agent can't inject
    # a line into anything that later logs or renders it. Unicode letters/emoji are
    # legitimate hostnames and remain allowed. Empty/None passes (older agents omit).
    if v is None or v == "":
        return v
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in v):
        raise ValueError("hostname must not contain control characters")
    return v


_METRICS_MAX = _int_env("SYSIBLE_MAX_METRICS_BYTES", 64 * 1024)
_MEASUREMENTS_MAX = _int_env("SYSIBLE_MAX_MEASUREMENTS_BYTES", 512 * 1024)
_COMMAND_MAX = _int_env("SYSIBLE_MAX_COMMAND_BYTES", 1024 * 1024)
_RESULT_MAX = _int_env("SYSIBLE_MAX_RESULT_BYTES", 4 * 1024 * 1024)
_PTY_DATA_MAX = _int_env("SYSIBLE_MAX_PTY_CHUNK_BYTES", 512 * 1024)


def _bounded_dict(v, cap, name):
    """Reject a dict whose serialized size exceeds `cap` bytes (None passes)."""
    if v is None:
        return v
    try:
        size = len(json.dumps(v))
    except (TypeError, ValueError):
        raise ValueError(f"{name} is not JSON-serializable")
    if size > cap:
        raise ValueError(f"{name} too large ({size} > {cap} bytes)")
    return v


class EnrollRequest(BaseModel):
    token: str = Field(max_length=_SHORT_MAX)
    host_id: str = Field(max_length=_ID_MAX)
    hostname: Optional[str] = Field(default=None, max_length=_HOSTNAME_MAX)
    platform: Optional[str] = Field(default=None, max_length=_SHORT_MAX)
    kernel: Optional[str] = Field(default=None, max_length=_SHORT_MAX)
    ip: Optional[str] = Field(default=None, max_length=_HOSTNAME_MAX)
    # Proof of possession of the EXISTING identity, for a re-enroll that lands on an
    # already-registered host_id. An agent that still holds its saved agent_secret
    # presents it here so the controller can confirm the caller IS the incumbent host
    # before overwriting its credential — closing the offline-host takeover where a
    # bearer-token holder re-binds a host they don't control. A brand-new host, or a
    # reinstall that legitimately lost its secret, omits it and instead uses an
    # admin-issued reissue token.
    prev_agent_secret: Optional[str] = Field(default=None, max_length=_ID_MAX)

    _v_ip = field_validator("ip")(classmethod(_validate_agent_addr))
    _v_hostname = field_validator("hostname")(classmethod(_validate_agent_hostname))


class HeartbeatRequest(BaseModel):
    host_id: str = Field(max_length=_ID_MAX)
    agent_secret: str = Field(max_length=_ID_MAX)
    ip: Optional[str] = Field(default=None, max_length=_HOSTNAME_MAX)
    hostname: Optional[str] = Field(default=None, max_length=_HOSTNAME_MAX)

    _v_ip = field_validator("ip")(classmethod(_validate_agent_addr))
    _v_hostname = field_validator("hostname")(classmethod(_validate_agent_hostname))
    # Short hash of the agent's own agent.py, so the controller knows which hosts
    # run the current agent (drives the web console's Update-agents progress).
    # Older agents omit it.
    agent_version: Optional[str] = Field(default=None, max_length=_SHORT_MAX)
    # Optional fleet-health sample (disk/mem/load + failed-units/systemd/OOM,
    # hypervisor role). Sent by newer agents at most once per
    # SYSIBLE_METRICS_INTERVAL, not on every heartbeat; older agents omit it (or
    # send only load1/cores/mem/disk). Extra keys are ignored. See
    # host_agent/agent.py's _collect_metrics().
    metrics: Optional[Dict[str, Any]] = None
    # Agent integrity (Tier 1): the agent's self-measurement manifest (sha256 of
    # its own files + version). Optional so older agents that don't send it keep
    # working; when present the controller compares it to the host's sealed
    # baseline and quarantines on mismatch. See backend/agent_integrity.py.
    measurements: Optional[dict] = None

    @field_validator("metrics")
    @classmethod
    def _cap_metrics(cls, v):
        return _bounded_dict(v, _METRICS_MAX, "metrics")

    @field_validator("measurements")
    @classmethod
    def _cap_measurements(cls, v):
        return _bounded_dict(v, _MEASUREMENTS_MAX, "measurements")


class SelfDisenrollRequest(BaseModel):
    """Body for POST /agents/{host_id}/disenroll - the agent-authenticated
    counterpart to the admin-only DELETE /agents/{host_id}. Lets the
    disenroll_agent.sh script (in the agent bundle) remove its own
    enrollment using the same host_id+agent_secret it already has on
    disk, instead of needing the controller's API key."""
    host_id: str = Field(max_length=_ID_MAX)
    agent_secret: str = Field(max_length=_ID_MAX)


class TaskCreateRequest(BaseModel):
    command: str = Field(max_length=_COMMAND_MAX)
    kind: str = Field(default="command", max_length=_SHORT_MAX)
    description: Optional[str] = Field(default=None, max_length=4096)  # human label for the activity log
    become_password: Optional[str] = Field(default=None, max_length=_SHORT_MAX)  # RAM only, never persisted
    # When False, skip the per-host activity-feed entry. The web console sets
    # this for multi-host tool runs so it can log ONE grouped summary entry
    # instead of N near-identical rows ("List disks · dev1", "· prod1", ...).
    log: bool = True


class ActivityLogRequest(BaseModel):
    """One attributed activity-feed entry, written by the web console to record a
    single grouped summary for a multi-host tool run."""
    host: str = Field(default="", max_length=_HOSTNAME_MAX)
    description: str = Field(max_length=4096)


class TaskResultRequest(BaseModel):
    host_id: str = Field(max_length=_ID_MAX)
    agent_secret: str = Field(max_length=_ID_MAX)
    task_id: int
    result: str = Field(max_length=_RESULT_MAX)


class PtyOutputRequest(BaseModel):
    """Agent -> controller: a chunk of shell output for an agent-hosted terminal
    (Option B). `ended` marks the shell exiting."""
    agent_secret: str = Field(max_length=_ID_MAX)
    data: str = Field(default="", max_length=_PTY_DATA_MAX)
    ended: bool = False
