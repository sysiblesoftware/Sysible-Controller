"""The first time sysiblectl runs on a machine, offer to update.

Asked for: "when sysible apps are chosen on install it should install the base
images but also ask if you want to update the software the first time that
sysiblectl is run on the machine." The base images already arrive with the ISO and
load at first boot, so what was missing is the question.

Every guard here is a way an interactive prompt has gone wrong before: on a
first-boot unit it blocks the boot, in CI it hangs the job, and one that is not
remembered asks again on every single run forever. So the test drives the real
shell functions through a real pty rather than reading the script and hoping.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

CTL = Path(__file__).resolve().parents[1] / "deploy" / "sysiblectl"
WANT = ("_first_run_already_asked", "_first_run_remember", "_maybe_offer_first_update")


def _extract(dst: Path):
    """Pull the functions out by brace matching.

    Line ranges are how a harness silently failed to define a helper earlier in
    this project and handed back a false pass; this matches braces and asserts on
    what it got."""
    src = CTL.read_text(encoding="utf-8")
    out = []
    for const in ("_FIRST_RUN_STATE_DIRS=", "_FIRST_RUN_FILE="):
        m = re.search(r"^%s.*$" % re.escape(const), src, re.M)
        assert m, "sysiblectl no longer defines " + const
        out.append(m.group(0))
    for name in WANT:
        i = src.index("\n%s() {" % name) + 1
        depth, j = 0, i
        while True:
            c = src[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out.append(src[i:j + 1])
    dst.write_text("\n".join(out) + "\n")


HARNESS = r"""#!/bin/bash
set -u
CLI_NAME=sysiblectl
SELF="$FAKE_SELF"
_hdr()  { echo "HDR $*"; }
_warn() { echo "WARN $*"; }
_git_root() { local d="$1"
  while [[ -n "$d" && "$d" != "/" ]]; do [[ -d "$d/.git" ]] && { echo "$d"; return 0; }; d="$(dirname "$d")"; done
  return 1; }
_dispatch() { echo "DISPATCH $*"; }
source "$FUNCS"
# If the extraction silently produced nothing, "did not ask" would look like a
# passing guard. Refuse to be that harness.
for f in _first_run_already_asked _first_run_remember _maybe_offer_first_update; do
  declare -F "$f" >/dev/null || { echo "HARNESS BROKEN: $f missing"; exit 99; }
