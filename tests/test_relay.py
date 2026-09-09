"""Sysible Relay — the bastion / jump-box transport.

The relay decides whether a connection goes THROUGH a bastion or AROUND it, and
both wrong answers are expensive: routing a directly-reachable host through a
jump box breaks it, and NOT routing a host that needs one silently reaches it by
a path the operator believes is closed.

Two properties get the most attention here:

* **Never fail open at save time.** Everywhere else a bad relay config degrades
  to "connect directly", which is safe at connect time. At SAVE time that same
  behaviour means the controller quietly bypasses the bastion, so the settings
  form has to refuse.
* **Never emit something ssh could re-read as an option.** The jump string and
  the identity path are handed to ``ssh -J`` / ``IdentityFile``; a leading dash
  or a shell metacharacter in either is a command-injection primitive.
"""
import backend.db as db
import pytest

from backend import relay


def _set(**fields):
    """Store relay settings directly, bypassing validation, so a test can pin what
    resolve_ssh_proxy does with a config that is already bad."""
    current = db.get_relay_config() or {}
    current.update({k: str(v) for k, v in fields.items()})
    db.set_relay_config(current, "tester")


@pytest.fixture(autouse=True)
def clean_relay_config(monkeypatch):
    """Every test starts with the relay OFF and leaves it that way — a leaked
    relay_host would silently re-route hosts in later tests. The environment
    fallbacks are cleared too, so a developer with SYSIBLE_RELAY_* exported does
    not get different results from CI."""
    for env in relay.ENV_KEYS.values():
        monkeypatch.delenv(env, raising=False)
    # A blank identity would otherwise default to the controller's relay key path,
    # which several routing assertions don't want to see.
    monkeypatch.setattr(relay, "_default_identity", lambda: "")
    db.set_relay_config({}, "tester")
    yield
    db.set_relay_config({}, "tester")


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
def test_the_relay_id_matches_the_enterprise_edition():
    """Shared with Enterprise so one set of bastion scripts and one page of docs
    describe both editions."""
    assert relay.RELAY_ID == "sysible-relay"
    assert relay.DEFAULT_RELAY_USER == "sysible-relay"


def test_the_relay_is_off_until_a_bastion_is_set():
    """There is no separate enable switch, so 'configured' must mean exactly
    'has somewhere to route through'."""
    assert relay.configured() is False
    assert relay.all_relays() == []
    assert relay.resolve_ssh_proxy({"ip": "10.0.0.5", "relay": True}) is None


# --------------------------------------------------------------------------- #
# Routing decision
# --------------------------------------------------------------------------- #
def test_a_host_that_did_not_opt_in_is_not_routed():
    _set(relay_host="bastion.example.com")
    assert relay.resolve_ssh_proxy({"ip": "10.0.0.5", "name": "web1"}) is None


def test_a_host_opted_in_by_relay_id_is_routed():
    _set(relay_host="bastion.example.com", relay_identity="")
    got = relay.resolve_ssh_proxy({"ip": "10.0.0.5", "relay": "sysible-relay"})
    assert got == {"jump": "sysible-relay@bastion.example.com"}


@pytest.mark.parametrize("token", ["true", "1", "yes", "on", "enabled", True])
def test_a_host_opted_in_by_a_truthy_token_is_routed(token):
    _set(relay_host="bastion.example.com")
    assert relay.resolve_ssh_proxy({"ip": "10.0.0.5", "relay": token}) is not None


def test_port_22_is_omitted_and_a_custom_port_is_included():
    """ssh treats a bare host as :22; emitting it anyway is noise in every log
    line and in the inventory key."""
    _set(relay_host="bastion.example.com", relay_port="22")
    assert relay.resolve_ssh_proxy({"relay": True})["jump"] == "sysible-relay@bastion.example.com"
    _set(relay_port="2222")
    assert relay.resolve_ssh_proxy({"relay": True})["jump"] == "sysible-relay@bastion.example.com:2222"


