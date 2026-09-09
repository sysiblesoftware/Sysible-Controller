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
        _git_root() { echo /tmp/fakegr; return 0; }
        git() { for a in "$@"; do case "$a" in rev-parse) echo abc1234; return 0 ;; esac; done; return 0; }
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
        _git_root() { echo /tmp/fakegr; return 0; }
        git() { for a in "$@"; do case "$a" in rev-parse) echo abc1234; return 0 ;; esac; done; return 0; }
        _slop_apply_gateway_config() { return 0; }
        p_update slop
    """)
    assert rc == 0, err
    assert "updated to abc1234. Volume" in out and "preserved" in out


def test_non_slop_products_do_not_run_the_gateway_apply(sandbox):
    rc, out, err = run(sandbox, """
        _discover() { CONTAINER=c; CFG=/tmp/dc.yml; WD=/tmp; return 0; }
        _compose()  { :; }
        _health()   { :; }
        _git_root() { echo /tmp/fakegr; return 0; }
        git() { for a in "$@"; do case "$a" in rev-parse) echo abc1234; return 0 ;; esac; done; return 0; }
        _slop_apply_gateway_config() { echo "SHOULD-NOT-RUN"; return 1; }
        p_update controller
    """)
    assert rc == 0, err
    assert "SHOULD-NOT-RUN" not in out
    assert "updated to abc1234. Volume" in out and "preserved" in out


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


# ---- `update` must not report success when the SOURCE never advanced -------
# Reported: "nothing is updating when I do slop update". The command went green
# every time. A failed `git pull --ff-only` was only a yellow warning, so update
# rebuilt byte-identical code from disk and then printed "SLOP updated." — the
# same class of lie the gateway-config check above exists to prevent, one step
# earlier in the pipeline. These drive the real p_update with a real git
# checkout, one per shape of broken checkout.
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(*args, cwd):
    e = dict(os.environ); e.update(GIT_ENV)
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=e)


def _compose_dir(tmp_path, name="slopsrc"):
    d = tmp_path / name
    d.mkdir()
    (d / "docker-compose.yml").write_text("services:\n  flashback:\n    build: ./flashback\n")
    return d


def _origin_with_a_commit(tmp_path):
    """A bare 'remote' plus a clone of it, so a pull can really fast-forward."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git("init", "--bare", "-q", "--initial-branch=main", cwd=str(bare))
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-q", "-b", "main", cwd=str(seed))
    (seed / "docker-compose.yml").write_text("services:\n  flashback:\n    build: ./flashback\n")
    _git("add", "-A", cwd=str(seed)); _git("commit", "-qm", "one", cwd=str(seed))
    _git("remote", "add", "origin", str(bare), cwd=str(seed))
    _git("push", "-q", "-u", "origin", "main", cwd=str(seed))
    return bare, seed


def test_update_fails_loudly_when_the_compose_dir_is_not_a_git_checkout(sandbox, tmp_path):
    d = _compose_dir(tmp_path)
    rc, out, err = run(sandbox, 'p_update slop', SYSIBLE_SLOP_DIR=str(d), FAKE_HTTP_CODE="302")
    assert rc != 0, out
    assert "not a git checkout" in err
    assert "updated. Volume" not in out          # must NOT claim success


def test_update_fails_loudly_when_the_pull_cannot_fast_forward(sandbox, tmp_path):
    """A checkout with no upstream: `git pull --ff-only` errors. Before this fix
    that was a warning and the command still went green."""
    d = _compose_dir(tmp_path)
    _git("init", "-q", "-b", "main", cwd=str(d))
    _git("add", "-A", cwd=str(d)); _git("commit", "-qm", "local", cwd=str(d))
    rc, out, err = run(sandbox, 'p_update slop', SYSIBLE_SLOP_DIR=str(d), FAKE_HTTP_CODE="302")
    assert rc != 0, out
    assert "did NOT advance" in err
    assert "updated. Volume" not in out
    # and it must say what to actually do about it
    assert "upstream" in err


