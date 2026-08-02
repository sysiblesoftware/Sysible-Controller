"""
Agent version scheme: a freshly ENROLLED agent must report the SAME version the
controller expects, even though the enrollment bundle bakes the resolved
controller URL into agent.py's os.getenv("SYSIBLE_CONTROLLER", ...) default.

Regression for the bug where the controller hashed the raw (unpatched) source
while every enrolled agent hashed its patched file — so the whole fleet showed
as permanently "outdated" and "Update agents" never looked like it worked.
"""
import hashlib
import re

from backend.agent_bundle import (
    AGENT_SOURCE_FILE, agent_version_of, _patch_agent_controller_default,
)


def _agent_side_version(src: str) -> str:
    """Replicates host_agent/agent.py's AGENT_VERSION computation exactly, so the
    test fails if the agent and controller ever normalize differently."""
    s = re.sub(r'os\.getenv\("SYSIBLE_CONTROLLER",\s*"[^"]*"\)',
               lambda _m: 'os.getenv("SYSIBLE_CONTROLLER", "https://127.0.0.1:9000")', src)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


def test_baked_controller_url_does_not_change_version():
    raw = AGENT_SOURCE_FILE.read_text(encoding="utf-8")
    patched = _patch_agent_controller_default(raw, "https://10.1.2.3:9000,https://vip:9000")
    assert patched != raw, "patch should change the file"
    assert agent_version_of(raw) == agent_version_of(patched), \
        "baked controller URL must be normalized out of the version hash"


def test_enrolled_agent_reports_the_controllers_current_version():
    raw = AGENT_SOURCE_FILE.read_text(encoding="utf-8")
    patched = _patch_agent_controller_default(raw, "https://10.1.2.3:9000")
    # After enrollment the agent runs the patched file; it must still read as
    # up-to-date against the controller's current (raw) version.
    assert _agent_side_version(patched) == agent_version_of(raw)
    assert _agent_side_version(raw) == agent_version_of(raw)


def test_agent_source_still_normalizes_before_hashing():
    # Guard: if someone reverts host_agent/agent.py to hashing raw bytes, enrolled
    # agents silently look outdated again. Keep the normalization in place.
    src = AGENT_SOURCE_FILE.read_text(encoding="utf-8")
    assert "_av_src" in src and "SYSIBLE_CONTROLLER" in src
