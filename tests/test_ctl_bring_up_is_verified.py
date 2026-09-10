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
  inspect)
    [ "${FAKE_NO_CONTAINER:-0}" = 1 ] && exit 1
    case "$*" in *StartedAt*) printf '%s' "${FAKE_STARTED:-2000-01-01T00:00:00Z}" ;; esac
    exit 0 ;;
  ps)      echo "    sysible-slop-gateway  Exited (1) 3 seconds ago"; exit 0 ;;
  logs)    echo "run: adapting config using caddyfile: Caddyfile:12 - unrecognized directive"; exit 0 ;;
  exec)
    case "$*" in
      *reload*)
        if [ "${FAKE_RELOAD_FAIL:-0}" = 1 ]; then
          echo 'caddy: sending configuration to instance: performing request: Post "http://localhost:2019/load": dial tcp [::1]:2019: connect: connection refused' >&2
          exit 1
        fi ;;
    esac
    exit 0 ;;
  restart) exit 0 ;;
esac
exit 0
"""

# Mimics real curl closely enough to matter: with -w it prints the status and
# exits 0 EVEN FOR 4xx/5xx, and when it never got an HTTP response at all it
# prints 000 and exits non-zero. Every invocation is logged so a test can assert
# what was actually probed.
FAKE_CURL = r"""#!/bin/sh
[ -n "$FAKE_CURL_LOG" ] && printf '%s\n' "$*" >> "$FAKE_CURL_LOG"
if [ "${FAKE_HEALTH_FAIL:-0}" = 1 ]; then printf '000'; exit 7; fi
case "$*" in
  *-w*) printf '%s' "${FAKE_HTTP_CODE:-302}"; exit 0 ;;
esac
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
    return {"bin": str(bindir), "lib": str(libp), "log": str(tmp_path / "docker.log"),
            "curl_log": str(tmp_path / "curl.log")}


def run(sandbox, snippet, **env):
    script = f'. "{sandbox["lib"]}"\n' + textwrap.dedent(snippet)
    e = {"PATH": sandbox["bin"] + ":" + os.environ.get("PATH", "/usr/bin:/bin"),
         "FAKE_LOG": sandbox["log"], "HOME": os.environ.get("HOME", "/root"),
         "FAKE_CURL_LOG": sandbox["curl_log"]}
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


# ---------------------------------------------------------------------------
# A FAILED UPDATE MUST LEAVE THE RUNNING SYSTEM ALONE.
#
# From a real host that had lost DNS: every `docker build` died at its FROM line
# ("failed to resolve source metadata ... Temporary failure in name resolution"),
# and `p_update` — which discarded the compose exit status — carried straight on
# to _slop_apply_gateway_config and restarted the gateway. The update brought in
# nothing and took :443 down with it. The machine was fine before the command ran.
# ---------------------------------------------------------------------------
def _update_stub(compose_ok=True, config_changed=True, extra=""):
    """p_update with its discovery, git and reporting stubbed — so the test drives
    the real ordering decisions and nothing else."""
    return f"""
        _discover() {{ WD=/nope; CFG=/nope/docker-compose.yml; CONTAINER=c; return 0; }}
        _git_root() {{ echo /nope; }}
        git() {{ return 0; }}
        _compose() {{ {"return 0" if compose_ok else "return 1"}; }}
        _slop_seed_cross_app_env() {{ :; }}
        _slop_caddyfile_is_newer_than_gateway() {{ {"return 0" if config_changed else "return 1"}; }}
        _slop_apply_gateway_config() {{ echo GATEWAY_TOUCHED; return 0; }}
        _health_wait() {{ return 0; }}
        {extra}
        p_update slop
    """


def test_a_failed_build_never_reaches_the_gateway(sandbox):
    rc, out, err = run(sandbox, _update_stub(compose_ok=False))
    assert rc != 0
    assert "GATEWAY_TOUCHED" not in out, "a failed update restarted the gateway"
    assert "build/recreate FAILED" in err
    assert "still serving what it was" in err


