"""A port that was never published is not a dead app.

Reported with a screenshot: `sysiblectl slop start` on a Server printed

    ERROR: Sysible Controller is NOT answering on port 8800 after 60s.
    ERROR: Sysible Linux Engineering Platform is NOT answering on port 8810 after 60s.

while the same screen showed `[+] up 9/9`, every container Running, docker
itself reporting (healthy), and the apps' own logs serving
`GET /api/health HTTP/1.1" 200 OK` the whole time.

Nothing was wrong. In the integrated (SLOP) layout those console ports are
DELIBERATELY not published -- docker-compose.yml says so on the controller
service, "NOT the console (8800) ... publishing it would put a door beside the
front door", and slep/connect carry `expose:` alone. The gateway on 443 is the
only front door. So the probe spent 60s knocking on a door this layout is built
never to open, and then called a healthy app dead -- twice, for a minute each,
on every bring-up.

Driven against a STUBBED docker so the four states can be produced on demand.
The functions are lifted out of the real script by brace-matching: an earlier
harness used sed line ranges, silently failed to define _p_port, and reported a
FALSE PASS on the one case that mattered. The guard below is why that cannot
happen again.
"""
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CTL = ROOT / "deploy" / "sysiblectl"

WANT = ["_p_container", "_p_port", "_p_pretty", "_p_health_path",
        "_published_on_host", "_container_health",
        "_health_probe", "_health_verdict", "_health"]


def _extract(names):
    """Each function, header to its matching close brace."""
    src = CTL.read_text().splitlines(True)
    out = []
    for name in names:
        pat = re.compile(r"^%s\(\)\s*\{" % re.escape(name))
        for i, line in enumerate(src):
            if pat.match(line):
                depth, body = 0, []
                for l in src[i:]:
                    body.append(l)
                    depth += l.count("{") - l.count("}")
                    if depth <= 0:
                        break
                out.append("".join(body))
                break
        else:
            raise AssertionError(
                f"{name}() is not in {CTL.name} -- the probe no longer "
                f"distinguishes a published port from an unpublished one")
    return "".join(out)


DOCKER_STUB = """#!/bin/sh
case "$1 $2" in
  "inspect -f")
    case "$3" in
      *NetworkSettings.Ports*) printf '%s\\n' "$STUB_PORTS" ;;
      *State.Health*)          printf '%s\\n' "$STUB_HEALTH" ;;
      *)                       printf '\\n' ;;
    esac ;;
  *) exit 0 ;;
esac
"""


@pytest.fixture()
def health(tmp_path):
    (tmp_path / "bin").mkdir()
    stub = tmp_path / "bin" / "docker"
    stub.write_text(DOCKER_STUB)
    os.chmod(stub, 0o755)
    funcs = tmp_path / "funcs.sh"
    funcs.write_text(_extract(WANT))
    runner = tmp_path / "run.sh"
    runner.write_text(textwrap.dedent("""\
        set -u
        . "$FUNCS"
        _ok()   { printf 'OK %s\\n' "$*"; }
        _warn() { printf 'WARN %s\\n' "$*"; }
        _err()  { printf 'ERR %s\\n' "$*"; }
        # Refuse to report a verdict from a half-loaded harness. The previous
        # harness lost _p_port and still printed "healthy".
        [ "$(_p_port controller)" = 8800 ] || { echo "HARNESS BROKEN"; exit 99; }
        [ "$(_p_container controller)" != "" ] || { echo "HARNESS BROKEN"; exit 99; }
        if _health controller; then echo "VERDICT healthy"; else echo "VERDICT not-healthy"; fi
        """))

    def run(ports, hstate):
        r = subprocess.run(
            ["bash", str(runner)], capture_output=True, text=True, timeout=120,
            env={**os.environ, "PATH": f"{tmp_path/'bin'}:{os.environ['PATH']}",
                 "FUNCS": str(funcs), "STUB_PORTS": ports, "STUB_HEALTH": hstate})
        assert "HARNESS BROKEN" not in r.stdout, r.stdout
        assert r.returncode != 99, r.stdout
        return r.stdout

    return run


UNPUBLISHED = '{"8090/tcp":[{"HostPort":"8090"}],"9000/tcp":[{"HostPort":"9000"}],"8800/tcp":null}'
PUBLISHED = '{"8800/tcp":[{"HostPort":"8800"}]}'


def test_a_healthy_container_behind_the_gateway_is_healthy(health):
    """The reported case. 60s of waiting and an ERROR, for an app that was up."""
    out = health(UNPUBLISHED, "healthy")
    assert "VERDICT healthy" in out, out
    assert "behind the gateway" in out, (
        "it reported healthy without saying WHY the port is unreachable; the "
        f"operator is left thinking the probe got lucky:\n{out}")


def test_an_unhealthy_container_is_still_a_failure(health):
    """The fix must not turn the check into a rubber stamp."""
    assert "VERDICT not-healthy" in health(UNPUBLISHED, "unhealthy")


def test_a_starting_container_is_not_yet_healthy(health):
    """`_health_wait` polls this; 'starting' has to keep it waiting."""
    assert "VERDICT not-healthy" in health(UNPUBLISHED, "starting")


def test_a_published_port_still_takes_the_http_probe(health):
    """A split/standalone host publishes the console, and there the HTTP probe is
    the real signal. Taking docker's word there would report an app healthy whose
    port answers nothing -- the opposite error, and harder to spot."""
    out = health(PUBLISHED, "healthy")
    assert "VERDICT not-healthy" in out, (
        "a PUBLISHED port took the container-health shortcut; nothing is "
        f"listening in this sandbox, so the HTTP probe must fail:\n{out}")
    assert "behind the gateway" not in out, out


def test_a_container_with_no_healthcheck_falls_through(health):
    """No healthcheck defined -> behave exactly as before the change."""
    out = health(UNPUBLISHED, "none")
    assert "VERDICT not-healthy" in out, out
    assert "behind the gateway" not in out, out
