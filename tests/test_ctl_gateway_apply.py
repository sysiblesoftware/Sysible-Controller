"""`sysible_ctl slop update` must not claim success when the gateway config never loaded.

The gateway's Caddyfile IS the authentication boundary — forward_auth plus the
401 -> /login deny path — and it is bind-mounted, so `compose up -d --build`
leaves the running Caddy on the old file. Applying it is a separate step, and
that step used to fail silently:

    if docker exec "$gwc" caddy reload ... >/dev/null 2>&1; then ...
    else
      _warn "caddy reload failed — restarting the gateway"
      docker restart "$gwc" >/dev/null 2>&1 || true     # <- swallowed
    fi
    ...
    _ok "Sysible SLOP gateway updated."                 # <- printed anyway

That is how a gateway kept serving a pre-fix config through an "update" that
reported success; `ps` showed "Up 19 hours" immediately after the supposed
restart, and a restart resets that clock, so the container had never come back.

These tests drive the real bash functions with a fake docker/curl on PATH, one
per failure mode, and assert the command FAILS loudly instead.
"""
import os
import re
import shutil
import subprocess
import textwrap

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
CTL = os.path.join(os.path.dirname(HERE), "deploy", "sysible_ctl")

FAKE_DOCKER = r"""#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_LOG"
case "$1" in
  inspect)
    [ "${FAKE_NO_CONTAINER:-0}" = 1 ] && exit 1
    exit 0 ;;
  exec)
    case "$*" in
      *validate*)
        if [ "${FAKE_VALIDATE_FAIL:-0}" = 1 ]; then
          echo 'Caddyfile:88: unrecognized directive: bogus_directive' >&2; exit 1
        fi
        exit 0 ;;
      *reload*)
        if [ "${FAKE_RELOAD_FAIL:-0}" = 1 ]; then
          echo 'caddy: sending configuration to instance: Post "http://localhost:2019/load": dial tcp 127.0.0.1:2019: connect: connection refused' >&2
          exit 1
        fi
        exit 0 ;;
    esac
    exit 0 ;;
  restart)
    if [ "${FAKE_RESTART_FAIL:-0}" = 1 ]; then
      echo 'Error response from daemon: cannot restart container: permission denied' >&2; exit 1
    fi
    exit 0 ;;
esac
exit 0
"""

FAKE_CURL = r"""#!/bin/sh
printf '%s' "${FAKE_HTTP_CODE:-000}"
exit 0
"""

# So the retry loop doesn't really wait 15 seconds in the inconclusive cases.
FAKE_SLEEP = "#!/bin/sh\nexit 0\n"


