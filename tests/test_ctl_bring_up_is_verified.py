"""A bring-up that cannot serve its own port is a FAILED bring-up.

`sysible_ctl <p> up` ran compose in a subshell whose exit status was discarded,
probed the port once with a 5-second timeout, threw that result away with
`|| true`, and then printed "is up." no matter what. install.sh reads only the
exit code, so it printed "SLOP gateway is up." over a dead front door.

What the operator saw was the two lines in the wrong order and neither of them
actionable:

    health: '' (no response on 443 — may still be starting)
    Sysible SLOP gateway is up.

A rebuilt stack legitimately needs more than five seconds, so the probe now
waits; and when it still does not answer, the container's status and last lines
are the diagnosis rather than a shrug.
"""
import os
import re
import subprocess
import textwrap

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
CTL = os.path.join(os.path.dirname(HERE), "deploy", "sysible_ctl")

FAKE_DOCKER = r"""#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_LOG"
case "$1" in
  inspect) [ "${FAKE_NO_CONTAINER:-0}" = 1 ] && exit 1; exit 0 ;;
  ps)      echo "    sysible-slop-gateway  Exited (1) 3 seconds ago"; exit 0 ;;
  logs)    echo "run: adapting config using caddyfile: Caddyfile:12 - unrecognized directive"; exit 0 ;;
  exec)    exit 0 ;;
  restart) exit 0 ;;
esac
exit 0
"""

# _health probes with -f and no -w; the deny-path probe uses -w '%{http_code}'.
FAKE_CURL = r"""#!/bin/sh
case "$*" in
  *-w*) printf '%s' "${FAKE_HTTP_CODE:-302}"; exit 0 ;;
esac
[ "${FAKE_HEALTH_FAIL:-0}" = 1 ] && exit 7
exit 0
"""

FAKE_SLEEP = "#!/bin/sh\nexit 0\n"


@pytest.fixture
def sandbox(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL), ("sleep", FAKE_SLEEP)):
        p = bindir / name
        p.write_text(body)
        p.chmod(0o755)
    src = open(CTL, encoding="utf-8").read()
    lib = re.sub(r'^main "\$@"\s*$', "", src, flags=re.M)
    libp = tmp_path / "ctl.lib.sh"
    libp.write_text(lib)
    return {"bin": str(bindir), "lib": str(libp), "log": str(tmp_path / "docker.log")}


def run(sandbox, snippet, **env):
    script = f'. "{sandbox["lib"]}"\n' + textwrap.dedent(snippet)
    e = {"PATH": sandbox["bin"] + ":" + os.environ.get("PATH", "/usr/bin:/bin"),
         "FAKE_LOG": sandbox["log"], "HOME": os.environ.get("HOME", "/root")}
    e.update({k: str(v) for k, v in env.items()})
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env=e, timeout=120)
    return r.returncode, r.stdout, r.stderr


# ---- the probe now waits, and reports ---------------------------------------
def test_a_port_that_answers_is_reported_ok(sandbox):
    rc, out, err = run(sandbox, "_health_wait slop 4")
    assert rc == 0, err
    assert "health: OK" in out


def test_a_port_that_never_answers_fails_instead_of_warning(sandbox):
    """This is the whole bug. It used to warn and carry on."""
    rc, out, err = run(sandbox, "_health_wait slop 4", FAKE_HEALTH_FAIL="1")
    assert rc == 1
    assert "NOT answering on port 443" in err


def test_the_failure_carries_the_container_status_and_its_last_words(sandbox):
    """"No response on 443" on its own is not something anyone can act on."""
    rc, out, err = run(sandbox, "_health_wait slop 4", FAKE_HEALTH_FAIL="1")
    both = out + err
    assert "Exited (1)" in both, "the container's status was not shown"
    assert "unrecognized directive" in both, "the container's own error was not shown"


def test_a_missing_container_says_the_stack_never_started(sandbox):
    rc, out, err = run(sandbox, "_health_wait slop 4",
                       FAKE_HEALTH_FAIL="1", FAKE_NO_CONTAINER="1")
    assert rc == 1
    assert "no container named" in err
    assert "did not start" in err


def test_it_keeps_trying_rather_than_giving_up_after_one_probe(sandbox):
    """A stack that was just rebuilt is legitimately slow to answer; a single
    5-second probe turned that into a reported failure (or, worse, a warning
    followed by 'is up.')."""
    rc, out, err = run(sandbox, """
        attempts=0
        _health() {                       # answers only on the third look
          attempts=$((attempts + 1))
          [ "$attempts" -ge 3 ] && return 0
          return 1
        }
        _health_wait slop 20
        echo "attempts=$attempts"
    """)
    assert rc == 0, err
    assert "attempts=3" in out


# ---- and "is up." is no longer unconditional --------------------------------
def test_up_refuses_to_announce_a_product_that_is_not_answering(sandbox):
    rc, out, err = run(sandbox, """
        _p_dir_override() { echo /nope; }
        _compose_file_in() { echo /nope/docker-compose.yml; }
        _detect_compose() { DC=(true); }
        _slop_secret_present() { return 0; }
        _slop_seed_cross_app_env() { :; }
        _slop_apply_gateway_config() { return 0; }
        p_up slop
    """, FAKE_HEALTH_FAIL="1")
    assert rc != 0, "a dead gateway was reported as a successful bring-up"
    assert "is up." not in out


def test_up_fails_when_compose_itself_fails(sandbox):
    """The subshell's exit status used to be discarded entirely."""
    rc, out, err = run(sandbox, """
        _p_dir_override() { echo /nope; }
        _compose_file_in() { echo /nope/docker-compose.yml; }
        _detect_compose() { DC=(false); }
        _slop_secret_present() { return 0; }
        p_up slop
    """)
    assert rc != 0
    assert "compose failed to build or start it" in err
    assert "is up." not in out


def test_up_applies_the_gateway_config_like_update_does(sandbox):
    """`up --build` leaves the bind-mounted Caddyfile unloaded, so a config that
    arrived with a git pull sat on disk while the front door served the old one.
    `update` always knew that; `up` did not."""
    rc, out, err = run(sandbox, """
        _p_dir_override() { echo /nope; }
        _compose_file_in() { echo /nope/docker-compose.yml; }
        _detect_compose() { DC=(true); }
        _slop_secret_present() { return 0; }
        _slop_seed_cross_app_env() { echo SEEDED; }
        _slop_apply_gateway_config() { echo APPLIED; return 0; }
        p_up slop
    """)
    assert rc == 0, err
    assert "SEEDED" in out and "APPLIED" in out


def test_up_fails_loudly_when_the_gateway_config_will_not_apply(sandbox):
    rc, out, err = run(sandbox, """
        _p_dir_override() { echo /nope; }
        _compose_file_in() { echo /nope/docker-compose.yml; }
        _detect_compose() { DC=(true); }
        _slop_secret_present() { return 0; }
        _slop_seed_cross_app_env() { :; }
        _slop_apply_gateway_config() { return 1; }
        p_up slop
    """)
    assert rc != 0
    assert "gateway config was NOT applied" in err
    assert "is up." not in out


def test_a_non_slop_product_is_untouched_by_the_gateway_step(sandbox):
    rc, out, err = run(sandbox, """
        _p_dir_override() { echo /nope; }
        _compose_file_in() { echo /nope/docker-compose.yml; }
        _detect_compose() { DC=(true); }
        _slop_apply_gateway_config() { echo SHOULD_NOT_RUN; return 1; }
        p_up connect
    """)
    assert "SHOULD_NOT_RUN" not in out
    assert rc == 0, err
