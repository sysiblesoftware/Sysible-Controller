"""An agent port bound to an address this host no longer has.

Flashback and Baseline went dark together while every other service in the
stack stayed green. They are the only two that publish a port on the docker
BRIDGE gateway rather than on every interface -- the Controller dials them at
host.docker.internal, which resolves there -- and that address is recorded in
SLOP's .env once, at install time.

A bridge gateway is not a fixed property of a host. Docker hands it out, and a
network that is removed and recreated -- `docker network prune`, a daemon
restart with a changed config, a migration from the standalone layout to the
integrated one -- can come back on a different subnet. The recorded address
then names no interface at all, and publishing a port on an address the host
does not have is not a warning:

    Error starting userland proxy: listen tcp4 172.17.0.1:8771:
    bind: cannot assign requested address

The container cannot start. Both install.sh and this script seeded that
address and then trusted it forever, so nothing on the host ever noticed it
had gone stale -- and the only symptom was two red dots on a portal with no
way to ask why.

Run: pytest tests/
"""
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CTL = REPO / "deploy" / "sysiblectl"

FAKE_IP = """#!/bin/sh
printf '%s\\n' "$FAKE_IP_LINES"
"""

FAKE_DOCKER = """#!/bin/sh
case "$1 $2" in
  "network inspect") echo "${FAKE_BRIDGE_GW-172.18.0.1}" ;;
esac
exit 0
"""


@pytest.fixture
def sandbox(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("ip", FAKE_IP), ("docker", FAKE_DOCKER)):
        f = bindir / name
        f.write_text(body)
        f.chmod(0o755)
    src = CTL.read_text(encoding="utf-8")
    lib = re.sub(r'^main "\$@"\s*$', "", src, flags=re.M)
    (tmp_path / "ctl.lib.sh").write_text(lib, encoding="utf-8")
    return tmp_path


def run(sandbox, snippet, drop=(), **env):
    for name in drop:
        os.remove(os.path.join(sandbox, "bin", name))
    script = f'. "{sandbox / "ctl.lib.sh"}"\n' + textwrap.dedent(snippet)
    e = {"PATH": str(sandbox / "bin") + ":" + os.environ.get("PATH", "/usr/bin:/bin"),
         "HOME": os.environ.get("HOME", "/root")}
    e.update({k: str(v) for k, v in env.items()})
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env=e, timeout=60)
    return r.returncode, r.stdout + r.stderr


# What `ip -4 -o addr show` prints on the host: loopback, the LAN, and the
# docker bridge as it is NOW.
HERE_NOW = ("1: lo    inet 127.0.0.1/8 scope host lo\n"
            "2: ens160    inet 192.168.155.133/24 scope global ens160\n"
            "3: docker0    inet 172.18.0.1/16 scope global docker0")


def envfile(sandbox, **kv):
    f = sandbox / ".env"
    f.write_text("".join(f"{k}={v}\n" for k, v in kv.items()), encoding="utf-8")
    return f


def repair(sandbox, env_kv, lines=HERE_NOW, gw="172.18.0.1", drop=()):
    f = envfile(sandbox, **env_kv)
    rc, out = run(sandbox, f'''
        _repair_agent_bind flashback SYSIBLE_FLASHBACK_AGENT_BIND "{f}" "Flashback"
        _repair_agent_bind baseline  SYSIBLE_BASELINE_AGENT_BIND  "{f}" "Baseline"
    ''', drop=drop, FAKE_IP_LINES=lines, FAKE_BRIDGE_GW=gw)
    return out, f.read_text(encoding="utf-8")


# ---- is the address even here ----------------------------------------------
def test_an_address_on_this_host_is_recognised(sandbox):
    rc, out = run(sandbox, 'st=0; _addr_is_local 192.168.155.133 || st=$?; echo "rc=$st"',
                  FAKE_IP_LINES=HERE_NOW)
    assert "rc=0" in out, out


