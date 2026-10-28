"""An image sysiblectl builds must carry the commit it was built from.

A SLOP console is server-rendered: the whole page comes out of the container, so
a stale image and a current one are indistinguishable from outside. Three rounds
of "is this my browser, my server, or your code?" ended with the answer being
"that code was never deployed", and the only evidence was noticing that a
sentence deleted two commits earlier was still on the page.

So the compose file takes the revision as a build arg, and the two places
sysiblectl runs `compose up --build` have to put it in the environment of the
docker process they spawn. That is what these tests check — not that the string
appears in the script, but that `_p_rev` finds the right commit and that the
value actually arrives at docker.

Run: pytest tests/test_ctl_stamps_the_build_it_made.py
"""
import os
import re
import subprocess
import textwrap

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
CTL = os.path.join(os.path.dirname(HERE), "deploy", "sysiblectl")

# Records its own SYSIBLE_SLOP_REV on every call, which is the only way to tell
# whether the prefix reached the child rather than just the shell function.
FAKE_DOCKER = r"""#!/bin/sh
printf '%s | SYSIBLE_SLOP_REV=%s\n' "$*" "${SYSIBLE_SLOP_REV-<unset>}" >> "$FAKE_LOG"
case "$1" in
  compose) exit 0 ;;
  inspect) exit 1 ;;
esac
exit 0
"""


@pytest.fixture
def sandbox(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("docker", FAKE_DOCKER),
                       ("sleep", "#!/bin/sh\nexit 0\n"),
                       ("curl", "#!/bin/sh\nprintf 302\nexit 0\n")):
        p = bindir / name
        p.write_text(body)
        p.chmod(0o755)
    src = open(CTL, encoding="utf-8").read()
    lib = re.sub(r'^main "\$@"\s*$', "", src, flags=re.M)
    libp = tmp_path / "ctl.lib.sh"
    libp.write_text(lib)
    return {"bin": str(bindir), "lib": str(libp), "log": str(tmp_path / "docker.log"),
            "tmp": tmp_path}


def run(sandbox, snippet, **env):
    script = f'. "{sandbox["lib"]}"\n' + textwrap.dedent(snippet)
    e = {"PATH": sandbox["bin"] + ":" + os.environ.get("PATH", "/usr/bin:/bin"),
         "FAKE_LOG": sandbox["log"], "HOME": os.environ.get("HOME", "/root")}
    e.update({k: str(v) for k, v in env.items()})
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env=e, timeout=120)
    return r.returncode, r.stdout, r.stderr


def _checkout(root, compose_in="."):
    """A real git checkout with a real commit, because _p_rev asks git."""
    root.mkdir(parents=True, exist_ok=True)
    d = root / compose_in
    d.mkdir(parents=True, exist_ok=True)
    (d / "docker-compose.yml").write_text("services:\n  x:\n    image: busybox\n")
    # slop refuses to rebuild without one, and the refusal happens before the
    # build — so without this the test would prove only that it bailed out.
    (d / ".env").write_text("SYSIBLE_SSO_SHARED_SECRET=" + "s" * 32 + "\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", "."], cwd=root, check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=root, check=True, env=env)
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root,
                          capture_output=True, text=True, check=True, env=env)
    return head.stdout.strip()


# ---- finding the revision ---------------------------------------------------
def test_it_reads_the_commit_of_the_checkout(sandbox):
    rev = _checkout(sandbox["tmp"] / "co")
    rc, out, err = run(sandbox, f'_p_rev "{sandbox["tmp"]}/co"')
    assert rc == 0, err
    assert out.strip() == rev, f"got {out.strip()!r}, the checkout is at {rev!r}"