done
_maybe_offer_first_update
"""


@pytest.fixture()
def box(tmp_path):
    """A machine: the extracted functions, a fake checkout, and its own state dirs."""
    if not shutil.which("script"):
        pytest.skip("util-linux `script` is needed to give the prompt a real pty")
    funcs = tmp_path / "funcs.sh"
    _extract(funcs)
    harness = tmp_path / "harness.sh"
    harness.write_text(HARNESS)
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / "repo" / "sysiblectl").write_text("#!/bin/bash\n")
    state = tmp_path / "state"
    state.mkdir()

    def run(answer="n", tty=True, env=None, self_path=None):
        e = dict(os.environ)
        # This container exports DEBIAN_FRONTEND=noninteractive, which the code
        # correctly treats as "nobody is here" — clear it so the default case is a
        # person at a terminal, and set it back explicitly in the guard test.
        for k in ("DEBIAN_FRONTEND", "CI", "INVOCATION_ID", "SYSTEMD_EXEC_PID",
                  "SYSIBLE_NO_UPDATE_PROMPT"):
            e.pop(k, None)
        e.update({"FUNCS": str(funcs), "HOME": str(tmp_path),
                  "XDG_STATE_HOME": str(state),
                  # Keep the machine-wide /var/lib/sysible out of it: these tests
                  # must not depend on, or leave behind, state on the machine
                  # running them — and as root the first test would otherwise write
                  # a marker that silenced every test after it.
                  "SYSIBLE_STATE_DIR": str(tmp_path / "machine-state"),
                  "FAKE_SELF": str(self_path or (tmp_path / "repo" / "sysiblectl"))})
        e.update(env or {})
        if tty:
            cmd = ["script", "-qec", "bash %s" % harness, "/dev/null"]
            p = subprocess.run(cmd, input=(answer + "\n"), capture_output=True,
                               text=True, env=e, timeout=60)
        else:
            p = subprocess.run(["bash", str(harness)], input="", capture_output=True,
                               text=True, env=e, timeout=60)
        assert p.returncode != 99, "harness broken: " + p.stdout
        return p.stdout.replace("\r", "")
    return run


# ------------------------------------------------------------ it does ask

def test_it_asks_on_a_fresh_machine(box):
    out = box(answer="n")
    assert "First run" in out, out
    assert "Update Sysible to the latest now?" in out


def test_yes_starts_the_update(box):
    assert "DISPATCH update all" in box(answer="y")


def test_no_does_not_update_and_says_how_to_later(box):
    out = box(answer="n")
    assert "DISPATCH" not in out
    assert "all update" in out, "declining leaves no way to find the command again"


@pytest.mark.parametrize("answer", ["n", "y"])
def test_it_asks_once_per_machine_whatever_the_answer(box, answer):
    """One machine per case: looping both answers through a single box meant the
    second iteration started on a machine that had already been asked."""
    assert "First run" in box(answer=answer)
    assert "First run" not in box(answer=answer), (
        "asked twice after answering %r — that is every invocation, forever" % answer)


def test_the_answer_is_recorded_before_the_update_runs(box, tmp_path):
    """An update that fails, or that the operator interrupts with ^C, must not come
    back as the same question on the next run."""
    out = box(answer="y")
    assert "DISPATCH" in out
    markers = (list((tmp_path / "machine-state").rglob("update-prompt-answered"))
               + list((tmp_path / "state").rglob("update-prompt-answered")))
    assert markers, "nothing was recorded, so the next run asks again"
    assert "asked " in markers[0].read_text()


# --------------------------------------------------- and when it must not

@pytest.mark.parametrize("env,why", [
    ({"SYSIBLE_NO_UPDATE_PROMPT": "1"}, "the documented opt-out for automation"),
    ({"INVOCATION_ID": "deadbeef"}, "running under systemd"),
    ({"SYSTEMD_EXEC_PID": "42"}, "running under systemd"),
    ({"CI": "true"}, "running in CI"),
    ({"DEBIAN_FRONTEND": "noninteractive"}, "an unattended package run"),
])
def test_it_stays_quiet_where_nobody_can_answer(box, env, why):
    out = box(answer="y", env=env)
    assert "First run" not in out, "prompted while %s — this blocks the run" % why
    assert "DISPATCH" not in out


def test_it_stays_quiet_with_no_terminal(box):
    """The first-boot case. A read here blocks the boot."""
    out = box(tty=False)
    assert "First run" not in out and "DISPATCH" not in out


def test_it_stays_quiet_with_nothing_to_update_from(box, tmp_path):
    """An airgapped ISO install has the images and no checkout. Offering an update
    it cannot perform is worse than staying quiet."""
    lone = tmp_path / "bin" / "sysiblectl"
    lone.parent.mkdir()
    lone.write_text("#!/bin/bash\n")
    assert "First run" not in box(answer="y", self_path=lone)


def test_a_guard_test_cannot_pass_by_the_feature_being_dead(box):
    """Every 'stays quiet' case above would also pass if the prompt never worked.
    This is the control that makes them mean something."""
    assert "First run" in box(answer="n")


def test_main_actually_calls_it():
    """The harness drives _maybe_offer_first_update directly, so every behaviour test
    above still passes if main() never calls it. A function nothing calls is a
    feature nothing has — and reverting exactly that was the one mutation the rest
    of this file did not notice."""
    src = CTL.read_text(encoding="utf-8")
    main = src.split("\nmain() {", 1)[1]
    assert "_maybe_offer_first_update" in main, (
        "sysiblectl defines the first-run prompt and never calls it")
    assert main.index("_maybe_offer_first_update") < main.index('_dispatch "$cmd"'), (
        "the prompt comes after the command it was asked to run")


def _early_verbs(path: Path):
    """The verbs main() handles BEFORE the first-run prompt."""
    main = path.read_text(encoding="utf-8").split("\nmain() {", 1)[1]
    return main[:main.index("_maybe_offer_first_update")]


@pytest.mark.parametrize("path", [
    CTL,
    pytest.param(Path("/home/user/Sysible-Controller/deploy/sysiblectl"), id="standalone"),
])
def test_it_is_not_offered_for_help_or_uninstall(path):
    """`--help` must not ask anything, and neither must the installer: the first-run
    install IS the thing that would be updated.

    `first-run` is required only where it EXISTS. The standalone Controller copy has
    no such verb — it runs SLOP's install.sh, and that repo has no install.sh — so
    demanding it there failed on a difference that is deliberate."""
    if not path.exists():
        pytest.skip("%s is not checked out beside this one" % path)
    early = _early_verbs(path)
    for verb in ("help", "uninstall"):
        assert verb in early, (
            "%r is handled after the prompt in %s, so it would be asked first"
            % (verb, path.name))
    if "cmd_first_run" in path.read_text(encoding="utf-8"):
        assert "first-run" in early, (
            "%s has a first-run verb but handles it after the prompt — the install "
            "itself would be asked whether to update first" % path.name)


# ------------------------------------------------------------- both copies

def test_both_sysiblectl_copies_have_it():
    """SLOP's vendored copy is the source, but the standalone one is what a
    Controller-only checkout runs — a fix in one and not the other is the drift
    this project has already been bitten by twice."""
    other = Path("/home/user/Sysible-Controller/deploy/sysiblectl")
    if not other.exists():
        pytest.skip("the standalone Controller checkout is not beside this one")
    text = other.read_text(encoding="utf-8")
    for name in WANT:
        assert "%s() {" % name in text, (
            "the standalone sysiblectl has no %s — the first-run prompt is only in "
            "one of the two copies" % name)
    assert "_maybe_offer_first_update" in text.split("main() {")[1], (
        "the standalone copy defines the prompt but never calls it")
