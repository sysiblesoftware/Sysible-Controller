"""
Controller self-enroll bundle: `sysible_controller self-enroll` downloads this
to enroll the controller itself as a managed host. It must (a) require the admin
API key, (b) always target loopback (127.0.0.1) so it works on a fresh install
before any LAN address is configured, and (c) mint a real single-use enrollment
token like every other bundle.
"""
import io
import zipfile

from conftest import key_headers


def test_self_enroll_bundle_requires_api_key(controller):
    r = controller.get("/self-enroll-bundle")
    assert r.status_code in (401, 403)


def test_self_enroll_bundle_is_loopback_zip(controller):
    r = controller.get("/self-enroll-bundle", headers=key_headers())
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    assert "agent.py" in names and "run_agent.sh" in names and "sysible_agent.env" in names
    env = z.read("sysible_agent.env").decode()
    assert "https://127.0.0.1:9000" in env      # loopback, not the configured LAN addr


def test_self_enroll_bundle_mints_a_usable_token(controller):
    import backend.db as db
    before = len(db.list_enroll_tokens()) if hasattr(db, "list_enroll_tokens") else None
    r = controller.get("/self-enroll-bundle", headers=key_headers())
    assert r.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(r.content))
    env = z.read("sysible_agent.env").decode()
    # The env file carries the freshly-minted token; it must validate on the
    # controller (the agent uses it on its first /agents/enroll call).
    tok = None
    for line in env.splitlines():
        if line.startswith("SYSIBLE_ENROLL_TOKEN="):
            tok = line.split("=", 1)[1].strip().strip('"')
            break
    assert tok, "bundle env is missing SYSIBLE_ENROLL_TOKEN"
    assert db.validate_enroll_token(tok) is True