@pytest.fixture
def sandbox(tmp_path):
    """A PATH with fake docker/curl/sleep, and the ctl script with `main` stripped
    so the functions can be sourced and called individually."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL), ("sleep", FAKE_SLEEP)):
        p = bindir / name
        p.write_text(body)
        p.chmod(0o755)

    src = open(CTL, encoding="utf-8").read()
    # Drop the trailing dispatch so sourcing doesn't run the CLI.
    lib = re.sub(r'^main "\$@"\s*$', "", src, flags=re.M)
    libp = tmp_path / "ctl.lib.sh"
    libp.write_text(lib)
    return {"bin": str(bindir), "lib": str(libp), "log": str(tmp_path / "docker.log")}


def run(sandbox, snippet: str, **env):
    """Source the ctl functions and run `snippet`; return (rc, stdout, stderr)."""
    script = f'. "{sandbox["lib"]}"\n' + textwrap.dedent(snippet)
    e = {
        # A minimal PATH: our fakes first, then the real tools bash needs (sed, seq).
        "PATH": sandbox["bin"] + ":" + os.environ.get("PATH", "/usr/bin:/bin"),
        "FAKE_LOG": sandbox["log"],
        "HOME": os.environ.get("HOME", "/root"),
    }
    e.update({k: str(v) for k, v in env.items()})
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=e, timeout=120)
    return r.returncode, r.stdout, r.stderr


def docker_calls(sandbox):
    try:
        return open(sandbox["log"], encoding="utf-8").read()
    except FileNotFoundError:
        return ""


# ---- the happy path still works -------------------------------------------
def test_a_successful_reload_is_verified_against_the_running_gateway(sandbox):
    rc, out, err = run(sandbox, "_slop_apply_gateway_config", FAKE_HTTP_CODE="302")
    assert rc == 0, err
    assert "gateway config reloaded" in out
    # Not just "reload said OK" — the deny path was probed.
    assert "verified" in out and "redirected to sign-in" in out


def test_the_config_is_validated_before_the_running_server_is_touched(sandbox):
    """Caddy exits on an invalid config, so restarting into one takes the gateway
    DOWN. Refuse, keep serving the old config, and report it."""
    rc, out, err = run(sandbox, "_slop_apply_gateway_config",
                       FAKE_VALIDATE_FAIL="1", FAKE_HTTP_CODE="302")
    assert rc == 1
    assert "INVALID" in err
    assert "unrecognized directive: bogus_directive" in err, "the real caddy error must be shown"
    calls = docker_calls(sandbox)
    assert "validate" in calls
    assert "reload" not in calls, "a config that failed validation must never be loaded"
    assert "restart" not in calls, "and must never trigger a restart into a crash loop"


# ---- the failure modes that used to be silent -----------------------------
def test_a_failed_reload_falls_back_to_a_restart_and_says_why(sandbox):
    rc, out, err = run(sandbox, "_slop_apply_gateway_config",
                       FAKE_RELOAD_FAIL="1", FAKE_HTTP_CODE="302")
    assert rc == 0, err
    assert "caddy reload failed" in err
    # The reload error used to go to /dev/null, leaving nothing to diagnose.
    assert "connection refused" in err
    assert "gateway restarted" in out
    assert "restart" in docker_calls(sandbox)


def test_reload_and_restart_both_failing_is_a_hard_error(sandbox):
    """THE regression. Both paths failed and `|| true` hid it, so the operator was
    told the update succeeded while the old config kept serving traffic."""
    rc, out, err = run(sandbox, "_slop_apply_gateway_config",
                       FAKE_RELOAD_FAIL="1", FAKE_RESTART_FAIL="1", FAKE_HTTP_CODE="302")
    assert rc == 1, "a gateway that never picked up the new config must fail the update"
    assert "not live" in err.lower()
    assert "cannot restart container" in err, "docker's own error must reach the operator"


def test_a_gateway_that_serves_anonymous_requests_fails_the_update(sandbox):
    """200 for an unauthenticated portal request means the forward_auth deny path
    is a no-op — the exact bypass that shipped once via a missing `*` matcher."""
    rc, out, err = run(sandbox, "_slop_apply_gateway_config", FAKE_HTTP_CODE="200")
    assert rc == 1
    assert "UNAUTHENTICATED" in err
    assert "handle_response" in err, "point the operator at the cause"


def test_an_unreachable_gateway_is_reported_as_unverified_not_as_success(sandbox):
    rc, out, err = run(sandbox, "_slop_apply_gateway_config", FAKE_HTTP_CODE="502")
    assert rc == 0, "inconclusive is not proof of a bypass — don't fail the update"
    assert "could not verify" in err
    assert "502" in err


def test_a_missing_gateway_container_is_skipped_with_a_hint(sandbox):
    rc, out, err = run(sandbox, "_slop_apply_gateway_config", FAKE_NO_CONTAINER="1")
    assert rc == 0
    assert "no gateway container" in err
    assert "SYSIBLE_SLOP_CONTAINER" in err
    assert "exec" not in docker_calls(sandbox)


def test_the_container_name_override_is_honoured(sandbox):
    run(sandbox, "_slop_apply_gateway_config",
        SYSIBLE_SLOP_CONTAINER="my-gw", FAKE_HTTP_CODE="302")
    assert "my-gw" in docker_calls(sandbox)


# ---- p_update must not print "updated" over a failed apply -----------------
def test_p_update_reports_failure_when_the_gateway_config_did_not_apply(sandbox):
    rc, out, err = run(sandbox, """
        _discover() { CONTAINER=gw; CFG=/tmp/dc.yml; WD=/tmp; return 0; }
        _compose()  { :; }
        _health()   { :; }
        _git_root() { return 1; }
        _slop_apply_gateway_config() { return 1; }
        p_update slop
    """)
    assert rc == 1
    assert "updated. Volume" not in out, "must not claim success"
    assert "was NOT applied" in err


def test_p_update_still_succeeds_for_slop_when_the_config_applies(sandbox):
    rc, out, err = run(sandbox, """
        _discover() { CONTAINER=gw; CFG=/tmp/dc.yml; WD=/tmp; return 0; }
        _compose()  { :; }
        _health()   { :; }
        _git_root() { return 1; }
        _slop_apply_gateway_config() { return 0; }
        p_update slop
    """)
    assert rc == 0, err
    assert "updated. Volume" in out


def test_non_slop_products_do_not_run_the_gateway_apply(sandbox):
    rc, out, err = run(sandbox, """
        _discover() { CONTAINER=c; CFG=/tmp/dc.yml; WD=/tmp; return 0; }
        _compose()  { :; }
        _health()   { :; }
        _git_root() { return 1; }
        _slop_apply_gateway_config() { echo "SHOULD-NOT-RUN"; return 1; }
        p_update controller
    """)
    assert rc == 0, err
    assert "SHOULD-NOT-RUN" not in out
    assert "updated. Volume" in out


# ---- source-level guard ---------------------------------------------------
def test_no_step_in_the_gateway_apply_path_is_swallowed():
    """Lint the shape, so the `|| true` cannot creep back in."""
    src = open(CTL, encoding="utf-8").read()
    fn = src[src.index("_slop_apply_gateway_config() {"):]
    fn = fn[:fn.index("\n}\n") + 3]
    assert "|| true" not in fn, "an ignored exit status here is what hid the failure"
    for cmd in ("caddy validate", "caddy reload", "docker restart"):
        assert cmd in fn
    # `docker inspect` may stay quiet — it is a presence probe whose output is
    # noise and whose exit status IS checked. The three commands that can explain
    # a failure must not be silenced.
    for line in fn.splitlines():
        if any(c in line for c in ("caddy validate", "caddy reload", "docker restart")):
            assert ">/dev/null" not in line, \
                f"the error output IS the diagnosis — capture it, don't discard it: {line.strip()}"
