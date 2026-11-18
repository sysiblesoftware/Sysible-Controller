"""`status` has to answer the question an operator actually has.

Two reports, one terminal screenshot.

"I switched wifi networks... I can't reach slop." Every line of `sysiblectl
status all` was green, and every one of those checks was against 127.0.0.1.
That is the right probe for "is the service up" and the wrong one for the only
question that matters once the box has moved to a different network: what do I
type into a browser? A machine nothing can reach prints exactly the same page.
The box knows its own addresses; reading them out beats an operator guessing
from a bookmark that worked last week.

And, four lines under `container: exited`:

    ERROR:  controller -> Flashback: UNREACHABLE (URLError)

which is a true sentence about the network describing a container that is
switched off. The probe ran against a service that was not listening, failed
the only way it can, and reported it in a vocabulary that sends someone
hunting for a routing problem they do not have.

These drive the real bash functions with a fake ip/ss/docker on PATH.

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
# `ip -4 -o addr show scope global`, in the format the real one prints.
printf '%s\\n' "$FAKE_IP_LINES"
"""

FAKE_SS = """#!/bin/sh
printf '%s\\n' "$FAKE_SS_LINES"
"""

FAKE_DOCKER = """#!/bin/sh
echo "docker $*" >> "$FAKE_LOG"
case "$1 $2" in
  "inspect -f")
    case "$3" in
      *compose.project*)
        # Which compose project owns this container. $4 is the container.
        case "$4" in
          *gateway*) echo "${FAKE_PROJ_SLOP-slop}" ;;
          *)         eval "echo \\${FAKE_PROJ_$(echo "$4" | sed 's/^sysible-//')-slop}" ;;
        esac ;;
      *State.Status*)   echo "${FAKE_FB_STATE:-running}" ;;
      *State.ExitCode*) echo "${FAKE_FB_EXIT:-0}" ;;
      *Config.Env*)     printf '%s\\n' "$FAKE_ENV" ;;
      *)                echo "" ;;
    esac
    exit 0 ;;
esac
case "$1" in
  inspect) exit 0 ;;
  exec)    echo "${FAKE_PROBE:-200}"; exit 0 ;;
  ps)      echo "deadbeef"; exit 0 ;;
esac
exit 0
"""


@pytest.fixture
def sandbox(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("ip", FAKE_IP), ("ss", FAKE_SS), ("docker", FAKE_DOCKER)):
        p = bindir / name
        p.write_text(body)
        p.chmod(0o755)
    src = CTL.read_text(encoding="utf-8")
    lib = re.sub(r'^main "\$@"\s*$', "", src, flags=re.M)
    libp = tmp_path / "ctl.lib.sh"
    libp.write_text(lib, encoding="utf-8")
    return {"bin": str(bindir), "lib": str(libp), "log": str(tmp_path / "docker.log")}


def run(sandbox, snippet, keep_ip=True, **env):
    script = f'. "{sandbox["lib"]}"\n' + textwrap.dedent(snippet)
    path = sandbox["bin"] + ":" + os.environ.get("PATH", "/usr/bin:/bin")
    if not keep_ip:
        # A box with no iproute2 at all. The fakes live in their own directory,
        # so dropping it is how "the command is missing" is expressed.
        os.remove(os.path.join(sandbox["bin"], "ip"))
    e = {"PATH": path, "FAKE_LOG": sandbox["log"],
         "HOME": os.environ.get("HOME", "/root")}
    e.update({k: str(v) for k, v in env.items()})
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env=e, timeout=120)
    return r.returncode, r.stdout + r.stderr


LAN = ("2: ens160    inet 192.168.155.140/24 brd 192.168.155.255 scope global ens160\n"
       "3: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\n"
       "4: br-9f2    inet 172.18.0.1/16 scope global br-9f2")
OPEN_443 = "LISTEN 0 4096 0.0.0.0:443 0.0.0.0:*"
LOOPBACK_443 = "LISTEN 0 4096 127.0.0.1:443 0.0.0.0:*"


def addresses(sandbox, lines=LAN, ss=OPEN_443, **kw):
    return run(sandbox, "_addresses_status", FAKE_IP_LINES=lines, FAKE_SS_LINES=ss, **kw)


