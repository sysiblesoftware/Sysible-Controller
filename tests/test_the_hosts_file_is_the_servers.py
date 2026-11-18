"""The hosts file the console shows is the SERVER's, and it is not frozen.

Two defects, one page.

THE FIRST. The console listed three names -- `4f96d7437a52`, which is a
container id, `host.docker.internal`, which is the machine the Controller is
running ON, and `ip6-localnet`, which is not a machine at all -- and labelled
them "container /etc/hosts". That file is Docker's, written at container
creation, and nothing in it is a host to manage. Offering
`host.docker.internal` for enrollment is offering to SSH into the platform
host, which with sshd listening could even succeed. There is one hosts file
worth showing an operator and it is the server's: the bind mount in a
container, /etc/hosts itself on the metal.

THE SECOND, and the one that cost the time. A bind mount of a single FILE is
bound to that file's INODE, not to its name. vim, sed -i and every other
editor that writes a temporary file and renames it over the original give
/etc/hosts a new inode -- and the container goes on reading the old one, with
the content it had when the container was created, forever. The mount still
reports present. It still reads without error. It is simply answering
questions about a file that no longer exists, which from the console is
indistinguishable from "the Controller cannot see my hosts file" and was
reported, accurately and uselessly, as `lists 0 names`.

It cannot be worked around from inside the container: the old inode is the
only thing we were handed. So the compose file mounts the DIRECTORY, and the
console detects and names the frozen case for every Controller that has not
been recreated yet.
"""
from pathlib import Path

import pytest
import yaml

from backend import host_resolver, name_table

REPO = Path(__file__).resolve().parents[1]
VIEW = (REPO / "webgui" / "frontend" / "src" / "views"
        / "HostEnrollment.jsx").read_text(encoding="utf-8")


@pytest.fixture()
def etc(tmp_path, monkeypatch):
    """A container with the server's whole /etc mounted at /host/etc."""
    d = tmp_path / "host-etc"
    d.mkdir()
    (d / "os-release").write_text("ID=ubuntu\n")
    (d / "hosts").write_text("127.0.0.1 localhost\n192.168.155.135  ubuntu-lab\n")
    monkeypatch.setenv("SYSIBLE_IN_CONTAINER", "1")
    monkeypatch.setenv("SYSIBLE_HOST_HOSTS_FILE", str(d / "hosts"))
    monkeypatch.setenv("SYSIBLE_NAME_TABLE", str(tmp_path / "own"))
    name_table._CACHE.clear()
    yield d
    name_table._CACHE.clear()


@pytest.fixture()
def one_file(tmp_path, monkeypatch):
    """The older deployment: the single file bind-mounted on its own, so
    /host/etc holds nothing but `hosts`."""
    d = tmp_path / "host-etc"
    d.mkdir()
    (d / "hosts").write_text("192.168.155.135  ubuntu-lab\n")
    monkeypatch.setenv("SYSIBLE_IN_CONTAINER", "1")
    monkeypatch.setenv("SYSIBLE_HOST_HOSTS_FILE", str(d / "hosts"))
    monkeypatch.setenv("SYSIBLE_NAME_TABLE", str(tmp_path / "own"))
    name_table._CACHE.clear()
    yield d
    name_table._CACHE.clear()


# ---- which file is the server's --------------------------------------------
def test_in_a_container_the_servers_file_is_the_mounted_one(etc):
    assert name_table.server_hosts_path() == str(etc / "hosts")
    assert name_table.lookup("ubuntu-lab") == ["192.168.155.135"]


