"""Regression guards for the exec_remote authorization fix (pentest HIGH:
'Arbitrary remote command execution on any enrolled host with only the machine
API key'). The tokenless=root branch must NOT be reachable by an API-key holder
coming in off the network; exec requires a resolvable OPERATOR token, and a
read-only auditor is refused. A genuine controller-INTERNAL tokenless caller
(loopback, or the SYSIBLE_REMOTE_INTERNAL_EXEC opt-in) is still allowed for the
BFF's background probes.
"""
import types

import backend.remote_routes as remote_routes
from conftest import key_headers


def _fake_ssh(monkeypatch):
    """Stub subprocess.run so the auth outcome is what's tested, not real SSH."""
    def _run(*a, **k):
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")
    monkeypatch.setattr(remote_routes.subprocess, "run", _run)


def test_tokenless_exec_from_network_is_refused(controller, ssh_host):
    """The exact pentest path: machine API key, NO admin token, arbitrary command.
    Previously ran body.cmd as root, unattributed. Now refused (403) — the caller
    is neither an operator (no token) nor a loopback/internal principal."""
    name = ssh_host()
    r = controller.post(f"/remote/hosts/{name}/exec", headers=key_headers(),
                        json={"cmd": "id; cat /etc/shadow", "log": False})
    assert r.status_code == 403


def test_auditor_cannot_exec(controller, ssh_host, auditor_headers):
    name = ssh_host()
    r = controller.post(f"/remote/hosts/{name}/exec", headers=auditor_headers,
                        json={"cmd": "id", "log": False})
    assert r.status_code == 403


def test_invalid_token_is_401(controller, ssh_host):
    name = ssh_host()
    r = controller.post(f"/remote/hosts/{name}/exec",
                        headers=key_headers({"X-Sysible-Admin-Token": "bogus-token"}),
                        json={"cmd": "id"})
    assert r.status_code == 401


def test_superuser_can_exec(controller, ssh_host, superuser_headers, monkeypatch):
    _fake_ssh(monkeypatch)
    name = ssh_host()
    r = controller.post(f"/remote/hosts/{name}/exec", headers=superuser_headers,
                        json={"cmd": "id"})
    assert r.status_code == 200, r.text
    assert r.json()["code"] == 0


def test_sysadmin_operator_can_exec(controller, ssh_host, sysadmin_headers, monkeypatch):
    _fake_ssh(monkeypatch)
    name = ssh_host()
    r = controller.post(f"/remote/hosts/{name}/exec", headers=sysadmin_headers,
                        json={"cmd": "id"})
    assert r.status_code == 200, r.text


def test_internal_tokenless_exec_allowed_with_optin(controller, ssh_host, monkeypatch):
    """A split BFF/backend topology opts the co-located internal caller in via
    SYSIBLE_REMOTE_INTERNAL_EXEC=1 (mirrors the loopback allowance the default
    single-node deployment gets for free)."""
    _fake_ssh(monkeypatch)
    monkeypatch.setenv("SYSIBLE_REMOTE_INTERNAL_EXEC", "1")
    name = ssh_host()
    r = controller.post(f"/remote/hosts/{name}/exec", headers=key_headers(),
                        json={"cmd": "uptime", "log": False})
    assert r.status_code == 200, r.text


def test_list_hosts_omits_key_path(controller, ssh_host):
    """The controller SSH key path is no longer disclosed in the inventory read."""
    name = ssh_host()
    r = controller.get("/remote/hosts", headers=key_headers())
    assert r.status_code == 200
    entry = r.json()[name]
    assert "key_path" not in entry
    assert entry["ip"] and entry["user"]
