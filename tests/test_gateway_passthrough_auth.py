"""The fleet/remote read routes accept the SLOP gateway shared secret as an alternative
to the machine API key, so a co-located app behind the gateway (Sysible Connect attaching
to the LOCAL Controller in the SLOP stack) reaches them with the SSO trust it already
holds — no separate API key to provision. The machine-key path still works, and a request
with neither a valid key nor the secret is refused."""
import pytest

from conftest import key_headers  # noqa: F401

_SECRET = "gateway-passthrough-secret-test"


@pytest.fixture(autouse=True)
def _sso_secret(monkeypatch):
    monkeypatch.setenv("SYSIBLE_SSO_SHARED_SECRET", _SECRET)


def _gw():
    return {"X-Sysible-Auth": _SECRET}


def test_agents_accepts_gateway_secret_without_api_key(controller):
    r = controller.get("/agents", headers=_gw())
    assert r.status_code == 200, r.text
    assert "agents" in r.json()


def test_remote_hosts_accepts_gateway_secret_without_api_key(controller):
    r = controller.get("/remote/hosts", headers=_gw())
    assert r.status_code == 200, r.text


def test_agents_still_accepts_the_machine_api_key(controller):
    r = controller.get("/agents", headers=key_headers())
    assert r.status_code == 200, r.text


def test_agents_refuses_wrong_secret_and_no_key(controller):
    assert controller.get("/agents", headers={"X-Sysible-Auth": "nope"}).status_code == 401
    assert controller.get("/agents").status_code == 401


def test_superuser_route_still_needs_admin_token_over_gateway(controller, make_admin):
    # The passthrough opens the API-KEY floor only — a superuser-gated route (delete all
    # SSH hosts) still demands an admin token, so the gateway secret alone can't wield it.
    # (An admin must exist first, else require_superuser is in first-run bootstrap where
    # no token is required at all — unrelated to this passthrough.)
    make_admin("some-super", "superuser")
    r = controller.delete("/remote/hosts", headers=_gw())
    assert r.status_code in (401, 403), r.text