def test_an_address_that_is_gone_is_recognised(sandbox):
    rc, out = run(sandbox, 'st=0; _addr_is_local 172.17.0.1 || st=$?; echo "rc=$st"',
                  FAKE_IP_LINES=HERE_NOW)
    assert "rc=1" in out, out


def test_not_being_able_to_look_is_its_own_answer(sandbox):
    """Two answers would make "no iproute2" mean "the address is gone", and
    re-seeding a perfectly good address is how a repair breaks a working host."""
    rc, out = run(sandbox, 'st=0; _addr_is_local 172.17.0.1 || st=$?; echo "rc=$st"',
                  drop=("ip",), FAKE_IP_LINES=HERE_NOW)
    assert "rc=2" in out, out


# ---- the repair -------------------------------------------------------------
def test_a_stale_address_is_replaced_with_the_live_gateway(sandbox):
    out, env = repair(sandbox, {"SYSIBLE_FLASHBACK_AGENT_BIND": "172.17.0.1",
                                "SYSIBLE_BASELINE_AGENT_BIND": "172.17.0.1"})
    assert "SYSIBLE_FLASHBACK_AGENT_BIND=172.18.0.1" in env, env
    assert "SYSIBLE_BASELINE_AGENT_BIND=172.18.0.1" in env, env


def test_the_repair_says_what_was_wrong_and_what_it_did(sandbox):
    out, _env = repair(sandbox, {"SYSIBLE_FLASHBACK_AGENT_BIND": "172.17.0.1"})
    assert "could not start" in out, out
    assert "172.17.0.1" in out and "172.18.0.1" in out, out


def test_baseline_is_repaired_too(sandbox):
    """It was never handled at all: the block only ever knew about Flashback,
    so the second service with the same failure had nothing watching it."""
    out, env = repair(sandbox, {"SYSIBLE_BASELINE_AGENT_BIND": "172.17.0.1"})
    assert "Baseline" in out, out
    assert "SYSIBLE_BASELINE_AGENT_BIND=172.18.0.1" in env, env


def test_a_good_address_is_left_exactly_alone(sandbox):
    out, env = repair(sandbox, {"SYSIBLE_FLASHBACK_AGENT_BIND": "172.18.0.1",
                                "SYSIBLE_BASELINE_AGENT_BIND": "172.18.0.1"})
    assert "SYSIBLE_FLASHBACK_AGENT_BIND=172.18.0.1" in env
    assert env.count("SYSIBLE_FLASHBACK_AGENT_BIND=") == 1, env
    assert "Flashback" not in out and "Baseline" not in out, out


def test_a_missing_address_is_still_seeded(sandbox):
    """The original job of this block. It must survive the new one."""
    out, env = repair(sandbox, {})
    assert "SYSIBLE_FLASHBACK_AGENT_BIND=172.18.0.1" in env, env
    assert "had no bind address" in out, out


def test_nothing_is_rewritten_when_we_cannot_look(sandbox):
    out, env = repair(sandbox, {"SYSIBLE_FLASHBACK_AGENT_BIND": "172.17.0.1"},
                      drop=("ip",))
    assert "SYSIBLE_FLASHBACK_AGENT_BIND=172.17.0.1" in env, env


def test_a_stale_address_docker_cannot_replace_is_reported_not_hidden(sandbox):
    """If the bridge gateway IS the stale address, there is nothing to swap to
    -- and silently leaving it is how this went unnoticed in the first place."""
    out, env = repair(sandbox, {"SYSIBLE_FLASHBACK_AGENT_BIND": "172.17.0.1"},
                      gw="172.17.0.1")
    assert "not an address on this" in out, out
    assert "cannot start" in out, out


def test_the_repair_runs_as_part_of_an_update():
    src = CTL.read_text(encoding="utf-8")
    assert src.count("_repair_agent_bind ") >= 2, \
        "the repair exists but is not called for both services"
    assert "_repair_agent_bind flashback" in src and "_repair_agent_bind baseline" in src
