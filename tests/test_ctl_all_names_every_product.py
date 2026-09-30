"""`<cmd> all` must account for every product, including the missing one.

Reported as "SLOP isn't coming up", with this transcript:

    # sysible_ctl restart all
    ==> Restarting Sysible Controller          restarted
    ==> Restarting Sysible Linux Engineering Platform   restarted
    ==> Restarting Sysible Connect             restarted
    #

Three products restarted and the fourth is not mentioned at all. p_all only
visited a product `_present` said it had, so a product whose containers had been
removed fell out of the loop without a word — and the operator reads that list as
complete. The one product missing from it is the one they were asking about.

Worse, the function's LAST statement was `_present slop && _ok "portal: ..."`,
so on any host where SLOP is not deployed the whole command exited 1 after
printing nothing but successes: correct output, failing status, no message.

These run the real script with a fake docker, so what is asserted is what the
operator would have seen.
"""
import os
import re
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
CTL = os.path.join(os.path.dirname(HERE), "deploy", "sysiblectl")

# Every container exists except the ones named in FAKE_MISSING, and every
# lifecycle verb succeeds unless the container is in FAKE_FAIL.
FAKE_DOCKER = r"""#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_LOG"
_named() { for n in $2; do [ "$n" = "$1" ] && return 0; done; return 1; }
case "$1" in
  inspect)
    for a in "$@"; do
      case "$a" in -*|inspect) continue ;; esac
      _named "$a" "${FAKE_MISSING:-}" && exit 1
    done
    exit 0 ;;
  restart|start|stop)
    _named "$2" "${FAKE_FAIL:-}" && { echo "Error: no such container: $2" >&2; exit 1; }
    exit 0 ;;
  compose) exit 0 ;;
  ps)
    # `docker ps -a --format {{.Names}}` — the half-recreate probe. FAKE_ORPHANS
    # holds the names compose left under its temporary <oldid>_<name> form.
    for n in ${FAKE_ORPHANS:-}; do echo "$n"; done
    exit 0 ;;
esac
exit 0
"""


@pytest.fixture()
def ctl(tmp_path):
    """The real CLI, with a fake docker and nothing else on PATH to find."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    d = bindir / "docker"
    d.write_text(FAKE_DOCKER)
    d.chmod(0o755)

    def _run(*args, **env):
        e = {"PATH": f"{bindir}:/usr/bin:/bin",
             "FAKE_LOG": str(tmp_path / "docker.log"),
             "HOME": str(tmp_path),
             # An empty source dir, so _find_checkout finds nothing unless a test
             # puts a checkout there on purpose.
             "SYSIBLE_SRC_DIR": str(tmp_path / "src")}
        e.update({k: str(v) for k, v in env.items()})
        r = subprocess.run(["bash", CTL, *args], capture_output=True, text=True,
                           env=e, timeout=180)
        return r.returncode, r.stdout + r.stderr
    return _run


def _checkout(tmp_path, name):
    """A directory _find_checkout will accept: a compose file makes it real."""
    d = tmp_path / "src" / name
    d.mkdir(parents=True)
    (d / "docker-compose.yml").write_text("services: {}\n")
    return d


# ---- the reported transcript -----------------------------------------------
def test_a_product_with_no_containers_is_still_named(ctl):
    rc, out = ctl("restart", "all", FAKE_MISSING="sysible-slop-gateway")
    assert "Sysible Controller" in out          # the three that were restarted
    assert re.search(r"Nothing to restart for:.*\bslop\b", out), (
        "the product with no containers vanished from the output — the operator "
        "reads that list as complete")


def test_it_says_what_to_do_about_it(ctl, tmp_path):
    """"Not running" and "not installed" need different commands, so the message
    has to know which one this is."""
    _checkout(tmp_path, "sysible-linux-operations-platform")
    rc, out = ctl("restart", "all", FAKE_MISSING="sysible-slop-gateway")
    assert "none of its containers exist" in out
    assert "sysiblectl slop start" in out, "no way forward was offered"


def test_a_product_that_is_not_installed_says_so_instead(ctl):
    rc, out = ctl("restart", "all", FAKE_MISSING="sysible-slop-gateway")
    assert "no containers and no checkout" in out
    assert "sysiblectl slop start" not in out, \
        "offering 'up' for a product with no checkout sends the operator in a circle"


# ---- ...and the status has to mean something --------------------------------
def test_a_clean_run_without_slop_still_exits_zero(ctl):
    """The bug: `_present slop` was the function's last statement, so its false
    was the command's exit code."""
    rc, out = ctl("restart", "all", FAKE_MISSING="sysible-slop-gateway")
    assert rc == 0, f"every product that exists was restarted, yet exit {rc}:\n{out}"


