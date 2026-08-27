"""API-key-gated agent bundle (GET /remote/agent-bundle): lets a trusted machine
peer (e.g. SLEP enrolling the VMs it just built) mint a one-time AGENT enrollment
bundle with only the machine API key — no human superuser login token — and with
the controller's REAL LAN address baked in (not loopback), so a remote host can
reach back. This is the agent (pull) enrollment path, distinct from the Connect
SSH-transport POST /hosts (superuser-gated)."""
import io
import zipfile

import backend.db as db
from conftest import key_headers


def test_agent_bundle_requires_api_key(controller):
    r = controller.get("/remote/agent-bundle")
    assert r.status_code in (401, 403)


def test_agent_bundle_works_with_api_key_only(controller, make_admin):
    # An admin already exists, so a token-less superuser check would 401 — proving the
    # route is authorized by the API key ALONE (like /self-enroll-bundle).
    make_admin("someadmin", "superuser")
    db.set_controller_config("", "10.0.0.9", "ip", 9000)
    r = controller.get("/remote/agent-bundle", headers=key_headers())
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    assert "agent.py" in names and "run_agent.sh" in names and "sysible_agent.env" in names
    env = z.read("sysible_agent.env").decode()
    # A reachable controller URL is baked in, and it is NOT loopback (a remote VM must
    # be able to reach back — unlike the self-enroll bundle which forces 127.0.0.1).
    assert "SYSIBLE_CONTROLLER=https://" in env
    assert "127.0.0.1" not in env and "localhost" not in env


def test_agent_bundle_mints_fresh_token_each_call(controller, make_admin):
    make_admin("someadmin", "superuser")
    db.set_controller_config("", "10.0.0.9", "ip", 9000)
    envs = []
    for _ in range(2):
        r = controller.get("/remote/agent-bundle", headers=key_headers())
        assert r.status_code == 200, r.text
        z = zipfile.ZipFile(io.BytesIO(r.content))
        envs.append(z.read("sysible_agent.env").decode())
    tok = lambda e: next((l.split("=", 1)[1] for l in e.splitlines() if l.startswith("SYSIBLE_ENROLL_TOKEN=")), "")
    # Each call bakes a NEW single-use token, so one bundle can be installed per host.
    assert tok(envs[0]) and tok(envs[1]) and tok(envs[0]) != tok(envs[1])
