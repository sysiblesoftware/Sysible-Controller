"""Every path the Controller persists to must be on the /data volume.

Reported as "ssh doesn't seem to be working", with a bare **Internal Server
Error** on Host Enrollment -> Enroll a host from here.

Two independent causes, both of which produce exactly that and neither of which
says anything useful:

  1. `openssh-client` was not in the image. The Controller generates its own
     keypair by running `ssh-keygen`, and reaches managed hosts by running
     `ssh`. A missing binary raises FileNotFoundError out of subprocess.run --
     an OSError, raised BEFORE the route's try/except, so it propagated as an
     unhandled 500.

  2. SYSIBLE_HOME, SYSIBLE_HOSTS_FILE and SYSIBLE_REMOTE_KEY_DIR were never set
     in the image, so they fell back to defaults written for the NATIVE install:
     /opt/sysible/... The container runs as uid 10001 and chowns only /data and
     /app, so the first mkdir under /opt raised PermissionError -- another
     OSError, another unhandled 500.

The Dockerfile sets ten other paths to /data and carefully explains why. These
three were simply missed, which is what makes it worth a test rather than a fix:
nothing about adding an eleventh `os.getenv("SYSIBLE_X", "/opt/sysible/x")`
tells you it has to be mirrored in a file two directories away.

The second one would have been worse had it WORKED. /opt/sysible is an image
layer: the keypair every enrolled host had authorised in its authorized_keys
would be discarded on the next update, locking the Controller out of its own
fleet with no error anywhere.

Run: pytest tests/
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DOCKERFILE = (REPO / "Dockerfile").read_text(encoding="utf-8")

# Where the container keeps everything that must outlive a recreate.
VOLUME = "/data"

# Source trees that run INSIDE the container. client/ is the agent, which runs
# on managed hosts and legitimately uses its own paths.
_SOURCES = ("backend", "webgui")


def _persisted_defaults():
    """(env var, default) for every path that defaults under /opt/sysible."""
    out = {}
    pattern = re.compile(
        r'getenv\(\s*"(?P<var>SYSIBLE_[A-Z0-9_]+)"\s*,\s*"(?P<default>/opt/sysible[^"]*)"')
    for sub in _SOURCES:
        for path in (REPO / sub).rglob("*.py"):
            if "node_modules" in path.parts:
                continue
            for m in pattern.finditer(path.read_text(encoding="utf-8", errors="replace")):
                out.setdefault(m.group("var"), (m.group("default"), path))
    return out


def _dockerfile_env():
    """The ENV assignments in the Dockerfile, as {name: value}."""
    env, in_env = {}, False
    for raw in DOCKERFILE.splitlines():
        line = raw.strip()
        if line.startswith("ENV "):
            in_env, line = True, line[4:].strip()
        elif not in_env:
            continue
        if line.startswith("#"):
            continue
        body = line[:-1].strip() if line.endswith("\\") else line
        for pair in body.split():
            if "=" in pair:
                k, v = pair.split("=", 1)
                env[k] = v.strip('"')
        if not line.endswith("\\"):
            in_env = False
    return env


def test_this_test_is_looking_at_something():
    """A regex that matches nothing would make every assertion below vacuous."""
    found = _persisted_defaults()
    assert len(found) >= 4, (
        f"only {len(found)} persisted paths found; the pattern has probably "
        f"stopped matching how these are written: {sorted(found)}")
    env = _dockerfile_env()
    assert env.get("SYSIBLE_DATA_DIR") == VOLUME, (
        f"the Dockerfile ENV parser read SYSIBLE_DATA_DIR as "
        f"{env.get('SYSIBLE_DATA_DIR')!r}; it has stopped parsing the file")


@pytest.mark.parametrize("var", sorted(_persisted_defaults()))
def test_every_persisted_path_is_redirected_onto_the_volume(var):
    default, source = _persisted_defaults()[var]
    env = _dockerfile_env()
    assert var in env, (
        f"{var} defaults to {default} ({source.relative_to(REPO)}) and the "
        f"Dockerfile never overrides it. In the container that path is neither "
        f"writable (uid 10001 owns only /data and /app) nor persistent (it is "
        f"an image layer). Add it to the ENV block beside the others.")
    assert env[var].startswith(VOLUME), (
        f"{var} is set to {env[var]!r}, which is not on the {VOLUME} volume. "
        f"Anything written there is discarded on the next recreate.")


# ---- the binaries the Controller shells out to ----------------------------
def _required_binaries():
    """Executables the backend runs by name, with no path and no fallback."""
    wanted = {}
    pattern = re.compile(r'\[\s*"(?P<bin>ssh|ssh-keygen|ssh-keyscan|scp|openssl)"')
    for sub in _SOURCES:
        for path in (REPO / sub).rglob("*.py"):
            if "node_modules" in path.parts:
                continue
            for m in pattern.finditer(path.read_text(encoding="utf-8", errors="replace")):
                wanted.setdefault(m.group("bin"), path)
    return wanted


# Which Debian package provides each one.
_PROVIDED_BY = {
    "ssh": "openssh-client",
    "ssh-keygen": "openssh-client",
    "ssh-keyscan": "openssh-client",
    "scp": "openssh-client",
    "openssl": "openssl",
}


def test_the_binary_list_is_still_being_found():
    found = _required_binaries()
    assert "ssh-keygen" in found, (
        "ssh-keygen is no longer detected as a subprocess call; this test has "
        "stopped seeing how the backend invokes it")


@pytest.mark.parametrize("binary", sorted(_required_binaries()))
def test_every_binary_the_backend_runs_is_installed(binary):
    """subprocess.run(["ssh-keygen", ...]) on an image without it raises
    FileNotFoundError, which is an OSError, which is a 500 with no detail."""
    pkg = _PROVIDED_BY[binary]
    source = _required_binaries()[binary]
    # Join backslash continuations first: the install list spans several lines,
    # and a pattern that stops at the first newline reads only "apt-get install
    # -y --no-install-recommends" and concludes that nothing is installed.
    joined = re.sub(r"\\\n\s*", " ", DOCKERFILE)
    installed = re.search(r"apt-get install[^\n]*", joined)
    assert installed, "no apt-get install line in the Dockerfile"
    assert re.search(rf"\b{re.escape(pkg)}\b", installed.group(0)), (
        f"{source.relative_to(REPO)} runs {binary!r}, which comes from "
        f"{pkg!r}, and the image does not install it. The call raises "
        f"FileNotFoundError and the console shows 'Internal Server Error'.")
