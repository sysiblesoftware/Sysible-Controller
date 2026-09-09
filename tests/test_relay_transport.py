"""Wiring the relay into the SSH paths.

backend/relay.py decides WHETHER a host is routed through the bastion; this is
about whether that decision actually reaches the connection. There are four
places the controller opens SSH — the subprocess dispatch path, key enrollment,
the terminal, and SFTP — and a relay that only reaches three of them is worse
than none: the operator sees hosts working and cannot tell which path silently
went around the jump box.

The other thing pinned here is teardown. A relay connection is TWO SSH sessions,
and this module closes clients from a dozen places, so the jump hop is closed by
wrapping the outer client's own ``close`` rather than by finding every call site.
Miss one and each relayed connection leaks a socket and a transport thread.
"""
import backend.remote_routes as rr
import pytest

from backend import relay


@pytest.fixture(autouse=True)
def relay_off(monkeypatch):
    for env in relay.ENV_KEYS.values():
        monkeypatch.delenv(env, raising=False)
    import backend.db as db
    db.set_relay_config({}, "tester")
    yield
    db.set_relay_config({}, "tester")


def _route_everything(monkeypatch, identity=""):
    """Pretend the relay claims every host, so these tests are about the wiring
    rather than about the routing decision (which test_relay.py covers)."""
    spec = {"jump": "sysible-relay@bastion.example.com:2222"}
    if identity:
        spec["identity"] = identity
    monkeypatch.setattr(relay, "resolve_ssh_proxy", lambda host: dict(spec))
    return spec


# --- the subprocess path ----------------------------------------------------
def test_ssh_argv_has_no_jump_when_the_host_is_not_routed():
    argv = rr._ssh_argv("/k", "root@10.0.0.5", "id")
    assert "-J" not in argv
    assert argv[-3:] == ["--", "root@10.0.0.5", "id"]


def test_ssh_argv_routes_through_the_bastion():
    argv = rr._ssh_argv("/k", "root@10.0.0.5", "id",
                        proxy={"jump": "sysible-relay@bastion.example.com:2222"})
    assert argv[argv.index("-J") + 1] == "sysible-relay@bastion.example.com:2222"
    # `--` must still come last so the target can never be read as an option.
    assert argv[-3:] == ["--", "root@10.0.0.5", "id"]


def test_ssh_argv_adds_the_relay_identity_for_the_jump_hop_only():
    argv = rr._ssh_argv("/host-key", "root@10.0.0.5", "id",
                        proxy={"jump": "j@b", "identity": "/keys/relay"})
    assert "IdentityFile=/keys/relay" in argv
    # The HOST's own key is still what authenticates to the host.
    assert argv[argv.index("-i") + 1] == "/host-key"


def test_the_jump_never_lands_after_the_option_terminator():
    """`--` ends option parsing. A -J emitted after it would be passed to the
    remote host as an argument instead of routing anything."""
    argv = rr._ssh_argv("/k", "root@10.0.0.5", "id", proxy={"jump": "j@b"})
    assert argv.index("-J") < argv.index("--")


# --- the routing decision reaching each path --------------------------------
def test_host_proxy_asks_the_relay_and_never_raises(monkeypatch):
    _route_everything(monkeypatch)
    assert rr._host_proxy({"ip": "10.0.0.5"})["jump"].endswith(":2222")

    def boom(host):
        raise RuntimeError("relay exploded")
    monkeypatch.setattr(relay, "resolve_ssh_proxy", boom)
    # A relay fault must degrade to a direct connection, not break transport.
    assert rr._host_proxy({"ip": "10.0.0.5"}) is None


def test_every_ssh_path_consults_the_relay():
    """A path that never calls _host_proxy silently bypasses the bastion. Checked
    against the source because three of the four are inside request handlers that
    would need a live SSH server to exercise."""
    import inspect
    src = inspect.getsource(rr)
    # dispatch (subprocess), key enrollment, terminal, sftp.
    assert src.count("_host_proxy(") >= 5, "a SSH path stopped consulting the relay"
    for fn in ("open_terminal",):
        assert "_host_proxy(" in inspect.getsource(getattr(rr, fn))


