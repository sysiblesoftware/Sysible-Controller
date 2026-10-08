"""The console port must be restrictable; the agent API port must not be.

Behind SLOP, Caddy is meant to be the only way to the console. Publishing :8800
on every interface let that be walked around — straight to :8800 and the
gateway's HSTS/CSP/frame headers are gone, and so is the platform's central login
throttle. The SLOP installer now sets this bind to the docker bridge gateway.

:9000 is the opposite case and these tests exist mostly to keep it that way. It
is the agent and CLI API; managed hosts across the network dial it, so binding it
would strand the whole fleet.
"""
import re
from pathlib import Path

COMPOSE = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text(
    encoding="utf-8")


def _port_lines():
    return [ln.strip() for ln in COMPOSE.splitlines()
            if re.match(r'^\s*-\s*"[^"]*\d+:\d+', ln)]


def test_the_console_bind_is_a_variable():
    assert any("SYSIBLE_CONTROLLER_CONSOLE_BIND" in ln and "8800:8800" in ln
               for ln in _port_lines()), "the console port is not restrictable"


def test_it_defaults_to_every_interface():
    """A STANDALONE controller has no gateway in front of it, so the default has
    to stay what it always was. Only the installer narrows it."""
    line = next(ln for ln in _port_lines() if "8800:8800" in ln)
    assert "${SYSIBLE_CONTROLLER_CONSOLE_BIND:-0.0.0.0}" in line, line


def test_the_agent_api_port_is_left_alone():
    """Managed hosts dial :9000 from across the network. Restricting it here
    would silently cut off every agent and every CLI."""
    line = next(ln for ln in _port_lines() if "9000:9000" in ln)
    assert "${" not in line, f"the agent API port was parameterised too: {line}"
    assert line.startswith('- "9000:9000"'), line
