"""Agent hosts no longer auto-register a shadow "Agent + SSH" connection.

An agent host is fully manageable over the agent's own outbound channel — the web
terminal streams a local PTY over it with no inbound SSH — so the auto-created SSH
record was a redundant second inventory entry per host. It is OFF by default now
(we are phasing SSH connections out) and restored only by SYSIBLE_AGENT_SSH_TERMINAL=1.
"""
import backend.app as app_module
from backend.remote_routes import get_agent_ssh_state, load_hosts


def test_agent_ssh_shadow_off_by_default(monkeypatch):
    monkeypatch.delenv("SYSIBLE_AGENT_SSH_TERMINAL", raising=False)
    app_module._maybe_enroll_agent_ssh("h-shadow-off", "web1", "10.0.0.11", "", force=True)
    # No SSH-enable task queued, no per-host SSH state, no shadow host record.
    assert get_agent_ssh_state("h-shadow-off") is None
    assert "web1" not in load_hosts()


def test_agent_ssh_shadow_opt_in(monkeypatch):
    monkeypatch.setenv("SYSIBLE_AGENT_SSH_TERMINAL", "1")
    # Isolate from the host toolchain: the SSH-enable command needs the controller
    # keypair (ssh-keygen), absent in CI. Stub the key + queue so we assert the
    # opt-in re-enables the flow, not the key generation itself.
    monkeypatch.setattr(app_module, "_ensure_controller_key", lambda: "ssh-ed25519 AAAA test")
    monkeypatch.setattr(app_module, "queue_task", lambda *a, **k: 4242)
    app_module._maybe_enroll_agent_ssh("h-shadow-on", "web2", "10.0.0.12", "", force=True)
    st = get_agent_ssh_state("h-shadow-on")
    assert st and st.get("status") == "pending" and st.get("task_id") == 4242
