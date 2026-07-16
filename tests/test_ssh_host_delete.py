"""BFF SSH-host delete route.

A pure-SSH host (no Sysible agent) was previously undeletable from the console:
the agent-oriented `removeHost` path never touched the SSH connection record.
`DELETE /api/ssh-host/{name}` fills that gap by proxying to the controller's
superuser-gated `DELETE /remote/hosts/{name}`.
"""
import os
import tempfile

os.environ.setdefault("SYSIBLE_API_KEY", "test-sshdel-key")
os.environ.setdefault("SYSIBLE_DATA_DIR", tempfile.mkdtemp(prefix="sysible-sshdel-"))

from fastapi.testclient import TestClient  # noqa: E402
import webgui.server as w  # noqa: E402

client = TestClient(w.app)


def test_delete_ssh_host_requires_auth():
    # No session cookie: the superuser dependency rejects before any proxying.
    r = client.delete("/api/ssh-host/web01",
                      headers={"origin": "http://testserver", "host": "testserver"})
    assert r.status_code in (401, 403)


def test_delete_ssh_host_proxies_to_controller():
    """With a superuser session, the route delegates to the controller's
    DELETE /remote/hosts/{name} verbatim (name URL-decoded, method preserved)."""
    calls = []

    def fake_superuser_request(method, path, request, **kw):
        calls.append((method, path))
        return {"deleted": True}

    orig = w._superuser_request
    w.app.dependency_overrides[w.require_superuser_session] = lambda: "root"
    w._superuser_request = fake_superuser_request
    try:
        r = client.delete("/api/ssh-host/web 01",
                          headers={"origin": "http://testserver", "host": "testserver"})
        assert r.status_code == 200, r.text
        assert r.json() == {"deleted": True}
        assert calls == [("DELETE", "/remote/hosts/web 01")]
    finally:
        w._superuser_request = orig
        w.app.dependency_overrides.pop(w.require_superuser_session, None)


def test_delete_all_ssh_hosts_requires_auth():
    r = client.delete("/api/ssh-hosts",
                      headers={"origin": "http://testserver", "host": "testserver"})
    assert r.status_code in (401, 403)


def test_delete_all_ssh_hosts_proxies_to_controller():
    """The bulk cleanup delegates to the controller's DELETE /remote/hosts (no name)."""
    calls = []

    def fake_superuser_request(method, path, request, **kw):
        calls.append((method, path))
        return {"deleted": 3, "hosts": ["a", "b", "c"]}

    orig = w._superuser_request
    w.app.dependency_overrides[w.require_superuser_session] = lambda: "root"
    w._superuser_request = fake_superuser_request
    try:
        r = client.delete("/api/ssh-hosts",
                          headers={"origin": "http://testserver", "host": "testserver"})
        assert r.status_code == 200, r.text
        assert r.json()["deleted"] == 3
        assert calls == [("DELETE", "/remote/hosts")]
    finally:
        w._superuser_request = orig
        w.app.dependency_overrides.pop(w.require_superuser_session, None)
