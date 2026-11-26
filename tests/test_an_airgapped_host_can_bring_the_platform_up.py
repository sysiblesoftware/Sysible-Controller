"""A host with the shipped images must start the platform without building.

The platform used to be BUILT FROM SOURCE on the operator's host. Every
service's Dockerfile fetches from PyPI, npm, Debian or Alpine, so that could
never work on an isolated network no matter what the ISO carried -- baking the
base images got as far as the first `pip install` and stopped.

The ISO now ships the finished images, built in CI, and loads them into Docker
at boot. This is the half that makes them count: if `p_rebuild` still ran
`up -d --build`, compose would rebuild from source with the images sitting
right there, and fail exactly as before.

`update` is deliberately NOT changed. An update is new source, rebuilt; a host
that cannot fetch source cannot be updated, and the honest answer there is to
say so, not to quietly start the old images again.
"""
import re
import os
import subprocess
import pathlib

import pytest

HERE = pathlib.Path(__file__).resolve().parent
CTL = HERE.parent / "deploy" / "sysiblectl"
SRC = CTL.read_text(encoding="utf-8")


def _fn(name):
    m = re.search(rf"^{name}\(\) \{{.*?^\}}", SRC, re.S | re.M)
    assert m, f"{name} is not in the CLI"
    return m.group(0)


def _code(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


# ---- the probe itself, executed -------------------------------------------
def _run_probe(tmp_path, present):
    """Run the REAL _images_all_present against a fake docker + compose."""
    bindir = tmp_path / "bin"; bindir.mkdir()
    (bindir / "docker").write_text(
        "#!/bin/sh\n"
        'case "$1 $2" in\n'
        '  "image inspect") [ "${FAKE_PRESENT:-1}" = 1 ] && exit 0 || exit 1 ;;\n'
        "esac\nexit 0\n")
    (bindir / "docker").chmod(0o755)
    cfg = tmp_path / "docker-compose.yml"; cfg.write_text("services: {}\n")
    script = tmp_path / "p.sh"
    script.write_text(
        'DC=(docker compose)\n'
        '"${DC[@]}"() { :; }\n'
        + _fn("_images_all_present").replace(
            '"${DC[@]}" -f "$cfg" config --images 2>/dev/null',
            'printf "%s\\n" sysible-slop-idp caddy:2-alpine')
        + f'\n_images_all_present "{cfg}" && echo PRESENT || echo MISSING\n')
    e = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
         "FAKE_PRESENT": "1" if present else "0"}
    return subprocess.run(["/bin/bash", str(script)], env=e,
                          capture_output=True, text=True, timeout=60)


def test_it_sees_the_images_when_they_are_loaded(tmp_path):
    assert "PRESENT" in _run_probe(tmp_path, present=True).stdout


def test_one_missing_image_means_a_build_is_needed(tmp_path):
    """Partially present must mean build, not start: compose would fail on the
    one image that is not there, after the slow part."""
    assert "MISSING" in _run_probe(tmp_path, present=False).stdout


# ---- it is actually used --------------------------------------------------
def test_the_first_bring_up_does_not_force_a_build():
    body = _code(_fn("p_rebuild"))
    # The flag must be cleared UNDER the images-present check. Asserting the
    # helper merely appears in the function passed when the branch was changed
    # to `if false`, because the registry preflight below also names it.
    assert re.search(r"if _images_all_present[^\n]*\n\s*_bld=\(\)", body), (
        "clearing the build flag is not gated on the images actually being "
        "present, so p_rebuild either always builds or never does")
    assert "--build" not in body.split("up -d")[1][:40], \
        "the build flag is still unconditional"


def test_the_registry_check_is_skipped_when_nothing_needs_building():
    body = _code(_fn("p_rebuild"))
    assert re.search(r"if ! _images_all_present[^\n]*\n\s*_registry_ready", body), (
        "an airgapped host that needs no registry would still be refused by the "
        "registry preflight")


def test_updating_still_rebuilds():
    """The one path that must NOT prefer the shipped images."""
    body = _code(_fn("p_update"))
    assert "up -d --build" in body, \
        "update no longer rebuilds — new source would never reach a container"
    assert "_images_all_present" not in body, \
        "update must not skip a build just because older images are present"


@pytest.mark.parametrize("phrase", [
    "airgapped ON PURPOSE",
    "install a newer ISO",
])
def test_update_says_what_airgapped_means(phrase):
    """A failed `git pull` is not an explanation. On a deliberately isolated
    host nothing is broken, and the message should say so."""
    assert phrase in SRC, f"the update path never says {phrase!r}"