def test_a_cidr_allowlist_routes_the_network_behind_the_bastion():
    _set(relay_host="bastion.example.com", route_allowlist="10.20.0.0/16")
    assert relay.resolve_ssh_proxy({"ip": "10.20.5.5"}) is not None      # inside
    assert relay.resolve_ssh_proxy({"ip": "10.30.5.5"}) is None          # outside


def test_a_suffix_allowlist_routes_by_name():
    _set(relay_host="bastion.example.com", route_allowlist=".internal.example.com")
    assert relay.resolve_ssh_proxy({"name": "db1.internal.example.com"}) is not None
    assert relay.resolve_ssh_proxy({"name": "db1.public.example.com"}) is None


def test_a_bare_dot_allowlist_does_not_match_every_host():
    """'.' would strip to an empty suffix, and an empty suffix endswith-matches
    ANY name — routing the whole fleet through a bastion by accident."""
    _set(relay_host="bastion.example.com", route_allowlist=". , ..")
    assert relay.resolve_ssh_proxy({"name": "anything.example.com"}) is None


def test_with_no_allowlist_only_explicit_opt_in_routes():
    """Turning the relay on must not silently re-route a fleet that was reachable
    directly."""
    _set(relay_host="bastion.example.com")
    assert relay.resolve_ssh_proxy({"ip": "10.20.5.5", "name": "web1"}) is None
    assert relay.resolve_ssh_proxy({"ip": "10.20.5.5", "relay": True}) is not None


# --------------------------------------------------------------------------- #
# Never emit something ssh could re-read as an option
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_host", [
    "-oProxyCommand=touch /tmp/pwned",
    "bastion.example.com -oProxyCommand=x",
    "bastion;touch /tmp/pwned",
    "bastion`id`",
    "bastion$(id)",
])
def test_an_option_injecting_relay_host_emits_no_jump(bad_host):
    """Fail SAFE: a jump we know is malformed is dropped, so the host is treated
    as directly reachable rather than dialled with a smuggled ssh option."""
    _set(relay_host=bad_host)
    assert relay.resolve_ssh_proxy({"relay": True}) is None


@pytest.mark.parametrize("bad_user", ["-oProxyCommand=x", "user;id", "user name"])
def test_an_option_injecting_relay_user_emits_no_jump(bad_user):
    _set(relay_host="bastion.example.com", relay_user=bad_user)
    assert relay.resolve_ssh_proxy({"relay": True}) is None


def test_an_out_of_range_port_is_not_emitted_as_typed():
    """The hop regex's [0-9]{1,5} alone would accept host:99999."""
    assert relay.valid_jump("bastion.example.com:99999") is False
    assert relay.valid_jump("bastion.example.com:2222") is True


@pytest.mark.parametrize("bad_identity", [
    "-oProxyCommand=x", "/key path/id_ed25519", "/keys/id;id", "/keys/$(id)",
])
def test_a_dangerous_identity_path_is_dropped_but_the_jump_survives(bad_identity):
    """The identity is optional, so a dangerous one is dropped rather than
    costing the whole route — but it is never forwarded."""
    _set(relay_host="bastion.example.com", relay_identity=bad_identity)
    got = relay.resolve_ssh_proxy({"relay": True})
    assert got is not None and "identity" not in got


def test_a_clean_identity_is_forwarded():
    _set(relay_host="bastion.example.com", relay_identity="/opt/sysible/relay_keys/relay_ed25519")
    got = relay.resolve_ssh_proxy({"relay": True})
    assert got["identity"] == "/opt/sysible/relay_keys/relay_ed25519"


def test_resolve_ssh_proxy_never_raises_on_junk():
    """A relay fault must degrade to a direct connection, never break transport."""
    _set(relay_host="bastion.example.com")
    for junk in (None, {}, {"ip": None}, {"relay": object()}, "not-a-dict"):
        try:
            relay.resolve_ssh_proxy(junk)
        except Exception as e:                                  # pragma: no cover
            pytest.fail(f"resolve_ssh_proxy raised on {junk!r}: {e}")


