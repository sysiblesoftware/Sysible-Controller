"""A build that cannot reach the registry must say so BEFORE it builds.

Reported from a fresh Sysible Server. `sysiblectl start slop` on a host whose
resolver could not answer for Docker Hub spent its time in buildkit and then
printed, under a wall of solver output:

    => ERROR [visualizer internal] load metadata for docker.io/library/python:3.12-slim
    target flashback: failed to solve: failed to fetch anonymous token:
      ... dial tcp: lookup auth.docker.io on 172.16.100.2:53: no such host
    ERROR: Sysible SLOP gateway: compose failed to build or start it (see the output above)

Every word of that is true and none of it is the sentence the operator needed,
which is "this host has no DNS". The diagnosis already existed in the suite:
install-sysible probes auth.docker.io before it starts and explains the failure
in one line, and this very file already does the same for the git half --
"That is a network/DNS fault, not a problem with the checkout ... Every
'docker build' below will fail the same way, for the same reason."

The build path simply never had it, so one fault produced a clear diagnosis
from one command and a stack trace from another.
"""
import os
import re
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
CTL = os.path.join(os.path.dirname(HERE), "deploy", "sysiblectl")
SRC = open(CTL, encoding="utf-8").read()


def _run_probe(tmp_path, curl_exit, env=None):
    """Run the REAL _registry_ready with a fake curl of our choosing."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "curl").write_text(f"#!/bin/sh\nexit {curl_exit}\n")
    (bindir / "curl").chmod(0o755)

    body = re.search(r"^_registry_ready\(\) \{.*?^\}", SRC, re.S | re.M)
    assert body, "_registry_ready is not in the CLI at all"

    script = tmp_path / "probe.sh"
    script.write_text(
        'RED=""; YELLOW=""; RESET=""\n'
        '_warn() { printf "%s\\n" "$*" >&2; }\n'
        '_err()  { printf "ERROR: %s\\n" "$*" >&2; }\n'
        + body.group(0) + "\n"
        '_registry_ready && echo PROCEED || echo REFUSED\n')
    e = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    e.update(env or {})
    return subprocess.run(["bash", str(script)], capture_output=True, text=True,
                          env=e, timeout=60)


def test_an_unreachable_registry_refuses_the_build(tmp_path):
    r = _run_probe(tmp_path, curl_exit=6)      # 6 = couldn't resolve host
    assert "REFUSED" in r.stdout, f"the build would have gone ahead: {r.stdout!r}"


def test_it_names_the_host_and_calls_it_a_dns_fault(tmp_path):
    err = _run_probe(tmp_path, curl_exit=6).stderr
    # The HEADLINE must name it. A first pass only asserted "auth.docker.io" was
    # somewhere in the output, which stayed true when the headline was reduced to
    # "cannot reach the registry" -- the hints below still mentioned the host, so
    # the guard passed over a message that no longer said what was unreachable.
    headline = err.strip().splitlines()[0]
    assert "auth.docker.io" in headline, \
        f"the first line does not name the host that failed: {headline!r}"
    assert re.search(r"network or DNS fault", err), \
        "the message does not say what KIND of fault this is"
    assert "not a problem with the checkout" in err, \
        "it does not rule out the things an operator would otherwise go and check"


@pytest.mark.parametrize("hint", [
    "getent hosts auth.docker.io",      # confirm the fault
    "cat /etc/resolv.conf",             # find the resolver
    "systemctl restart docker",         # docker resolving differently from the host
    "SYSIBLE_SKIP_REGISTRY_CHECK=1",    # mirror / images already pulled
])
def test_the_message_is_actionable(tmp_path, hint):
    assert hint in _run_probe(tmp_path, curl_exit=6).stderr, \
        f"the diagnosis does not tell the operator to try {hint!r}"


def test_a_reachable_registry_proceeds(tmp_path):
    r = _run_probe(tmp_path, curl_exit=0)
    assert "PROCEED" in r.stdout, f"a working host was refused a build: {r.stdout!r}"


def test_the_check_can_be_skipped(tmp_path):
    """A registry mirror, or base images already pulled, needs no Docker Hub."""
    r = _run_probe(tmp_path, curl_exit=6, env={"SYSIBLE_SKIP_REGISTRY_CHECK": "1"})
    assert "PROCEED" in r.stdout, "the documented escape hatch does not work"


def test_a_host_without_curl_is_not_blocked(tmp_path):
    """Nothing to probe with is not evidence of a broken network."""
    bindir = tmp_path / "nocurl"
    bindir.mkdir()
    body = re.search(r"^_registry_ready\(\) \{.*?^\}", SRC, re.S | re.M).group(0)
    script = tmp_path / "p.sh"
    script.write_text('_warn() { :; }\n_err() { :; }\n' + body +
                      '\n_registry_ready && echo PROCEED || echo REFUSED\n')
    # An absolute interpreter: PATH here deliberately contains no curl, and a
    # bare "bash" would fail to resolve for that same reason and look like a
    # refusal.
    bash = "/bin/bash" if os.path.exists("/bin/bash") else "/usr/bin/bash"
    r = subprocess.run([bash, str(script)], capture_output=True, text=True,
                       env={"PATH": str(bindir)}, timeout=60)
    assert "PROCEED" in r.stdout, "a host with no curl was refused a build"


def test_the_check_runs_before_the_build_not_after():
    """Ordering is the entire point: after the build it is just another line in
    the trace the operator already could not read."""
    fn = re.search(r"^p_rebuild\(\) \{.*?^\}", SRC, re.S | re.M)
    assert fn, "p_rebuild is not in the CLI"
    body = fn.group(0)
    build = body.find("up -d --build")
    assert build != -1, "p_rebuild no longer builds — this test needs updating"
    # The BLOCKING form, not merely a mention. `_registry_ready || true` keeps
    # the call, keeps the position, and lets the build proceed anyway -- which a
    # search for the bare name cannot tell apart from the real thing.
    m = re.search(r"_registry_ready\s*\|\|\s*return\s+1", body)
    assert m, ("p_rebuild does not REFUSE the build on an unreachable registry "
               "(expected `_registry_ready || return 1`)")
    assert m.start() < build, \
        "the registry check runs AFTER the build it was meant to prevent"