# ---- what to type into a browser -------------------------------------------
def test_it_prints_the_address_other_machines_would_use(sandbox):
    _rc, out = addresses(sandbox)
    assert "https://192.168.155.140/" in out, out


def test_it_does_not_offer_this_machines_own_bridges(sandbox):
    """docker0 and the compose bridge have addresses and are reachable from
    nowhere. Printing 172.17.0.1 as somewhere to point a browser is an
    afternoon."""
    _rc, out = addresses(sandbox)
    assert "172.17.0.1" not in out, out
    assert "172.18.0.1" not in out, out


def test_a_machine_with_no_address_is_told_so(sandbox):
    _rc, out = addresses(sandbox, lines="")
    assert "no address on any network" in out, out
    assert "192.168" not in out


def test_being_unable_to_look_is_not_the_same_as_finding_nothing(sandbox):
    """Without iproute2 this reported "no address on any network", which sends
    an operator to check an adapter that is working perfectly."""
    _rc, out = addresses(sandbox, keep_ip=False)
    assert "cannot tell" in out, out
    assert "no address on any network" not in out, out


def test_the_self_signed_certificate_is_mentioned_once(sandbox):
    """The first thing that happens when they paste the URL is a browser
    warning. Unexplained, that reads as "I still cannot reach it"."""
    _rc, out = addresses(sandbox)
    assert out.lower().count("self-signed") == 1, out


# ---- listening where it says it is -----------------------------------------
def test_a_port_open_on_every_interface_is_confirmed(sandbox):
    _rc, out = addresses(sandbox)
    assert "open on every interface" in out, out


def test_a_loopback_only_bind_is_caught(sandbox):
    """The failure that leaves every check above green: the console works in
    here and from nowhere else."""
    _rc, out = addresses(sandbox, ss=LOOPBACK_443)
    assert "bound to 127.0.0.1:443" in out, out
    assert "no other" in out


def test_nothing_listening_is_not_reported_as_a_bind_problem(sandbox):
    _rc, out = addresses(sandbox, ss="")
    assert "nothing is listening on port 443" in out, out


def test_a_non_default_port_appears_in_the_url(sandbox):
    _rc, out = run(sandbox, '_p_port(){ echo 8443; }\n_addresses_status',
                   FAKE_IP_LINES=LAN, FAKE_SS_LINES="LISTEN 0 4096 0.0.0.0:8443 0.0.0.0:*")
    assert "https://192.168.155.140:8443/" in out, out


def test_the_section_is_part_of_status(sandbox):
    """It is no use as a function nothing calls."""
    src = CTL.read_text(encoding="utf-8")
    m = re.search(r"^p_status\(\) \{.*?^\}", src, re.S | re.M)
    assert m, "p_status is gone or no longer in the expected shape"
    assert "_addresses_status" in m.group(0), \
        "status never says where the machine can be reached"


# ---- a stopped container is not a network failure --------------------------
def _flashback(sandbox, state, probe="URLError"):
    env = "SYSIBLE_FLASHBACK_AGENT_TOKEN=tok\nSYSIBLE_FLASHBACK_URL=http://host.docker.internal:8770"
    return run(sandbox, "_flashback_status",
               FAKE_FB_STATE=state, FAKE_FB_EXIT="1", FAKE_ENV=env, FAKE_PROBE=probe)


def test_a_stopped_flashback_is_reported_as_stopped(sandbox):
    _rc, out = _flashback(sandbox, "exited")
    assert "is not running" in out, out


def test_a_stopped_flashback_is_not_reported_as_unreachable(sandbox):
    """"UNREACHABLE (URLError)" under "container: exited" describes a switched
    off container in the vocabulary of a routing problem."""
    _rc, out = _flashback(sandbox, "exited")
    assert "UNREACHABLE" not in out, out


def test_a_stopped_flashback_says_how_to_start_it(sandbox):
    _rc, out = _flashback(sandbox, "exited")
    assert "slop start" in out, out