def test_valid_jump_chain_rejects_a_bad_hop_anywhere_in_the_chain():
    assert relay.valid_jump_chain("a@h1,b@h2:2222") == "a@h1,b@h2:2222"
    assert relay.valid_jump_chain("a@h1,-oProxyCommand=x") is None
    assert relay.valid_jump_chain("a@h1,") is None
    assert relay.valid_jump_chain("x" * 600) is None


# --------------------------------------------------------------------------- #
# Inventory spec
# --------------------------------------------------------------------------- #
def test_all_relays_describes_the_endpoint_for_the_inventory():
    _set(relay_host="bastion.example.com", relay_port="2222", relay_user="jump",
         relay_identity="/keys/relay", relay_os="linux")
    (spec,) = relay.all_relays()
    assert spec == {"id": "sysible-relay", "host": "bastion.example.com", "port": 2222,
                    "user": "jump", "os_hint": "linux", "identity": "/keys/relay"}


def test_all_relays_defaults_the_user_port_and_os():
    _set(relay_host="bastion.example.com")
    (spec,) = relay.all_relays()
    assert (spec["user"], spec["port"], spec["os_hint"]) == ("sysible-relay", 22, "auto")
    assert "identity" not in spec


def test_get_config_only_answers_for_our_own_relay_id():
    """relay_inventory passes the id stored on the row; a foreign id must not
    silently receive this relay's credentials."""
    _set(relay_host="bastion.example.com")
    assert relay.get_config("sysible-relay")["relay_host"] == "bastion.example.com"
    assert relay.get_config("some-other-relay") == {}


# --------------------------------------------------------------------------- #
# Validation — the one place the relay is allowed to refuse
# --------------------------------------------------------------------------- #
def _save(**fields):
    return relay.save_config(fields, actor="tester")


def test_saving_a_public_key_as_the_identity_is_refused():
    """The single most common mistake, and it surfaces at connect time only as a
    silent auth failure."""
    with pytest.raises(Exception) as e:
        _save(relay_host="bastion.example.com", relay_identity="/keys/relay.pub")
    assert "private" in str(e.value).lower() and ".pub" in str(e.value)


def test_saving_a_loopback_relay_host_is_refused():
    with pytest.raises(Exception) as e:
        _save(relay_host="127.0.0.1")
    assert "loopback" in str(e.value).lower()
    with pytest.raises(Exception):
        _save(relay_host="localhost")


def test_saving_the_controllers_own_address_is_refused(monkeypatch):
    """Pointing the relay at the controller makes every connection auth-fail
    against an account that does not exist there."""
    monkeypatch.setattr(relay, "_controller_local_addresses",
                        lambda: {"10.1.2.3", "controller.example.com"})
    with pytest.raises(Exception) as e:
        _save(relay_host="10.1.2.3")
    assert "controller" in str(e.value).lower()


def test_saving_an_endpoint_ssh_could_not_use_is_refused():
    """Fail LOUD at save time: silently not routing means going AROUND the
    bastion, which is the opposite of what the operator asked for."""
    with pytest.raises(Exception):
        _save(relay_host="bastion example.com")
    with pytest.raises(Exception):
        _save(relay_host="bastion.example.com", relay_user="-oProxyCommand=x")


def test_a_real_bastion_config_saves_and_takes_effect():
    _save(relay_host="bastion.example.com", relay_user="jump", relay_port="2222",
          relay_identity="/opt/sysible/relay_keys/relay_ed25519")
    assert relay.configured() is True
    assert relay.resolve_ssh_proxy({"relay": True}) == {
        "jump": "jump@bastion.example.com:2222",
        "identity": "/opt/sysible/relay_keys/relay_ed25519"}


def test_clearing_the_relay_host_turns_routing_off():
    """Disenroll's job: stop routing through a bastion that was just dropped."""
    _save(relay_host="bastion.example.com")
    assert relay.resolve_ssh_proxy({"relay": True}) is not None
    _save(relay_host="")
    assert relay.resolve_ssh_proxy({"relay": True}) is None


