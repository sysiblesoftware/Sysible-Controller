"""Any output the Controller puts on screen has to be copyable.

Asked for across the platform: "I should be able to copy logs from ANYWHERE in the
platforms." The Controller could already copy the things it asks you to paste
somewhere else -- an enrollment command, a generated password, a command preview --
but not the output it produces. Per-host results, package output, the activity log
and both update logs had no Copy at all, and those are exactly what you need when
something went wrong. Drag-selecting a pane that is still streaming does not work:
it auto-scrolls, so the selection is lost every time a line lands.
"""
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "webgui" / "frontend" / "src"
BTN = (SRC / "components" / "CopyButton.jsx")


def test_there_is_one_copy_control():
    assert BTN.exists(), "no shared CopyButton — every pane would roll its own"
    txt = BTN.read_text(encoding="utf-8")
    assert "export default function CopyButton" in txt


@pytest.mark.parametrize("rel,what", [
    ("components/HostResults.jsx", "a host's command output"),
    ("components/PkgResults.jsx", "a host's package output"),
    ("views/LiveActivity.jsx", "the activity log"),
    ("views/Settings.jsx", "the controller update log"),
    ("views/Updates.jsx", "a host's update output"),
])
def test_each_output_pane_can_be_copied(rel, what):
    txt = (SRC / rel).read_text(encoding="utf-8")
    assert "CopyButton" in txt, "%s renders %s with no way to copy it" % (rel, what)
    assert re.search(r"import CopyButton from", txt), "%s uses CopyButton without importing it" % rel


def test_no_output_pane_is_left_out():
    """A new <pre> of captured output must not ship without a Copy. ErrorBoundary is
    excluded on purpose: it renders a React stack for a crash, not run output."""
    missing = []
    for f in sorted(list(SRC.glob("views/*.jsx")) + list(SRC.glob("components/*.jsx"))):
        if f.name == "ErrorBoundary.jsx":
            continue
        txt = f.read_text(encoding="utf-8")
        if "<pre" in txt and "CopyButton" not in txt:
            missing.append(f.name)
    assert not missing, "output panes with no way to copy them: %s" % missing


@pytest.mark.parametrize("rel,anchor,var", [
    ("views/LiveActivity.jsx", 'label="Copy log"', "log"),
    ("views/Settings.jsx", 'label="Copy log"', "ctrlLog"),
])
def test_a_long_log_is_not_stringified_on_every_render(rel, anchor, var):
    """Checked at the log's OWN button: a file-level regex passed against a reverted
    LiveActivity because the command-copy button in the same file is also lazy."""
    txt = (SRC / rel).read_text(encoding="utf-8")
    i = txt.index(anchor)
    btn = txt[max(0, i - 300):i + 100]
    assert re.search(r"text=\{\(\) =>\s*" + var, btn), (
        "%s materialises the whole log on every render instead of on the click:\n%s"
        % (rel, btn))


def test_the_clipboard_can_fail_and_the_button_says_so():
    txt = BTN.read_text(encoding="utf-8")
    assert 'execCommand("copy")' in txt, "no fallback when navigator.clipboard is unavailable"
    assert "Blocked" in txt, "a failed copy is not reported"
    assert "setTimeout" in txt, "the confirmation never reverts"


def test_the_copy_control_matches_the_one_in_slep():
    """The two apps ship the same control. If one grows a fix the other should too --
    this is the drift that has already bitten the vendored Controller twice."""
    slep = SRC.parents[3] / "slep" / "webgui" / "frontend" / "src" / "ui.jsx"
    if not slep.exists():
        pytest.skip("SLEP tree not beside this one")
    mine, theirs = BTN.read_text(encoding="utf-8"), slep.read_text(encoding="utf-8")
    # Whole calls, not bare names: "setSelectionRange" is a substring of
    # "setSelectionRangeX", so a typo'd call passed this check.
    for behaviour in (r"typeof text === ", r"\.setSelectionRange\(0, s\.length\)",
                      r"execCommand\(.copy.\)", r"Blocked", r"✓ Copied",
                      r"document\.body\.removeChild\(ta\)"):
        for name, src in (("controller", mine), ("slep", theirs)):
            assert re.search(behaviour, src), (
                "%s's CopyButton is missing %s, so the two apps behave differently"
                % (name, behaviour))


def test_no_button_calls_the_clipboard_directly():
    """Three buttons did `navigator.clipboard?.writeText(x).then(...)`. On a plain
    -http deployment navigator.clipboard is undefined, so the optional chain yields
    undefined and .then() throws -- the button did nothing AND raised. A fourth
    dropped the .then() and showed "Copied" whether or not anything was copied."""
    offenders = []
    for f in sorted(list(SRC.glob("views/*.jsx")) + list(SRC.glob("components/*.jsx"))):
        if f.name == "CopyButton.jsx":
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if "navigator.clipboard" not in line:
                continue
            # A programmatic copy inside try/catch (reissued token) is not a button.
            if "try {" in line and "catch" in line:
                continue
            offenders.append("%s:%d" % (f.name, i))
    assert not offenders, (
        "these write to the clipboard outside CopyButton: %s — "
        "`navigator.clipboard?.writeText(x).then()` throws where the API is absent"
        % offenders)


def test_the_broken_optional_chain_pattern_is_gone():
    for f in list(SRC.glob("views/*.jsx")) + list(SRC.glob("components/*.jsx")):
        txt = f.read_text(encoding="utf-8")
        assert "clipboard?.writeText" not in txt, (
            "%s still uses navigator.clipboard?.writeText — undefined?.writeText(x) "
            "is undefined, and calling .then() on it throws" % f.name)