def test_the_reachability_probe_does_not_run_against_a_stopped_container(sandbox):
    """Probing a stopped container tells you it is stopped, slowly, in the
    wrong words. The probe is the exec carrying _FB_PROBE_URL -- the other
    execs in here read the version store and the poll times, and those are
    already guarded against coming back empty."""
    _flashback(sandbox, "exited")
    calls = Path(sandbox["log"]).read_text(encoding="utf-8")
    assert "_FB_PROBE_URL" not in calls, calls


def test_a_running_flashback_is_still_probed(sandbox):
    """The guard must not switch the check off for the case it exists for: a
    container that is up with the chain to it broken."""
    _rc, out = _flashback(sandbox, "running", probe="URLError")
    assert "UNREACHABLE" in out, out
    calls = Path(sandbox["log"]).read_text(encoding="utf-8")
    assert "_FB_PROBE_URL" in calls


def test_a_healthy_chain_still_reads_as_healthy(sandbox):
    _rc, out = _flashback(sandbox, "running", probe="200")
    assert "reachable" in out and "UNREACHABLE" not in out, out


# ---- an escape that is not an escape ---------------------------------------
def test_the_script_has_no_unicode_escapes_bash_will_not_read():
    r"""Third time in this codebase: \uXXXX in a context that does not escape.

    An octal \25B8 in a non-raw Python string, … in JSX TEXT, and then
    "—" in a bash double-quoted string -- which prints those six
    characters, because only $'...' and printf interpret them in shell. Each
    one looked right in the diff and wrong on the screen."""
    src = CTL.read_text(encoding="utf-8")
    bad = [ln for ln in src.splitlines()
           if re.search(r'(?<!\$)"[^"]*\\u[0-9a-fA-F]{4}', ln)]
    assert not bad, (
        "bash does not interpret \\uXXXX inside double quotes; write the "
        f"character itself or use $'...':\n" + "\n".join(bad))


# ---- two deployments, one set of names -------------------------------------
#
# The conflict an operator actually hit, verbatim:
#
#   Error response from daemon: Conflict. The container name
#   "/sysible-controller" is already in use by container "3db424fd..."
#
# The installer had left the box with both layouts. The running Controller
# belonged to its own standalone checkout, so the platform's copy could not
# start -- and for the days before it ever said "Conflict", every change
# shipped into the platform's apps/ was built into an image nothing ran, with
# status green on both deployments and nothing anywhere naming the cause.

def split(sandbox, **projects):
    env = {"FAKE_IP_LINES": LAN, "FAKE_SS_LINES": OPEN_443}
    env.update(projects)
    return run(sandbox, "_split_deployment_status", **env)


def test_an_app_from_its_own_checkout_is_named(sandbox):
    _rc, out = split(sandbox, FAKE_PROJ_controller="sysible-controller")
    assert "Two deployments on one host" in out, out
    assert "controller" in out, out


def test_it_says_what_the_consequence_is(sandbox):
    """Not "these differ" -- what it DOES: the platform's copy never runs, and
    every check stays green while it does not."""
    _rc, out = split(sandbox, FAKE_PROJ_controller="sysible-controller")
    assert "cannot start" in out, out
    assert "green" in out, out


def test_it_names_the_tool_that_moves_the_data(sandbox):
    """Telling someone to delete the container that holds the name is telling
    them to delete their enrolled hosts: the volumes are per-project too."""
    _rc, out = split(sandbox, FAKE_PROJ_controller="sysible-controller")
    assert "migrate-to-integrated" in out, out
    assert "--dry-run" in out, out


def test_all_three_apps_are_checked(sandbox):
    _rc, out = split(sandbox, FAKE_PROJ_slep="sysible-slep",
                     FAKE_PROJ_connect="sysible-connect")
    assert "slep" in out and "connect" in out, out


def test_one_deployment_says_nothing(sandbox):
    """It has to be silent on a healthy host, or it is noise on every status."""
    _rc, out = split(sandbox)
    assert out.strip() == "", out


def test_the_check_is_part_of_status():
    src = CTL.read_text(encoding="utf-8")
    m = re.search(r"^p_status\(\) \{.*?^\}", src, re.S | re.M)
    assert "_split_deployment_status" in m.group(0), \
        "status never notices two deployments fighting over one name"