def test_a_failed_build_says_the_running_system_was_left_alone(sandbox):
    _, out, err = run(sandbox, _update_stub(compose_ok=False))
    assert "Nothing was changed on the running system" in err


def test_an_unchanged_config_does_not_restart_the_gateway(sandbox):
    """The Caddyfile is bind-mounted and the admin API is off, so 'apply' means
    'restart'. Re-applying the file Caddy already loaded is a self-inflicted
    outage window for no change at all."""
    rc, out, err = run(sandbox, _update_stub(config_changed=False))
    assert "GATEWAY_TOUCHED" not in out
    assert "leaving it alone" in out


def test_a_changed_config_is_still_applied(sandbox):
    """The case the apply step exists for must keep working."""
    rc, out, err = run(sandbox, _update_stub(config_changed=True))
    assert "GATEWAY_TOUCHED" in out, "a genuine config change was never applied"


def test_a_hand_edited_caddyfile_counts_as_changed(sandbox, tmp_path):
    """The decision is about the FILE and the container, not about git — an
    operator who edits the config on the host must still get it loaded."""
    gw = tmp_path / "gateway"
    gw.mkdir()
    (gw / "Caddyfile").write_text(":443 { respond 204 }\n")
    rc, out, err = run(sandbox, f"""
        _p_container() {{ echo gw; }}
        _git_root() {{ echo {tmp_path}; }}
        WD={tmp_path}
        if _slop_caddyfile_is_newer_than_gateway; then echo NEWER; else echo SAME; fi
    """, FAKE_STARTED="1970-01-01T00:00:00Z")
    assert "NEWER" in out


def test_a_config_older_than_the_gateway_is_left_alone(sandbox, tmp_path):
    gw = tmp_path / "gateway"
    gw.mkdir()
    (gw / "Caddyfile").write_text(":443 { respond 204 }\n")
    rc, out, err = run(sandbox, f"""
        _p_container() {{ echo gw; }}
        _git_root() {{ echo {tmp_path}; }}
        WD={tmp_path}
        if _slop_caddyfile_is_newer_than_gateway; then echo NEWER; else echo SAME; fi
    """, FAKE_STARTED="2999-01-01T00:00:00Z")
    assert "SAME" in out


def test_an_unknowable_state_applies_rather_than_skips(sandbox, tmp_path):
    """Failing to apply a config that DID change is the worse of the two
    mistakes, so anything unknown answers yes."""
    rc, out, err = run(sandbox, f"""
        _p_container() {{ echo gw; }}
        _git_root() {{ echo {tmp_path}; }}
        WD={tmp_path}
        if _slop_caddyfile_is_newer_than_gateway; then echo NEWER; else echo SAME; fi
    """)
    assert "NEWER" in out, "no Caddyfile at all should not silently skip the apply"


# ---- naming the fault the operator can actually see -------------------------
def test_a_dns_failure_is_called_a_dns_failure(sandbox):
    """It used to answer an unreachable github.com with three checkout problems —
    upstream, local edits, detached HEAD — none of which were wrong."""
    rc, out, err = run(sandbox, """
        _discover() { WD=/nope; CFG=/nope/dc.yml; CONTAINER=c; return 0; }
        _git_root() { echo /nope; }
        git() { case "$*" in *pull*) echo "fatal: unable to access: Could not resolve host: github.com" >&2; return 1 ;; esac; return 0; }
        _compose() { return 1; }
        p_update controller
    """)
    both = out + err
    assert "network/DNS fault" in both
    assert "not a" in both and "problem with the checkout" in both
    assert "no upstream for this branch" not in both, "the misleading causes were still printed"


