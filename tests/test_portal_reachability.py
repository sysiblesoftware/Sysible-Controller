"""A running Webserver Portal that nothing outside the container can reach.

Reported from a live controller: the console showed the portal badge green,
"Portal started.", and "Reachable at: https://192.168.8.139:8090" — while an
nmap of that port from the LAN answered `closed`.

Both halves of that were true. The portal is a separate listener on its own
port, and it really was up: the start-up health check proves it by asking
/health on loopback. But in a container that is the CONTAINER's loopback, and
docker-compose published only 8800 and 9000 — never the portal's port. So the
portal was listening somewhere nothing could route to, and the console reported
the one thing it had checked as though it were the thing the operator cares
about.

Two fixes, covered here:
  * the compose file publishes the portal's port, so the advertised address is
    the address that works;
  * the controller says so when it can tell that it will not — decided from what
    the container was CREATED with, so it is certain rather than a guess about
    the network. A port publishing is fixed at container-create time, so a port
    picked later in the console cannot be reached until the container is
    recreated, and the portal binding it happily inside the container proves
    nothing either way.
"""
import re
from pathlib import Path

import pytest

from backend import portal_manager

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def not_a_container(monkeypatch):
    monkeypatch.setenv("SYSIBLE_CONTAINER", "0")
    monkeypatch.setattr(portal_manager.os.path, "exists",
                        lambda p: False if p == "/.dockerenv" else Path(p).exists())


@pytest.fixture()
def in_a_container(monkeypatch):
    monkeypatch.setenv("SYSIBLE_CONTAINER", "1")


# ---- the diagnosis ---------------------------------------------------------
def test_a_bare_install_is_never_warned_about(not_a_container):
    """Outside a container the portal binds on the host itself; there is nothing
    between it and the network to get wrong."""
    assert portal_manager.unreachable_reason(8090) is None


def test_a_container_that_publishes_nothing_is_named_as_the_cause(in_a_container, monkeypatch):
    """The reported box exactly: an image created before the portal port was
    published. Nothing outside can reach it, whatever port is chosen."""
    monkeypatch.delenv(portal_manager.PUBLISHED_PORT_ENV, raising=False)
    why = portal_manager.unreachable_reason(8090)
    assert why, "a container with no published portal port reported no problem"
    assert "container" in why.lower()
    assert "recreate" in why.lower(), "the message does not say what to actually do"


def test_a_container_publishing_the_same_port_is_fine(in_a_container, monkeypatch):
    monkeypatch.setenv(portal_manager.PUBLISHED_PORT_ENV, "8090")
    assert portal_manager.unreachable_reason(8090) is None


def test_a_port_changed_in_the_console_is_flagged(in_a_container, monkeypatch):
    """The trap the console's own Port field sets: a container's published ports
    are fixed when it is created, so saving 9095 here silently stops working."""
    monkeypatch.setenv(portal_manager.PUBLISHED_PORT_ENV, "8090")
    why = portal_manager.unreachable_reason(9095)
    assert why, "a portal on an unpublished port reported no problem"
    assert "9095" in why and "8090" in why, \
        "the message must name both the port in use and the one that is published"


def test_a_nonsense_published_port_does_not_invent_a_warning(in_a_container, monkeypatch):
    """Better to say nothing than to accuse the operator on a value we cannot
    even parse."""
    monkeypatch.setenv(portal_manager.PUBLISHED_PORT_ENV, "not-a-port")
    assert portal_manager.unreachable_reason(8090) is None


# ---- it has to reach the console -------------------------------------------
def test_status_carries_the_reason_whatever_the_state(monkeypatch, tmp_path):
    """The console reads status(); a diagnosis it never sees is no diagnosis.
    The key is always present so the UI can rely on its shape."""
    monkeypatch.setattr(portal_manager, "PORTAL_PID_FILE", tmp_path / "portal.pid")
    monkeypatch.setattr(portal_manager, "PORTAL_PORT_FILE", tmp_path / "portal.port")
    st = portal_manager.status()
    assert st["running"] is False
    assert "unreachable_reason" in st and st["unreachable_reason"] is None


def test_a_running_portal_in_an_unpublished_container_reports_it(monkeypatch, tmp_path):
    monkeypatch.setenv("SYSIBLE_CONTAINER", "1")
    monkeypatch.delenv(portal_manager.PUBLISHED_PORT_ENV, raising=False)
    pid_file = tmp_path / "portal.pid"
    port_file = tmp_path / "portal.port"
    pid_file.write_text("4242")
    port_file.write_text("8090")
    monkeypatch.setattr(portal_manager, "PORTAL_PID_FILE", pid_file)
    monkeypatch.setattr(portal_manager, "PORTAL_PORT_FILE", port_file)
    monkeypatch.setattr(portal_manager, "_is_alive", lambda pid: True)

    st = portal_manager.status()
    assert st["running"] is True and st["port"] == 8090
    assert st["unreachable_reason"], \
        "status() reported a healthy portal that the network cannot reach, with no explanation"


# ---- ...and the port is actually published ---------------------------------
def test_the_compose_file_publishes_the_portal_port():
    """The fix that makes it work at all. `sysible_ctl controller up` brings the
    container up with this file, so it is the only place the mapping exists."""
    compose = (REPO / "docker-compose.yml").read_text()
    ports = re.findall(r'^\s*-\s*"([^"]+)"', compose, re.M)
    mapped = [p for p in ports if "SYSIBLE_PORTAL_PORT" in p or p.startswith("8090:")]
    assert mapped, ("docker-compose.yml publishes no portal port — the portal would "
                    "listen inside the container and nothing on the network could reach it")
    # A mapping is host:container, but each side may be a ${VAR:-default} whose
    # own colon must not be mistaken for the separator.
    side = r"(?:\$\{[^}]*\}|\d+)"
    for m in mapped:
        parts = re.fullmatch(rf"({side}):({side})", m)
        assert parts, f"cannot read the portal port mapping {m!r}"
        assert parts.group(1) == parts.group(2), \
            (f"portal port mapping {m!r} maps different ports inside and out; the console "
             f"advertises one number, so they have to match")


def test_the_container_is_told_which_port_it_published():
    """Without this the controller cannot tell a published port from an
    unpublished one, and is back to guessing."""
    compose = (REPO / "docker-compose.yml").read_text()
    assert re.search(r'SYSIBLE_PORTAL_PORT:\s*"\$\{SYSIBLE_PORTAL_PORT', compose), \
        "the container never learns which portal port it publishes"