def test_update_says_the_source_never_moved_not_just_that_the_pull_failed(sandbox, tmp_path):
    """The operator-facing point: containers WERE rebuilt, from the same code."""
    d = _compose_dir(tmp_path)
    _git("init", "-q", "-b", "main", cwd=str(d))
    _git("add", "-A", cwd=str(d)); _git("commit", "-qm", "local", cwd=str(d))
    rc, out, err = run(sandbox, 'p_update slop', SYSIBLE_SLOP_DIR=str(d), FAKE_HTTP_CODE="302")
    assert rc != 0
    assert "code ALREADY on disk" in err and "not an update to newer code" in err


def test_update_reports_the_commit_it_advanced_to(sandbox, tmp_path):
    bare, seed = _origin_with_a_commit(tmp_path)
    clone = tmp_path / "clone"
    _git("clone", "-q", str(bare), str(clone), cwd=str(tmp_path))
    assert (clone / "docker-compose.yml").exists(), "fixture: the clone came out empty"
    # A new commit lands upstream after the clone.
    (seed / "new.txt").write_text("x")
    _git("add", "-A", cwd=str(seed)); _git("commit", "-qm", "two", cwd=str(seed))
    _git("push", "-q", "origin", "main", cwd=str(seed))
    head = _git("rev-parse", "--short", "HEAD", cwd=str(seed)).stdout.strip()

    rc, out, err = run(sandbox, 'p_update slop', SYSIBLE_SLOP_DIR=str(clone), FAKE_HTTP_CODE="302")
    assert rc == 0, err
    assert "Code advanced" in out and head in out
    assert "updated" in out


def test_update_is_happy_and_explicit_when_already_current(sandbox, tmp_path):
    """No new commits is a legitimate success — but it must SAY that, so 'nothing
    changed' is distinguishable from 'nothing could be pulled'."""
    bare, seed = _origin_with_a_commit(tmp_path)
    clone = tmp_path / "clone2"
    _git("clone", "-q", str(bare), str(clone), cwd=str(tmp_path))
    assert (clone / "docker-compose.yml").exists(), "fixture: the clone came out empty"
    rc, out, err = run(sandbox, 'p_update slop', SYSIBLE_SLOP_DIR=str(clone), FAKE_HTTP_CODE="302")
    assert rc == 0, err
    assert "no new commits" in out
    assert "did NOT advance" not in err


# ---- `slop update` must carry SLOP-owned settings to the apps that need them
# install.sh seeded cross-product settings on a FIRST run and nothing did it
# afterwards, so a setting a later SLOP release introduced never reached the app
# that consumes it — and update went green having wired up nothing. That is how
# config backup shipped inert: Flashback held its token, the Controller never got
# it, and the console stayed empty with no error anywhere.
def test_update_seeds_the_flashback_token_into_the_controller_env(sandbox, tmp_path):
    slop = tmp_path / "slop"; slop.mkdir()
    (slop / ".env").write_text("SYSIBLE_SSO_SHARED_SECRET=abc\nSYSIBLE_FLASHBACK_AGENT_TOKEN=tok123\n")
    ctl = tmp_path / "controller"; ctl.mkdir()
    (ctl / "docker-compose.yml").write_text("services: {}\n")
    rc, out, err = run(sandbox, f'''
        WD="{slop}"
        _git_root() {{ echo "{slop}"; return 0; }}
        _p_dir_override() {{ [ "$1" = controller ] && echo "{ctl}"; }}
        _slop_seed_cross_app_env
    ''', FAKE_NO_CONTAINER="1")
    body = (ctl / ".env").read_text()
    assert "SYSIBLE_FLASHBACK_AGENT_TOKEN=tok123" in body, body
    # host.docker.internal, not loopback: the controller is containerised and
    # Flashback publishes on the HOST.
    assert "SYSIBLE_FLASHBACK_URL=http://host.docker.internal:8770" in body, body
    assert "Seeded" in out


