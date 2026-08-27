"""F1: opt-in machine-key access to the /remote file endpoints."""
from conftest import key_headers


def test_download_requires_superuser_by_default(controller, monkeypatch):
    monkeypatch.delenv("SYSIBLE_REMOTE_FILE_API", raising=False)
    r = controller.get("/remote/hosts/anyhost/files/download?path=/etc/hostname", headers=key_headers())
    assert r.status_code == 403 and "superuser" in r.json()["detail"].lower()


def test_download_allowed_with_optin(controller, monkeypatch):
    monkeypatch.setenv("SYSIBLE_REMOTE_FILE_API", "1")
    r = controller.get("/remote/hosts/nope/files/download?path=/etc/hostname", headers=key_headers())
    assert r.status_code != 403
