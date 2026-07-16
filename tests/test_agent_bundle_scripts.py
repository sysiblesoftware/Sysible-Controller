"""
The agent bundle ships two lifecycle scripts alongside run_agent.sh:

  - disenroll_agent.sh   removes the agent from a host AND tells the controller to
                         drop the enrollment. Its controller-notify step delegates to
                         `agent.py --disenroll` so it reuses the agent's own connection
                         path (pinned cert, controller failover) instead of a hand-rolled
                         POST that couldn't reach a controller only reachable indirectly.
  - migrate_agent.sh     repoints an installed agent at a different controller (IP change,
                         DNS cutover, failover) without reinstalling.

These tests lock in that both are present, executable, valid bash, and wired the way
the agent expects.
"""
import io
import shutil
import subprocess
import zipfile

import pytest

from backend import agent_bundle


def _bundle():
    _, data = agent_bundle.build_agent_bundle(["127.0.0.1"], 9000, "tok-abc")
    return zipfile.ZipFile(io.BytesIO(data))


def _is_exec(zi):
    # External attr stores unix mode in the high 16 bits; owner-exec bit = 0o100.
    return bool((zi.external_attr >> 16) & 0o100)


def test_bundle_ships_both_lifecycle_scripts_executable():
    z = _bundle()
    names = z.namelist()
    for script in ("disenroll_agent.sh", "migrate_agent.sh"):
        assert script in names, f"{script} missing from bundle"
        assert _is_exec(z.getinfo(script)), f"{script} is not marked executable"


@pytest.mark.skipif(not shutil.which("bash"), reason="bash not available")
@pytest.mark.parametrize("script", ["disenroll_agent.sh", "migrate_agent.sh", "run_agent.sh"])
def test_generated_scripts_are_valid_bash(script):
    z = _bundle()
    src = z.read(script).decode()
    p = subprocess.run(["bash", "-n"], input=src, text=True, capture_output=True)
    assert p.returncode == 0, f"{script} failed bash -n:\n{p.stderr}"


def test_disenroll_notifies_via_the_agent():
    # The notify step must call the agent's --disenroll mode (reuses the agent's own
    # connection path), not embed a plain requests.post that can't route indirectly.
    src = _bundle().read("disenroll_agent.sh").decode()
    assert '"$AGENT_PY" --disenroll' in src    # delegates to the agent's own mode
    assert 'AGENT_PY="/opt/sysible-agent/agent.py"' in src
    assert "requests.post" not in src          # the old hand-rolled POST is gone


def test_migrate_repoints_controller_and_restarts():
    src = _bundle().read("migrate_agent.sh").decode()
    assert "SYSIBLE_CONTROLLER" in src         # rewrites the controller address
    assert "--token" in src                    # supports a genuinely separate controller
    assert "systemctl restart sysible-agent.service" in src
    # A no-token migration must KEEP local state (same-DB failover re-attaches);
    # state is only cleared when a fresh token is supplied.
    assert 'rm -f "$STATE_FILE"' in src


def test_agent_source_has_disenroll_mode():
    # The bundled agent.py must actually implement what disenroll_agent.sh invokes.
    src = _bundle().read("agent.py").decode()
    assert "def disenroll_self" in src
    assert '"--disenroll" in sys.argv' in src
