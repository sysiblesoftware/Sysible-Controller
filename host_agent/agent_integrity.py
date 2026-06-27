"""
Agent self-measurement (Tier-1 integrity, PROTOTYPE / RFC).

Produces a manifest of the agent's own on-disk files (sha256 + version) that
the agent attaches to its heartbeat. The CONTROLLER compares this against the
baseline it sealed (see backend/agent_integrity.py) and quarantines the host on
a mismatch.

HONEST LIMIT (Tier 1): this defends against accidental corruption, config
drift, version skew, swapped files, and tampering by a NON-root actor. An
attacker with root on the host can edit the agent AND this measurer to report a
clean hash - so this is a speed bump, not a guarantee. The robust defence
against host-root tampering is TPM remote attestation (Tier 2), out of scope
here. Crucially this is only meaningful ON TOP OF the privilege dispatcher: once
the agent runs as the locked 'sysible' user and cannot escalate except through
vetted verbs, it can no longer rewrite its own code, so a mismatch means
something with real root touched the files.
"""
import hashlib
import os

try:
    # version.py at the repo/install root, if present
    from version import __version__ as AGENT_VERSION  # type: ignore
except Exception:
    AGENT_VERSION = os.environ.get("SYSIBLE_AGENT_VERSION", "unknown")


def _sha256(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except FileNotFoundError:
        return "MISSING"
    except PermissionError:
        return "UNREADABLE"
    except OSError:
        return "ERROR"


def default_files():
    """The agent's own trust-relevant files. The dispatcher path comes from
    SYSIBLE_PRIV when set; otherwise we look beside this module."""
    here = os.path.dirname(os.path.abspath(__file__))
    files = [
        os.path.join(here, "agent.py"),
        os.path.join(here, "agent_integrity.py"),
    ]
    disp = os.environ.get("SYSIBLE_PRIV")
    if disp:
        files.append(disp)
    else:
        local = os.path.join(here, "sysible_priv.py")
        if os.path.exists(local):
            files.append(local)
    return files


def measure(files=None, version=None):
    """Return {'version': str, 'files': {abspath: sha256|MARKER}}. Pure; never
    raises (so a measurement problem can't break the heartbeat)."""
    try:
        files = files if files is not None else default_files()
        return {
            "version": version or AGENT_VERSION,
            "files": {p: _sha256(p) for p in files},
        }
    except Exception as e:  # pragma: no cover - measurement must never throw
        return {"version": version or AGENT_VERSION, "files": {}, "error": str(e)}