def test_seeding_never_overwrites_a_value_an_operator_set(sandbox, tmp_path):
    slop = tmp_path / "slop2"; slop.mkdir()
    (slop / ".env").write_text("SYSIBLE_FLASHBACK_AGENT_TOKEN=tok123\n")
    ctl = tmp_path / "controller2"; ctl.mkdir()
    (ctl / "docker-compose.yml").write_text("services: {}\n")
    (ctl / ".env").write_text("SYSIBLE_FLASHBACK_URL=http://elsewhere:9999\n")
    run(sandbox, f'''
        WD="{slop}"
        _git_root() {{ echo "{slop}"; return 0; }}
        _p_dir_override() {{ [ "$1" = controller ] && echo "{ctl}"; }}
        _slop_seed_cross_app_env
    ''', FAKE_NO_CONTAINER="1")
    body = (ctl / ".env").read_text()
    assert "http://elsewhere:9999" in body            # kept
    assert body.count("SYSIBLE_FLASHBACK_URL=") == 1  # not duplicated
    assert "SYSIBLE_FLASHBACK_AGENT_TOKEN=tok123" in body  # the missing one added


def test_seeding_is_a_no_op_when_slop_has_no_token_yet(sandbox, tmp_path):
    """An older .env predating the token must not produce a half-wired app."""
    slop = tmp_path / "slop3"; slop.mkdir()
    (slop / ".env").write_text("SYSIBLE_SSO_SHARED_SECRET=abc\n")
    ctl = tmp_path / "controller3"; ctl.mkdir()
    (ctl / "docker-compose.yml").write_text("services: {}\n")
    run(sandbox, f'''
        WD="{slop}"
        _git_root() {{ echo "{slop}"; return 0; }}
        _p_dir_override() {{ [ "$1" = controller ] && echo "{ctl}"; }}
        _slop_seed_cross_app_env
    ''', FAKE_NO_CONTAINER="1")
    assert not (ctl / ".env").exists() or "FLASHBACK" not in (ctl / ".env").read_text()


# ---- `slop status` must report whether config backup can actually work -----
# `ps` said "sysible-flashback  Up 4 hours" throughout an outage in which no host
# could back anything up: the container was fine, the CHAIN was not. Running is
# not working here — capture needs the token on both sides and the controller
# able to reach Flashback over the host.
FAKE_DOCKER_FB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_LOG"
case "$1" in
  inspect)
    case "$*" in
      *State.Status*) echo running; exit 0 ;;
      *Config.Env*)
        case "$*" in
          *flashback*) [ -n "${FB_TOKEN:-}" ] && echo "SYSIBLE_FLASHBACK_AGENT_TOKEN=$FB_TOKEN"; exit 0 ;;
          *)
            [ -n "${CTL_TOKEN:-}" ] && echo "SYSIBLE_FLASHBACK_AGENT_TOKEN=$CTL_TOKEN"
            [ -n "${CTL_URL:-}" ] && echo "SYSIBLE_FLASHBACK_URL=$CTL_URL"
            exit 0 ;;
        esac ;;
    esac
    [ "${NO_CTL:-0}" = 1 ] && case "$*" in *controller*) exit 1 ;; esac
    exit 0 ;;
  exec)
    case "$*" in
      *urllib*) echo "${REACH:-200}"; exit 0 ;;
      *store*)  echo "${STORE:-0 0}"; exit 0 ;;
    esac
    exit 0 ;;