def test_it_finds_the_commit_from_the_compose_subdirectory(sandbox):
    """The dir handed to it is the COMPOSE dir. For products whose compose lives
    in deploy/ that is a subdirectory of the checkout, so a `.git` test on the
    dir itself finds nothing and the product ships unstamped. It has to resolve
    the checkout root."""
    rev = _checkout(sandbox["tmp"] / "co", compose_in="deploy")
    rc, out, err = run(sandbox, f'_p_rev "{sandbox["tmp"]}/co/deploy"')
    assert rc == 0, err
    assert out.strip() == rev, \
        f"a compose dir inside the checkout gave {out.strip()!r}, not {rev!r}"


def test_a_dir_that_is_not_a_checkout_yields_nothing(sandbox):
    """Empty, not an error and not a guess: the console then says "version
    unknown" rather than claiming a build it cannot prove."""
    plain = sandbox["tmp"] / "plain"
    plain.mkdir()
    rc, out, err = run(sandbox, f'_p_rev "{plain}"; echo "rc=$?"')
    assert "rc=0" in out, err
    assert out.replace("rc=0", "").strip() == ""


def test_no_dir_at_all_is_not_an_error(sandbox):
    rc, out, err = run(sandbox, '_p_rev ""; echo "rc=$?"')
    assert "rc=0" in out, err
    assert out.replace("rc=0", "").strip() == ""


# ---- the value reaching docker ----------------------------------------------
def test_rebuild_puts_the_revision_in_dockers_environment(sandbox):
    """The prefix is on a shell FUNCTION call, so this is the assertion that
    matters: not that the script mentions the variable, but that the docker
    process spawned several frames down actually has it."""
    rev = _checkout(sandbox["tmp"] / "slop")
    rc, out, err = run(sandbox, 'p_rebuild slop || true',
                       SYSIBLE_SLOP_DIR=str(sandbox["tmp"] / "slop"))
    log = open(sandbox["log"], encoding="utf-8").read()
    builds = [ln for ln in log.splitlines() if "up -d --build" in ln]
    assert builds, f"nothing was built at all:\n{log}\n{out}\n{err}"
    for ln in builds:
        assert f"SYSIBLE_SLOP_REV={rev}" in ln, \
            f"docker built without the revision in its environment: {ln}"


def test_an_unstamped_checkout_still_builds(sandbox):
    """A tarball install is not a git checkout. It must still come up — just
    without a revision, which the console reports honestly."""
    d = sandbox["tmp"] / "notgit"
    d.mkdir()
    (d / "docker-compose.yml").write_text("services:\n  x:\n    image: busybox\n")
    (d / ".env").write_text("SYSIBLE_SSO_SHARED_SECRET=" + "s" * 32 + "\n")
    rc, out, err = run(sandbox, 'p_rebuild slop || true', SYSIBLE_SLOP_DIR=str(d))
    log = open(sandbox["log"], encoding="utf-8").read()
    builds = [ln for ln in log.splitlines() if "up -d --build" in ln]
    assert builds, f"a non-git checkout was not built at all:\n{log}\n{err}"
    for ln in builds:
        assert "SYSIBLE_SLOP_REV=" in ln, ln
        assert "SYSIBLE_SLOP_REV=<unset>" not in ln, \
            f"the variable never reached docker: {ln}"


# ---- both sites, not just the one that was tested ---------------------------
def test_every_build_site_stamps():
    """There are two `compose up --build` calls — update and rebuild. One of them
    stamping is the failure mode where the version is right until an operator
    uses the other verb."""
    src = open(CTL, encoding="utf-8").read()
    # Join backslash continuations FIRST. One of the two sites is wrapped, so a
    # line-based scan reads its second half on its own and calls a stamped site
    # unstamped — which is exactly what this test did on its first run.
    joined = re.sub(r"\\\n\s*", " ", src)
    sites = [ln.strip() for ln in joined.splitlines()
             if "up -d --build" in ln and not ln.strip().startswith("#")]
    assert len(sites) >= 2, f"expected both build sites, found {sites}"
    for ln in sites:
        assert "SYSIBLE_SLOP_REV=" in ln, f"this build site does not stamp: {ln}"