def test_the_hosts_own_name_is_passed_for_suffix_matching():
    """hosts.json keys BY name, so the record itself has none. Without passing it
    the allowlist's domain-suffix half silently never matches on these paths."""
    import inspect
    src = inspect.getsource(rr)
    assert '_host_proxy({**host, "name": name})' in src


# --- jump-hop parsing -------------------------------------------------------
@pytest.mark.parametrize("hop,expected", [
    ("bastion.example.com", (None, "bastion.example.com", 22)),
    ("jump@bastion.example.com", ("jump", "bastion.example.com", 22)),
    ("jump@bastion.example.com:2222", ("jump", "bastion.example.com", 2222)),
    ("[2001:db8::1]", (None, "2001:db8::1", 22)),
    ("jump@[2001:db8::1]:2222", ("jump", "2001:db8::1", 2222)),
])
def test_parse_jump_hop(hop, expected):
    assert rr._parse_jump_hop(hop) == expected


def test_an_ipv6_literals_colons_are_not_read_as_a_port():
    """The bare-IPv6 case is why the bracket branch exists: rsplit(':') on
    2001:db8::1 would otherwise take '1' as the port and mangle the host."""
    assert rr._parse_jump_hop("[2001:db8::1]") == (None, "2001:db8::1", 22)


# --- teardown ---------------------------------------------------------------
class _FakeClient:
    def __init__(self):
        self.closed = False
        self.connected_with = None

    def connect(self, ip, **kw):
        self.connected_with = (ip, kw)

    def close(self):
        self.closed = True


def test_closing_a_relayed_client_also_closes_the_jump(monkeypatch):
    """The whole reason close is wrapped: this module closes clients from a dozen
    places, and a missed one leaks a live SSH session per relayed connection."""
    jump = _FakeClient()
    chan = type("C", (), {})()
    chan.sysible_jump_client = jump
    monkeypatch.setattr(rr, "_proxy_sock", lambda proxy, ip, port=22: chan)

    client = _FakeClient()
    rr._ssh_connect(client, "10.0.0.5", proxy={"jump": "j@b"}, username="root")
    assert client.connected_with[1]["sock"] is chan
    assert not jump.closed

    client.close()
    assert client.closed and jump.closed, "the relay hop outlived the session"


def test_a_failed_connect_through_the_relay_leaves_nothing_open(monkeypatch):
    jump = _FakeClient()
    chan = type("C", (), {})()
    chan.sysible_jump_client = jump
    monkeypatch.setattr(rr, "_proxy_sock", lambda proxy, ip, port=22: chan)

    class _Boom(_FakeClient):
        def connect(self, ip, **kw):
            raise OSError("host unreachable")

    with pytest.raises(OSError):
        rr._ssh_connect(_Boom(), "10.0.0.5", proxy={"jump": "j@b"}, username="root")
    assert jump.closed, "the jump session leaked when the target refused"


def test_a_direct_connection_is_untouched():
    """No relay must mean exactly the old behaviour — no sock, no wrapping."""
    client = _FakeClient()
    rr._ssh_connect(client, "10.0.0.5", username="root")
    ip, kw = client.connected_with
    assert ip == "10.0.0.5" and "sock" not in kw
    assert not hasattr(client, "_sysible_jump_client")


def test_the_connect_phases_are_bounded_either_way():
    """A host that completes the TCP handshake then stalls at the banner would
    otherwise tie up a worker for paramiko's much longer defaults."""
    client = _FakeClient()
    rr._ssh_connect(client, "10.0.0.5", username="root")
    kw = client.connected_with[1]
    assert kw["banner_timeout"] == 15 and kw["auth_timeout"] == 15