def test_a_genuine_checkout_problem_still_lists_the_usual_causes(sandbox):
    """The old advice is right when the remote IS reachable — keep it for that."""
    rc, out, err = run(sandbox, """
        _discover() { WD=/nope; CFG=/nope/dc.yml; CONTAINER=c; return 0; }
        _git_root() { echo /nope; }
        git() {
          case "$*" in
            *ls-remote*) return 0 ;;
            *pull*) echo "fatal: Not possible to fast-forward, aborting." >&2; return 1 ;;
          esac
          return 0
        }
        _compose() { return 1; }
        p_update controller
    """)
    both = out + err
    assert "no upstream for this branch" in both
    assert "network/DNS fault" not in both


def test_the_reload_refusal_is_not_reported_as_a_surprise(sandbox):
    """`admin off` in the Caddyfile means caddy reload can NEVER work; presenting
    its guaranteed failure as an error sends people to debug a non-problem."""
    rc, out, err = run(sandbox, "_slop_apply_gateway_config",
                       FAKE_RELOAD_FAIL="1", FAKE_HTTP_CODE="302")
    both = out + err
    assert "admin API is off (by design)" in both
    assert "caddy reload failed" not in both


def curl_calls(sandbox):
    try:
        return open(sandbox["curl_log"], encoding="utf-8").read()
    except FileNotFoundError:
        return ""


# ---- the probe must reach the gateway the way a BROWSER does ----------------
# Found on a live server: `slop update` reported "NOT answering on port 443"
# while the gateway was, in the same minute, serving that operator's browser and
# logging 502s from apps behind it. The gateway has no domain — it mints one
# self-signed cert under a fixed internal name and relies on `default_sni` to
# serve it to clients that send NO SNI, which is what a browser hitting
# https://<server-ip>/ does. curl DOES send SNI for a hostname, so probing
# `https://localhost/` matched no certificate and Caddy aborted the handshake.
# `-k` cannot help: the failure is a TLS alert from the server, not a validation
# error at the client. Reproduced against real Caddy 2.8.4 with this exact
# default_sni setup — localhost: curl exit 35, no HTTP at all; 127.0.0.1: 200.
def test_the_probe_never_asks_for_a_hostname_the_gateway_cannot_serve(sandbox):
    run(sandbox, "_health slop")
    calls = curl_calls(sandbox)
    assert calls.strip(), "the probe made no request at all"
    assert "localhost" not in calls, f"probed by name, which the gateway cannot serve: {calls}"
    assert "127.0.0.1" in calls, calls


def test_a_redirect_to_sign_in_is_the_gateways_healthy_answer(sandbox):
    """The apex is behind forward_auth, so an anonymous probe is SUPPOSED to be
    bounced to /login. Treating that as a near-miss would fail every healthy
    gateway."""
    rc, out, err = run(sandbox, "_health_wait slop 4", FAKE_HTTP_CODE="302")
    assert rc == 0, err
    assert "health: OK" in out


def test_a_gateway_that_proxies_a_dead_app_is_still_a_live_gateway(sandbox):
    """A 502 from a REVERSE PROXY means the proxy is up and something behind it
    is not. Reporting 'the gateway is not answering' there sends the operator to
    restart the one component that was working."""
    rc, out, err = run(sandbox, "_health_wait slop 4", FAKE_HTTP_CODE="502")
    assert rc == 0, err
    both = out + err
    assert "is answering" in both, both
    assert "BEHIND the gateway" in both, both
    assert "NOT answering" not in both, both


def test_an_app_that_answers_502_is_NOT_called_healthy(sandbox):
    """The proxy allowance is for the gateway alone — an app's own health
    endpoint returning 502 is a failed bring-up."""
    rc, out, err = run(sandbox, "_health_wait controller 4", FAKE_HTTP_CODE="502")
    assert rc == 1
    assert "answered 502" in err, err
    assert "not a healthy status" in err, err


def test_nothing_listening_still_says_NOT_answering(sandbox):
    """The two faults must stay distinguishable in the message."""
    rc, out, err = run(sandbox, "_health_wait controller 4", FAKE_HEALTH_FAIL="1")
    assert rc == 1
    assert "NOT answering on port 8800" in err, err
