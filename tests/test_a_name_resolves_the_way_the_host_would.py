"""Enrollment resolves a name through the PLATFORM HOST's resolver.

Asked for in these words: "Make the controller resolve it through the host."

The Controller runs in a container, and a container gets its own resolver:
Docker's embedded DNS and whatever that forwards to. The machine it runs on
has a different one -- the LAN DNS server DHCP handed it, a corporate
resolver, a search domain that turns `ubuntu-lab` into
`ubuntu-lab.corp.example.com`. So a name an operator resolves on that machine
every day was simply unknown in here, and `getaddrinfo` kept saying so --
correctly, and uselessly. Mounting the host's /etc/hosts did not help,
because the name was never in /etc/hosts: it was in DNS.

The container cannot rewrite its own /etc/resolv.conf (Docker owns it, and
this uid cannot write it), so the fix is not to repoint the container's
resolver. It is to read the host's resolver CONFIGURATION, mounted read-only,
and ask those servers directly -- same servers, same search domains.

THE PART THAT IS EASY TO GET WRONG, and which these tests exist for: on a
systemd-resolved host -- Ubuntu, Debian, most of the fleet -- /etc/resolv.conf
says `nameserver 127.0.0.53`. Inside a container that address is the
CONTAINER's loopback, where nothing is listening, so taking the file at face
value buys a timeout instead of an answer. Installed NATIVELY on the host, the
same address is systemd-resolved and is exactly the right server to ask. The
same address means opposite things, so it cannot be judged by looking at it.

These tests run a REAL DNS server and ask it real questions. Nothing is
stubbed: the client builds the packet, the server parses it and answers, and
the assertions are about what came back.

Run: pytest tests/
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
from fakedns import FakeDNS                                   # noqa: E402

from backend import host_resolver as hr                       # noqa: E402

ZONE = {
    "ubuntu-lab": ["192.168.155.135"],
    "db.corp.example.com": ["10.0.0.5"],
    "two-homes": ["10.0.0.7", "10.0.0.8"],
}


@pytest.fixture()
def dns():
    """A real DNS server on 127.0.0.2:53.

    Port 53 and no port plumbing anywhere, because resolv.conf cannot express
    a port -- `#` is its comment character -- so a test that needed one would
    be testing a path the product can never take.
    """
    srv = FakeDNS(ZONE, addr="127.0.0.2")
    yield srv
    srv.close()


@pytest.fixture()
def host(tmp_path, monkeypatch, dns):
    """The host's resolver config, as mounted, with this test's server in it."""
    def write(body):
        p = tmp_path / "resolv.conf"
        p.write_text(body)
        monkeypatch.setenv("SYSIBLE_HOST_RESOLV", str(p))
        return p
    monkeypatch.setenv("SYSIBLE_IN_CONTAINER", "0")
    write("nameserver 127.0.0.2\n")
    return write


# ---- reading the host's configuration --------------------------------------
def test_the_servers_and_search_domains_are_read():
    servers, search = hr.parse_resolv(
        "# generated\nnameserver 192.168.1.1\nnameserver 1.1.1.1\n"
        "search corp.example.com lan\noptions edns0 trust-ad\n")
    assert servers == ["192.168.1.1", "1.1.1.1"]
    assert search == ["corp.example.com", "lan"]


def test_domain_counts_as_a_search_domain():
    """The older directive, still written by some DHCP clients."""
    assert hr.parse_resolv("domain corp.example.com\n")[1] == ["corp.example.com"]


def test_a_comment_is_not_a_nameserver():
    assert hr.parse_resolv("#nameserver 9.9.9.9\n; nameserver 8.8.8.8\n")[0] == []


def test_a_port_cannot_be_written_in_resolv_conf():
    """`#` is the comment character, so `1.2.3.4#5353` IS `1.2.3.4`. Pinned
    because the first version of this module tried to honour a port there and
    silently matched nothing."""
    assert hr.parse_resolv("nameserver 1.2.3.4#5353\n")[0] == ["1.2.3.4"]


def test_the_systemd_upstream_file_is_preferred(tmp_path, monkeypatch):
    """/etc/resolv.conf is the stub pointer on these hosts; the run file has
    the servers that can actually answer."""
    run = tmp_path / "run.conf"; run.write_text("nameserver 192.168.1.1\n")
    etc = tmp_path / "etc.conf"; etc.write_text("nameserver 127.0.0.53\n")
    monkeypatch.setenv("SYSIBLE_HOST_RESOLV", f"{run}:{etc}")
    monkeypatch.setenv("SYSIBLE_IN_CONTAINER", "1")
    assert hr.host_config()["servers"] == ["192.168.1.1"]


def test_the_other_file_is_used_when_the_first_is_absent(tmp_path, monkeypatch):
    etc = tmp_path / "etc.conf"; etc.write_text("nameserver 192.168.1.1\n")
    monkeypatch.setenv("SYSIBLE_HOST_RESOLV", f"{tmp_path / 'gone'}:{etc}")
    assert hr.host_config()["servers"] == ["192.168.1.1"]


def test_nothing_mounted_is_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSIBLE_HOST_RESOLV", str(tmp_path / "nowhere"))
    cfg = hr.host_config()
    assert cfg["present"] is False and cfg["servers"] == []
    assert hr.resolve("ubuntu-lab") == ([], "")


def test_a_directory_where_the_file_should_be_is_not_a_crash(tmp_path, monkeypatch):
    """Docker creates a DIRECTORY when a bind mount's source does not exist,
    which is what a host with no systemd-resolved gets."""
    (tmp_path / "as-a-dir").mkdir()
    monkeypatch.setenv("SYSIBLE_HOST_RESOLV", str(tmp_path / "as-a-dir"))
    assert hr.host_config()["present"] is False


# ---- the same address, two meanings ----------------------------------------
def test_a_loopback_server_is_skipped_in_a_container(tmp_path, monkeypatch):
    p = tmp_path / "r.conf"
    p.write_text("nameserver 127.0.0.53\nnameserver 192.168.1.1\n")
    monkeypatch.setenv("SYSIBLE_HOST_RESOLV", str(p))
    monkeypatch.setenv("SYSIBLE_IN_CONTAINER", "1")
    cfg = hr.host_config()
    assert cfg["servers"] == ["192.168.1.1"]
    assert cfg["skipped"] == ["127.0.0.53"], \
        "127.0.0.53 is this container's loopback; asking it buys a timeout"


def test_a_loopback_server_is_used_when_installed_natively(tmp_path, monkeypatch):
    """Then it IS systemd-resolved, and it is the best server on the box: it
    knows the search domains, the per-link resolvers and the local names."""
    p = tmp_path / "r.conf"; p.write_text("nameserver 127.0.0.53\n")
    monkeypatch.setenv("SYSIBLE_HOST_RESOLV", str(p))
    monkeypatch.setenv("SYSIBLE_IN_CONTAINER", "0")
    assert hr.host_config()["servers"] == ["127.0.0.53"]


def test_the_container_check_can_be_forced_either_way(monkeypatch):
    for val, want in (("1", True), ("yes", True), ("0", False), ("off", False)):
        monkeypatch.setenv("SYSIBLE_IN_CONTAINER", val)
        assert hr.in_container() is want


# ---- asking, for real ------------------------------------------------------
def test_a_name_the_hosts_dns_knows_is_resolved(host, dns):
    got, via = hr.resolve("ubuntu-lab")
    assert got == ["192.168.155.135"]
    assert "127.0.0.2" in via, via
    assert dns.asked == ["ubuntu-lab"], "the server was not actually asked"


def test_a_search_domain_is_tried_for_a_bare_name(host, dns):
    """What makes `db` work on the host and not in here."""
    host("nameserver 127.0.0.2\nsearch corp.example.com\n")
    got, via = hr.resolve("db")
    assert got == ["10.0.0.5"]
    assert "db.corp.example.com" in via, f"the reason does not say how: {via}"
    assert dns.asked == ["db", "db.corp.example.com"], \
        f"the bare name must be tried first: {dns.asked}"


def test_a_dotted_name_does_not_get_the_search_domains(host, dns):
    """A name with a dot is already qualified; appending to it asks for
    machines that do not exist."""
    host("nameserver 127.0.0.2\nsearch corp.example.com\n")
    hr.resolve("db.corp.example.com")
    assert dns.asked == ["db.corp.example.com"]


def test_every_address_comes_back(host):
    assert sorted(hr.resolve("two-homes")[0]) == ["10.0.0.7", "10.0.0.8"]


def test_a_name_nothing_knows_resolves_to_nothing(host):
    assert hr.resolve("not-a-machine") == ([], "")


def test_a_dead_server_does_not_stop_the_next_one(host, dns):
    """A host listing a server that no longer answers is ordinary."""
    host("nameserver 192.0.2.1\nnameserver 127.0.0.2\n")   # TEST-NET-1, unroutable
    got, _via = hr.resolve("ubuntu-lab")
    assert got == ["192.168.155.135"]


def test_an_empty_name_asks_nothing(host, dns):
    assert hr.resolve("   ") == ([], "")
    assert dns.asked == []


def test_a_reply_to_someone_elses_query_is_ignored():
    """The id is checked. A UDP socket will happily hand over any packet that
    arrives on it."""
    import struct
    buf = struct.pack(">HHHHHH", 0x1234, 0x8180, 0, 0, 0, 0)
    with pytest.raises(ValueError):
        hr._parse_answers(buf, 0x5678)


def test_an_nxdomain_is_an_answer_not_an_error():
    import struct
    buf = struct.pack(">HHHHHH", 7, 0x8183, 0, 0, 0, 0)     # RCODE 3
    assert hr._parse_answers(buf, 7) == []


# ---- and enrollment uses it ------------------------------------------------
def test_enrollment_resolves_a_name_only_the_hosts_dns_knows(host, monkeypatch):
    """The whole point. No /etc/hosts entry anywhere, and it still works."""
    import backend.remote_routes as rr
    from backend import name_table
    monkeypatch.setenv("SYSIBLE_NAME_TABLE", "/nonexistent/own")
    monkeypatch.setenv("SYSIBLE_HOST_HOSTS_FILE", "/nonexistent/host")
    monkeypatch.setenv("SYSIBLE_LOCAL_HOSTS_FILE", "/nonexistent/local")
    name_table._CACHE.clear()
    monkeypatch.setattr(rr.socket, "getaddrinfo",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no container DNS")))
    assert rr._resolve_addrs("ubuntu-lab") == {"192.168.155.135"}


def test_a_written_down_name_still_wins_over_dns(host, tmp_path, monkeypatch):
    """Precedence unchanged: what the operator wrote down beats the network,
    exactly as /etc/hosts beats DNS on any machine."""
    import backend.remote_routes as rr
    from backend import name_table
    own = tmp_path / "own"; own.write_text("10.1.1.1  ubuntu-lab\n")
    monkeypatch.setenv("SYSIBLE_NAME_TABLE", str(own))
    monkeypatch.setenv("SYSIBLE_HOST_HOSTS_FILE", "/nonexistent/host")
    monkeypatch.setenv("SYSIBLE_LOCAL_HOSTS_FILE", "/nonexistent/local")
    name_table._CACHE.clear()
    assert rr._resolve_addrs("ubuntu-lab") == {"10.1.1.1"}


def test_the_failure_says_what_the_hosts_dns_was_asked(host, monkeypatch):
    from backend import name_table
    monkeypatch.setenv("SYSIBLE_NAME_TABLE", "/nonexistent/own")
    monkeypatch.setenv("SYSIBLE_HOST_HOSTS_FILE", "/nonexistent/host")
    monkeypatch.setenv("SYSIBLE_LOCAL_HOSTS_FILE", "/nonexistent/local")
    name_table._CACHE.clear()
    note = name_table.diagnose("not-a-machine")
    assert "127.0.0.2" in note and "did not have it" in note, note


def test_the_failure_says_when_the_config_is_not_mounted(tmp_path, monkeypatch):
    from backend import name_table
    monkeypatch.setenv("SYSIBLE_HOST_RESOLV", str(tmp_path / "nowhere"))
    monkeypatch.setenv("SYSIBLE_NAME_TABLE", "/nonexistent/own")
    monkeypatch.setenv("SYSIBLE_HOST_HOSTS_FILE", "/nonexistent/host")
    monkeypatch.setenv("SYSIBLE_LOCAL_HOSTS_FILE", "/nonexistent/local")
    name_table._CACHE.clear()
    assert "not mounted" in name_table.diagnose("x")


def test_the_failure_says_when_every_server_is_loopback(tmp_path, monkeypatch):
    """The systemd-resolved case, which is the one that looks like a hang."""
    from backend import name_table
    p = tmp_path / "r.conf"; p.write_text("nameserver 127.0.0.53\n")
    monkeypatch.setenv("SYSIBLE_HOST_RESOLV", str(p))
    monkeypatch.setenv("SYSIBLE_IN_CONTAINER", "1")
    monkeypatch.setenv("SYSIBLE_NAME_TABLE", "/nonexistent/own")
    monkeypatch.setenv("SYSIBLE_HOST_HOSTS_FILE", "/nonexistent/host")
    monkeypatch.setenv("SYSIBLE_LOCAL_HOSTS_FILE", "/nonexistent/local")
    name_table._CACHE.clear()
    note = name_table.diagnose("x")
    assert "only loopback" in note and "127.0.0.53" in note, note


# ---- the mounts are a contract ---------------------------------------------
@pytest.mark.parametrize("path", ["/run/systemd/resolve/resolv.conf:/host/run/resolv.conf:ro",
                                  "/etc/resolv.conf:/host/etc/resolv.conf:ro"])
def test_the_compose_file_mounts_the_resolver_config(path):
    import yaml
    repo = Path(__file__).resolve().parents[1]
    doc = yaml.safe_load((repo / "docker-compose.yml").read_text(encoding="utf-8"))
    vols = doc["services"]["controller"]["volumes"]
    assert path in vols, f"without {path} the host's resolver is invisible in here"


def test_the_paths_the_code_reads_are_the_paths_mounted(monkeypatch):
    monkeypatch.delenv("SYSIBLE_HOST_RESOLV", raising=False)
    assert hr.host_resolv_paths() == ["/host/run/resolv.conf", "/host/etc/resolv.conf"]