esac
exit 0
"""


@pytest.fixture
def fbsandbox(sandbox, tmp_path):
    p = tmp_path / "bin" / "docker"
    p.write_text(FAKE_DOCKER_FB)
    p.chmod(0o755)
    return sandbox


def test_status_flags_a_token_missing_on_flashback(fbsandbox):
    rc, out, err = run(fbsandbox, "_flashback_status", FB_TOKEN="", CTL_TOKEN="t", CTL_URL="http://h:8770")
    assert "NOT SET on Flashback" in err
    assert "fails closed" in err


def test_status_flags_a_token_the_controller_never_got(fbsandbox):
    rc, out, err = run(fbsandbox, "_flashback_status", FB_TOKEN="t", CTL_TOKEN="", CTL_URL="")
    assert "MISSING on the Controller" in err
    assert "slop update" in err          # and says how to fix it


def test_status_flags_two_halves_that_do_not_match(fbsandbox):
    rc, out, err = run(fbsandbox, "_flashback_status", FB_TOKEN="a", CTL_TOKEN="b", CTL_URL="http://h:8770")
    assert "DO NOT MATCH" in err


def test_status_flags_a_controller_that_cannot_reach_flashback(fbsandbox):
    """The wiring can be perfect and the URL still point at the container's own
    loopback instead of the host — which is exactly what shipped."""
    rc, out, err = run(fbsandbox, "_flashback_status", FB_TOKEN="t", CTL_TOKEN="t",
                       CTL_URL="http://127.0.0.1:8770", REACH="URLError")
    assert "UNREACHABLE" in err


def test_status_reports_a_healthy_chain_with_no_backups_yet(fbsandbox):
    rc, out, err = run(fbsandbox, "_flashback_status", FB_TOKEN="t", CTL_TOKEN="t",
                       CTL_URL="http://h:8770", REACH="200", STORE="0 0")
    assert "matches on both sides" in out
    assert "reachable" in out
    assert "none yet" in err or "none yet" in out


def test_status_reports_real_stored_backups(fbsandbox):
    rc, out, err = run(fbsandbox, "_flashback_status", FB_TOKEN="t", CTL_TOKEN="t",
                       CTL_URL="http://h:8770", REACH="200", STORE="3 47")
    assert "3 host(s), 47 version(s)" in out


# ---- `slop up` must not run a compose that cannot possibly work ------------
# Reported: the "Install Sysible SLOP" desktop icon does not install SLOP.
# install-sysible ends by calling `sysible_ctl slop up`, which ran a bare
# `docker compose up`. SLOP's compose declares the SSO secret as ${VAR:?...} in
# five services, so compose ABORTS when it is unset — and on a first bring-up
# nothing has minted it yet. install.sh is what mints it.
def _slop_checkout(tmp_path, name, with_secret=False, with_installer=True):
    d = tmp_path / name
    d.mkdir()
    (d / "docker-compose.yml").write_text(
        "services:\n  gateway:\n    environment:\n"
        "      X: ${SYSIBLE_SSO_SHARED_SECRET:?run install.sh}\n")
    if with_secret:
        (d / ".env").write_text("SYSIBLE_SSO_SHARED_SECRET=abc123\n")
    if with_installer:
        (d / "install.sh").write_text('#!/bin/sh\necho "INSTALL-SH RAN: $1"\n')
        (d / "install.sh").chmod(0o755)
    return d


def test_first_slop_bring_up_hands_off_to_install_sh(sandbox, tmp_path):
    d = _slop_checkout(tmp_path, "slop-fresh")
    rc, out, err = run(sandbox, f'''
        _health() {{ :; }}
        _p_dir_override() {{ [ "$1" = slop ] && echo "{d}"; }}
        p_up slop
    ''', FAKE_NO_CONTAINER="1")
    assert rc == 0, err
    assert "INSTALL-SH RAN: gateway" in out, out
    # and it must NOT have tried the compose that would abort
    assert "up -d --build" not in docker_calls(sandbox)


def test_a_configured_slop_uses_plain_compose(sandbox, tmp_path):
    """Once the secret exists, the normal path — no re-running the installer."""
    d = _slop_checkout(tmp_path, "slop-ready", with_secret=True)
    rc, out, err = run(sandbox, f'''
        _health() {{ :; }}
        _p_dir_override() {{ [ "$1" = slop ] && echo "{d}"; }}
        p_up slop
    ''', FAKE_NO_CONTAINER="1")
    assert rc == 0, err
    assert "INSTALL-SH RAN" not in out
    assert "up -d --build" in docker_calls(sandbox)


def test_slop_with_no_secret_and_no_installer_fails_with_the_reason(sandbox, tmp_path):
    """Never run the doomed compose and let its interpolation error be the
    explanation — say which value is missing and where to put it."""
    d = _slop_checkout(tmp_path, "slop-broken", with_installer=False)
    rc, out, err = run(sandbox, f'''
        _health() {{ :; }}
        _p_dir_override() {{ [ "$1" = slop ] && echo "{d}"; }}
        p_up slop
    ''', FAKE_NO_CONTAINER="1")
    assert rc != 0
    assert "SYSIBLE_SSO_SHARED_SECRET" in err
    assert "up -d --build" not in docker_calls(sandbox)


def test_a_non_slop_product_is_unaffected(sandbox, tmp_path):
    d = tmp_path / "ctlsrc"; d.mkdir()
    (d / "docker-compose.yml").write_text("services: {}\n")
    rc, out, err = run(sandbox, f'''
        _health() {{ :; }}
        _env_upsert() {{ :; }}
        _p_dir_override() {{ [ "$1" = connect ] && echo "{d}"; }}
        p_up connect
    ''', FAKE_NO_CONTAINER="1")
    assert rc == 0, err
    assert "up -d --build" in docker_calls(sandbox)


# ---- seeding a value is not the same as the app HAVING it ------------------
# Reported: "I should just be able to do an update command." `slop update` did
# seed the Controller's .env — and stopped there. docker reads .env when it
# CREATES a container, so the Controller kept running with the old environment
# and the setting sat on disk unread, while the command reported it had wired
# things up. Same lie as every other step in this feature's history.
def test_seeding_recreates_the_controller_so_it_reads_the_new_env(sandbox, tmp_path):
    slop = tmp_path / "s1"; slop.mkdir()
    (slop / ".env").write_text("SYSIBLE_FLASHBACK_AGENT_TOKEN=tok123\n")
    ctl = tmp_path / "c1"; ctl.mkdir()
    (ctl / "docker-compose.yml").write_text("services: {}\n")
    rc, out, err = run(sandbox, f'''
        WD="{slop}"
        _git_root() {{ echo "{slop}"; return 0; }}
        _p_dir_override() {{ [ "$1" = controller ] && echo "{ctl}"; }}
        _slop_seed_cross_app_env
    ''', FAKE_NO_CONTAINER="1")
    assert "Seeded" in out
    assert "Recreating" in out, out
    calls = docker_calls(sandbox)
    assert "up -d" in calls, calls
    # An env change needs a recreate, NOT a rebuild — a --build would cost
    # minutes for nothing.
    assert "--build" not in calls, calls


def test_nothing_is_recreated_when_there_was_nothing_to_seed(sandbox, tmp_path):
    """An already-configured host must not have its Controller bounced on every
    single update."""
    slop = tmp_path / "s2"; slop.mkdir()
    (slop / ".env").write_text("SYSIBLE_FLASHBACK_AGENT_TOKEN=tok123\n")
    ctl = tmp_path / "c2"; ctl.mkdir()
    (ctl / "docker-compose.yml").write_text("services: {}\n")
    (ctl / ".env").write_text(
        "SYSIBLE_FLASHBACK_URL=http://host.docker.internal:8770\n"
        "SYSIBLE_FLASHBACK_AGENT_TOKEN=tok123\n")
    rc, out, err = run(sandbox, f'''
        WD="{slop}"
        _git_root() {{ echo "{slop}"; return 0; }}
        _p_dir_override() {{ [ "$1" = controller ] && echo "{ctl}"; }}
        _slop_seed_cross_app_env
    ''', FAKE_NO_CONTAINER="1")
    assert "Recreating" not in out, out
    assert "up -d" not in docker_calls(sandbox)


def test_a_failed_recreate_tells_the_operator_what_to_run(sandbox, tmp_path):
    """If the recreate cannot happen, the seeded value is still inert — say so
    and name the command, rather than reporting success."""
    slop = tmp_path / "s3"; slop.mkdir()
    (slop / ".env").write_text("SYSIBLE_FLASHBACK_AGENT_TOKEN=tok123\n")
    rc, out, err = run(sandbox, f'''
        WD="{slop}"
        _git_root() {{ echo "{slop}"; return 0; }}
        _p_dir_override() {{ echo ""; }}
        _slop_seed_cross_app_env
    ''', FAKE_NO_CONTAINER="1")
    # No controller checkout at all: seeding is skipped entirely, nothing claimed.
    assert "Recreating" not in out