def test_natively_the_servers_file_is_etc_hosts(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSIBLE_IN_CONTAINER", "0")
    monkeypatch.setenv("SYSIBLE_LOCAL_HOSTS_FILE", str(tmp_path / "etc-hosts"))
    assert name_table.server_hosts_path() == str(tmp_path / "etc-hosts")


def test_the_containers_own_etc_hosts_is_never_a_source(tmp_path, monkeypatch):
    """What was on the screen: a container id, the platform host under the name
    host.docker.internal, and ip6 boilerplate, listed as names to manage."""
    (tmp_path / "docker-hosts").write_text(
        "172.24.0.2\t4f96d7437a52\n"
        "172.17.0.1\thost.docker.internal\n"
        "fe00::0\t\tip6-localnet\n")
    monkeypatch.setenv("SYSIBLE_IN_CONTAINER", "1")
    monkeypatch.setenv("SYSIBLE_LOCAL_HOSTS_FILE", str(tmp_path / "docker-hosts"))
    monkeypatch.setenv("SYSIBLE_HOST_HOSTS_FILE", str(tmp_path / "not-mounted"))
    monkeypatch.setenv("SYSIBLE_NAME_TABLE", str(tmp_path / "own"))
    name_table._CACHE.clear()
    assert [r["name"] for r in name_table.entries()] == []
    assert name_table.lookup("host.docker.internal") == []
    paths = [p for _k, p, _l in name_table._source_specs()]
    assert str(tmp_path / "docker-hosts") not in paths


# ---- the frozen mount -------------------------------------------------------
def test_a_whole_etc_mount_is_live(etc):
    assert name_table.server_hosts_is_live()
    assert name_table.stale_mount_note() == ""


def test_a_single_file_mount_is_reported_as_frozen(one_file):
    assert not name_table.server_hosts_is_live()
    note = name_table.stale_mount_note()
    assert "vim" in note and "/etc:/host/etc:ro" in note, note


def test_the_frozen_case_is_named_in_the_diagnosis(one_file):
    """The whole point. It reads fine and lists names, so a diagnosis that
    only counted names said the mount was working."""
    msg = name_table.diagnose("ubuntu-lab")
    assert "never reach this container" in msg, msg


def test_a_live_mount_says_nothing_about_being_frozen(etc):
    assert "never reach this container" not in name_table.diagnose("nope")


def test_a_controller_on_the_metal_is_never_frozen(tmp_path, monkeypatch):
    """There is no mount in the way, so there is nothing to warn about and a
    warning would be noise on every native install."""
    monkeypatch.setenv("SYSIBLE_IN_CONTAINER", "0")
    monkeypatch.setenv("SYSIBLE_LOCAL_HOSTS_FILE", str(tmp_path / "hosts"))
    (tmp_path / "hosts").write_text("10.0.0.1  box\n")
    name_table._CACHE.clear()
    assert name_table.server_hosts_is_live()
    assert name_table.stale_mount_note() == ""


def test_the_console_is_told_whether_the_mount_is_frozen(one_file):
    src = next(s for s in name_table.sources() if s["key"] == "server")
    assert src["present"] and src["names"] == 1
    assert src["live"] is False and src["stale_note"]
    assert src["in_container"] is True


# ---- reading it, so the operator does not have to fetch it ------------------
def test_the_servers_hosts_file_can_be_read_whole(etc):
    text = name_table.read_server_hosts()
    assert "ubuntu-lab" in text and "127.0.0.1 localhost" in text


def test_reading_keeps_the_comments(etc):
    """It is text to pick hosts out of. A copy rebuilt from the parsed table
    would have dropped the comments that say what each block is for."""
    (etc / "hosts").write_text("# the lab rack\n10.0.0.5  db\n")
    name_table._CACHE.clear()
    assert "# the lab rack" in name_table.read_server_hosts()


def test_reading_a_file_that_is_not_there_is_empty_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSIBLE_IN_CONTAINER", "1")
    monkeypatch.setenv("SYSIBLE_HOST_HOSTS_FILE", str(tmp_path / "nope"))
    assert name_table.read_server_hosts() == ""


def test_a_directory_where_the_file_should_be_is_not_a_crash(tmp_path, monkeypatch):
    """A bind mount whose source does not exist makes Docker create a
    DIRECTORY at the target, so this is what a wrong path looks like."""
    (tmp_path / "as-a-dir").mkdir()
    monkeypatch.setenv("SYSIBLE_IN_CONTAINER", "1")
    monkeypatch.setenv("SYSIBLE_HOST_HOSTS_FILE", str(tmp_path / "as-a-dir"))
    name_table._CACHE.clear()
    assert name_table.read_server_hosts() == ""
    assert name_table.lookup("anything") == []


def test_the_route_serves_it_with_the_frozen_flag(one_file):
    import backend.remote_routes as rr
    out = rr.names_server_hosts()
    assert "ubuntu-lab" in out["text"]
    assert out["path"] == str(one_file / "hosts")
    assert out["present"] is True
    assert out["live"] is False and out["stale_note"]


def test_every_names_route_is_superuser_only():
    """This one reads a file off the platform host, which is more than the
    others do -- and asserting the whole set means the next route added here
    cannot arrive unguarded either."""
    import backend.remote_routes as rr
    routes = [r for r in rr.router.routes
              if getattr(r, "path", "").startswith("/remote/names")]
    assert any(r.path == "/remote/names/server-hosts" for r in routes), \
        "the server's hosts file is not served at all"
    for r in routes:
        guards = {getattr(d.dependency, "__name__", "") for d in r.dependencies}
        assert "require_superuser" in guards, f"{r.path} {sorted(r.methods)}: {guards}"


# ---- the resolver config has the same problem, and the same fix ------------
def test_a_loopback_only_resolver_file_does_not_end_the_search(tmp_path, monkeypatch):
    """With the whole /etc mounted, /host/etc/resolv.conf is now readable --
    and on a systemd-resolved host it is the 127.0.0.53 stub, which in here is
    this container. Stopping at the first file that parses would report the
    one answer that cannot work and never reach the real upstreams."""
    (tmp_path / "stub.conf").write_text("nameserver 127.0.0.53\nsearch lan\n")
    (tmp_path / "real.conf").write_text("nameserver 192.168.155.2\nsearch lan\n")
    monkeypatch.setenv("SYSIBLE_IN_CONTAINER", "1")
    monkeypatch.setenv("SYSIBLE_HOST_RESOLV",
                       f"{tmp_path / 'stub.conf'}:{tmp_path / 'real.conf'}")
    cfg = host_resolver.host_config()
    assert cfg["servers"] == ["192.168.155.2"], cfg


def test_loopback_everywhere_is_still_reported_as_loopback(tmp_path, monkeypatch):
    """Falling through must not turn "every server here is this container" into
    "no resolver config at all" -- different causes, different fixes."""
    (tmp_path / "a.conf").write_text("nameserver 127.0.0.53\n")
    (tmp_path / "b.conf").write_text("nameserver 127.0.0.1\n")
    monkeypatch.setenv("SYSIBLE_IN_CONTAINER", "1")
    monkeypatch.setenv("SYSIBLE_HOST_RESOLV",
                       f"{tmp_path / 'a.conf'}:{tmp_path / 'b.conf'}")
    cfg = host_resolver.host_config()
    assert cfg["present"] and cfg["servers"] == [] and cfg["skipped"] == ["127.0.0.53"]
    assert "only loopback" in name_table.host_dns_note()


def test_the_systemd_upstreams_are_tried_before_etc_resolv_conf(monkeypatch):
    monkeypatch.delenv("SYSIBLE_HOST_RESOLV", raising=False)
    paths = host_resolver.host_resolv_paths()
    assert paths.index("/host/run/systemd-resolve/resolv.conf") < \
        paths.index("/host/etc/resolv.conf"), paths


# ---- the mounts are the contract -------------------------------------------
@pytest.mark.parametrize("compose", [REPO / "docker-compose.yml"])
def test_no_mount_target_is_a_file_the_code_opens(compose, monkeypatch):
    """The property, not a list of blessed strings: never bind-mount a path
    that the code calls open() on. Such a mount is bound to that file's inode,
    so the first `vim` on the server detaches it for good. Mount the directory
    that CONTAINS it, and the name is resolved fresh on every open.

    Stated this way it keeps itself honest -- a new file the resolver learns
    to read is covered the day it is added, with nothing here to remember."""
    monkeypatch.delenv("SYSIBLE_HOST_RESOLV", raising=False)
    monkeypatch.delenv("SYSIBLE_HOST_HOSTS_FILE", raising=False)
    opened = set(host_resolver.host_resolv_paths())
    opened.add(name_table.host_hosts_path())

    doc = yaml.safe_load(compose.read_text(encoding="utf-8"))
    vols = [v for v in doc["services"]["controller"]["volumes"] if "/host/" in v]
    assert vols, "the controller no longer reads anything from the server"
    targets = []
    for v in vols:
        target = v.split(":")[1]
        assert target not in opened, (
            f"{v} mounts {target}, which is a file the Controller open()s. A "
            f"bind mount of a FILE is bound to its inode: the first edit on "
            f"the server detaches this container from it, silently and for "
            f"good. Mount {str(Path(target).parent)} instead.")
        assert v.endswith(":ro"), f"{v} is not read-only"
        targets.append(target)

    # ...and the files the code opens have to be reachable THROUGH one of
    # those directories, or the mounts are tidy and useless.
    reachable = [p for p in opened
                 if any(p.startswith(t.rstrip("/") + "/") for t in targets)]
    assert reachable, f"none of {sorted(opened)} is inside any of {targets}"
    assert name_table.host_hosts_path() in reachable, \
        "the server's /etc/hosts is not reachable through any mount"


def test_the_console_offers_the_servers_hosts_file(monkeypatch):
    """Nearly every list an operator wants to enroll from is the one the
    server already keeps, and the Controller can read it directly. Asking them
    to fetch it by hand was asking them to do this."""
    assert "serverHosts" in VIEW, "the console never asks for it"
    assert "Use the server's hosts file" in VIEW, "there is no way to ask for it"


def test_the_console_warns_when_the_mount_is_frozen():
    assert "staleServerHosts" in VIEW, \
        "the console shows a frozen mount as an ordinary working one"


def test_the_preview_is_given_the_text_not_left_to_read_state():
    """React state is not set by the time the caller returns. "Use the
    server's hosts file" has the new text in hand, and a preview that read
    bulkText instead would have shown the list as it was one edit ago -- which
    looks exactly like the button doing nothing."""
    import re
    for fn in ("loadServerHosts",):
        m = re.search(r"async function " + fn + r"\(\)[\s\S]*?\n  \}", VIEW)
        assert m, f"{fn} is gone or no longer in the expected shape"
        calls = re.findall(r"previewHostsFile\(([^)]*)\)", m.group(0))
        assert calls, f"{fn} never previews what it just added"
        for arg in calls:
            assert arg.strip(), (
                f"{fn} calls previewHostsFile() with no argument, so it reads "
                f"the state it has only just asked React to change")