def test_a_product_that_actually_failed_exits_nonzero(ctl):
    rc, out = ctl("restart", "all", FAKE_FAIL="sysible-connect")
    assert rc != 0, "a failed restart was reported as a success"
    assert "Completed with problems" in out and "connect" in out


def test_a_fully_deployed_host_is_unchanged(ctl):
    """The warning must not appear when there is nothing to warn about."""
    rc, out = ctl("restart", "all")
    assert rc == 0
    assert "Nothing to restart for" not in out
    assert "SLOP portal" in out, "the front-door line went missing"


def test_stop_all_is_accounted_for_the_same_way(ctl):
    rc, out = ctl("stop", "all", FAKE_MISSING="sysible-slop-gateway")
    assert rc == 0
    assert "Nothing to stop for:" in out


# ---- the state that looks like "not installed" but is not -------------------
#
# Reported as "why is this up but not available", with a `docker ps` showing the
# SLOP stack running under names like 7ffb46fbf6d4_sysible-slop-gateway while
# `sysiblectl restart all` never mentioned SLOP at all.
#
# `docker compose up` replaces a container by creating the new one as
# <old-container-id>_<name>, removing the old one, then renaming the new over it.
# Kill the compose process between the create and the rename — which is what
# happened every time SLOP updated ITSELF, because the client ran inside a
# container that same run was replacing — and the stack is left RUNNING under a
# name nothing looks for. Every tool that addresses a product by container name
# then reports it as not installed, on a host where it is plainly running.
ORPHAN = "7ffb46fbf6d4_sysible-slop-gateway"


def test_a_half_recreated_stack_is_named_as_such(ctl):
    rc, out = ctl("restart", "all", FAKE_MISSING="sysible-slop-gateway",
                  FAKE_ORPHANS=ORPHAN)
    assert "HALF-RECREATED" in out, (
        "a stack left under compose's temporary names still reports as simply "
        "absent — which is what made this baffling in the first place")
    assert ORPHAN in out, "the container actually holding the service is not named"


def test_it_does_not_call_a_running_stack_uninstalled(ctl):
    """The misleading half. Saying 'no containers and no checkout' about a stack
    whose containers are up sends the operator looking for the wrong thing."""
    rc, out = ctl("restart", "all", FAKE_MISSING="sysible-slop-gateway",
                  FAKE_ORPHANS=ORPHAN)
    assert "no containers and no checkout" not in out
    assert "none of its containers exist" not in out


def test_it_says_how_to_finish_the_swap(ctl):
    rc, out = ctl("restart", "all", FAKE_MISSING="sysible-slop-gateway",
                  FAKE_ORPHANS=ORPHAN)
    assert "sysiblectl slop start" in out, "no way forward was offered"
    assert "from the HOST" in out, (
        "the fix has to say where to run it — running it from inside the stack is "
        "what produced this state")


def test_only_the_temporary_name_pattern_counts(ctl):
    """`sysible-slop-gateway-2` or a hand-named copy is not a half-recreate, and
    calling one an interrupted update sends someone chasing a problem that is not
    there."""
    rc, out = ctl("restart", "all", FAKE_MISSING="sysible-slop-gateway",
                  FAKE_ORPHANS="my_sysible-slop-gateway sysible-slop-gateway-old")
    assert "HALF-RECREATED" not in out


def test_a_healthy_host_says_nothing_about_it(ctl):
    rc, out = ctl("restart", "all")
    assert "HALF-RECREATED" not in out


def test_status_reports_it_too(ctl):
    """The other place someone goes when the front door is dark."""
    rc, out = ctl("slop", "status", FAKE_MISSING="sysible-slop-gateway",
                  FAKE_ORPHANS=ORPHAN)
    assert "HALF-RECREATED" in out
