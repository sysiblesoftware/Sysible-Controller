"""The tool is `sysiblectl`, and the old name keeps working.

Asked for: "it should be sysiblectl not sysible_ctl", alongside an audit that cut
`up`, `build` and `install <product>` — three spellings of one operation — down to
`start` (make it run, building only if there is nothing to start) and `rebuild`
(rebuild from the checkout as it is on disk).

A rename is the easy half. The half that costs people is everything already
written down: `sysible_ctl` is on every host's PATH, in the SLOP installer, in
the Administration page's own instructions, in runbooks and in whatever anyone
automated. So both names are installed and both work, and `up`/`build` still
route — they just say what replaced them. These tests hold that promise, because
it is the kind that quietly lapses the next time someone tidies up.
"""
import os
import re
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
DEPLOY = os.path.join(os.path.dirname(HERE), "deploy")
CTL = os.path.join(DEPLOY, "sysiblectl")
LEGACY = os.path.join(DEPLOY, "sysible_ctl")


@pytest.fixture(scope="module")
def src():
    with open(CTL, encoding="utf-8") as fh:
        return fh.read()


def _help(argv0=CTL):
    r = subprocess.run(["bash", argv0, "help"], capture_output=True, text=True,
                       env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                            "HOME": os.environ.get("HOME", "/root")}, timeout=60)
    return r.stdout + r.stderr


# ---- the name --------------------------------------------------------------
def test_the_script_is_called_sysiblectl():
    assert os.path.isfile(CTL), "deploy/sysiblectl is missing"
    assert os.access(CTL, os.X_OK), "deploy/sysiblectl is not executable"


def test_the_old_path_still_resolves_to_it():
    """SLOP's installer links whatever it finds at deploy/sysible_ctl, and older
    copies of that installer are on hosts right now. Removing the path outright
    would break an install that has not been updated yet."""
    assert os.path.exists(LEGACY), "deploy/sysible_ctl is gone, not redirected"
    assert os.path.islink(LEGACY), "deploy/sysible_ctl should be a link, not a copy"
    assert os.path.realpath(LEGACY) == os.path.realpath(CTL)


def test_it_installs_under_both_names(src):
    body = src[src.index("cmd_install() {"):src.index("cmd_uninstall() {")]
    assert "$CLI_NAME" in body and "$CLI_LEGACY_NAME" in body, \
        "installing no longer puts the old name on PATH beside the new one"
    assert body.count("ln -sf") >= 2


def test_uninstall_removes_both(src):
    body = src[src.index("cmd_uninstall() {"):src.index("# One line, on stderr")]
    assert "$CLI_LEGACY_NAME" in body, \
        "uninstall leaves the old name behind, pointing at a script that may be gone"


def test_the_old_name_says_so_once(src):
    assert 'CLI_LEGACY_NAME="sysible_ctl"' in src
    fn = src[src.index("_warn_legacy_name() {"):]
    fn = fn[:fn.index("\n}\n") + 3]
    assert "_warn" in fn, "the notice must go to stderr, so it cannot break a pipeline"
    assert 'basename "$0"' in fn, "the notice fires regardless of how it was invoked"


def test_the_help_calls_it_sysiblectl():
    out = _help()
    assert "sysiblectl" in out
    assert "sysible_ctl" in out, \
        "the help never mentions the old name, so anyone typing it learns nothing"


# ---- the vocabulary --------------------------------------------------------
EXPECTED = {"start", "stop", "restart", "status", "logs",
            "update", "rebuild", "backup", "destroy"}


def test_the_advertised_commands_are_exactly_these(src):
    m = re.search(r'^KNOWN_CMDS="([^"]+)"', src, re.M)
    assert m, "KNOWN_CMDS is gone"
    assert set(m.group(1).split()) == EXPECTED, \
        f"the command set drifted: {m.group(1)}"


def test_up_and_build_are_no_longer_advertised(src):
    m = re.search(r'^KNOWN_CMDS="([^"]+)"', src, re.M)
    for gone in ("up", "build", "install"):
        assert gone not in m.group(1).split(), \
            f"'{gone}' is back in the advertised set — that is the redundancy that was cut"


def test_they_are_still_accepted(src):
    m = re.search(r'^DEPRECATED_CMDS="([^"]+)"', src, re.M)
    assert m, "the old verbs are not accepted at all — every old script breaks"
    assert set(m.group(1).split()) == {"up", "build"}


def test_the_help_documents_every_advertised_command():
    out = _help()
    for cmd in EXPECTED:
        assert re.search(rf"\b{cmd}\b", out), f"'{cmd}' is a real command the help never names"


def test_start_is_the_one_that_brings_a_product_up(src):
    """The whole point of the audit: `up` was the first-run verb and `start` only
    started something already built, so an operator had to know which situation
    they were in before choosing a word."""
    fn = src[src.index("p_start() {"):]
    fn = fn[:fn.index("\n}\n") + 3]
    assert "p_rebuild" in fn, "start can no longer bring up a product that was never built"
    assert "_present" in fn, "start always rebuilds — it must start what is already there"


def test_start_says_when_it_is_about_to_take_minutes(src):
    """A command that usually returns in seconds and occasionally takes five
    minutes has to say which one this is before it begins."""
    fn = src[src.index("p_start() {"):]
    fn = fn[:fn.index("\n}\n") + 3]
    assert "not built yet" in fn and "takes a few minutes" in fn


def test_install_no_longer_means_two_things(src):
    """Bare `install` linked the CLI; `install <product>` built that product. One
    word, two unrelated jobs, chosen by how many words followed it."""
    block = src[src.index("    install)"):src.index("  esac", src.index("    install)"))]
    assert "does not build a product" in block
    assert "start" in block and "rebuild" in block, \
        "the refusal does not name the verbs that do the job it used to do"


def test_nothing_still_tells_anyone_to_run_up(src):
    """Messages that hand out a command have to hand out one that is documented."""
    offenders = [ln.strip() for ln in src.splitlines()
                 if re.search(r'\$\(basename "\$0"\)\s+\$?\w*\s*\bup\b', ln)]
    assert not offenders, f"still advising the retired verb: {offenders}"