def test_a_blank_relay_host_is_not_treated_as_a_broken_config():
    """'Off' must be savable — validation only judges a relay that is set."""
    relay.validate_config({"relay_host": "", "relay_identity": ""})


# --------------------------------------------------------------------------- #
# Bastion setup scripts
# --------------------------------------------------------------------------- #
def test_the_bastion_setup_scripts_ship_in_the_build():
    """These used to come from the pip package. If they don't ship in-tree, the
    'Enroll a bastion' one-liner 404s on the jump box."""
    linux = relay.bastion_script("linux")
    windows = relay.bastion_script("windows")
    assert linux and "#!" in linux.splitlines()[0]
    assert windows and len(windows) > 1000
    assert relay.bastion_script("plan9") is None
    assert relay.bastion_script("") is None




# --------------------------------------------------------------------------- #
# The console's controls
# --------------------------------------------------------------------------- #
def test_the_relay_endpoint_never_returns_the_private_key(controller, superuser_headers):
    """It returns the key's PATH so an operator can see what's configured. The key
    itself is the credential that authenticates to every bastion — it must not be
    reachable through an API response."""
    _set(relay_host="bastion.example.com", relay_identity="/keys/relay")
    body = controller.get("/admin/relay", headers=superuser_headers).json()
    assert body["relay"]["relay_identity"] == "/keys/relay"
    blob = str(body)
    assert "PRIVATE KEY" not in blob and "BEGIN OPENSSH" not in blob


def test_saving_a_broken_relay_is_a_400_not_a_silent_bypass(controller, superuser_headers):
    r = controller.post("/admin/relay", headers=superuser_headers,
                        json={"relay_host": "127.0.0.1"})
    assert r.status_code == 400
    assert "loopback" in r.json()["detail"].lower()
    # And nothing was stored, so the controller is not half-configured.
    assert relay.configured() is False


def test_saving_one_field_does_not_blank_the_others(controller, superuser_headers):
    controller.post("/admin/relay", headers=superuser_headers,
                    json={"relay_host": "bastion.example.com", "relay_user": "jump"})
    controller.post("/admin/relay", headers=superuser_headers, json={"relay_port": "2222"})
    cfg = relay.config()
    assert cfg["relay_host"] == "bastion.example.com" and cfg["relay_user"] == "jump"
    assert cfg["relay_port"] == 2222


def test_the_relay_endpoints_are_superuser_only(controller, sysadmin_headers):
    assert controller.get("/admin/relay", headers=sysadmin_headers).status_code in (401, 403)
    assert controller.post("/admin/relay", headers=sysadmin_headers,
                           json={"relay_host": "b"}).status_code in (401, 403)


def test_the_bastion_script_is_public_and_named(controller):
    """The one-liner curls it FROM the bastion, before any credential exists."""
    r = controller.get("/api/relay/bastion-script?os=linux")
    assert r.status_code == 200
    assert r.text.startswith("#!") and "Sysible Relay" in r.text
    r = controller.get("/api/relay/bastion-script?os=windows")
    assert r.status_code == 200
    assert "install-windows-bastion.ps1" in r.headers.get("content-disposition", "")
    assert controller.get("/api/relay/bastion-script?os=beos").status_code == 400


def test_the_environment_is_the_fallback_not_the_override(monkeypatch):
    """A compose deployment can bake a relay in, but the console must still be able
    to point the controller somewhere else without editing the file."""
    monkeypatch.setenv("SYSIBLE_RELAY_HOST", "from-compose.example.com")
    assert relay.config()["relay_host"] == "from-compose.example.com"
    _set(relay_host="from-console.example.com")
    assert relay.config()["relay_host"] == "from-console.example.com"
    # Clearing it in the console falls back to the environment rather than to "off",
    # which is what "the environment is the fallback" has to mean.
    _set(relay_host="")
    assert relay.config()["relay_host"] == "from-compose.example.com"
