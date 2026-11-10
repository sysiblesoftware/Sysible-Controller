"""CSS that is wrong but not loud.

Reported as "the tiles on the System Administration tools page look wrong
compared to the rest of the platform". Three defects, none of which produce an
error anywhere — a browser drops a declaration it cannot parse and renders on:

  1. `align-items: flashback` in four rules. `baseline` is a CSS keyword, and
     commit 4e9ffc7 ("Flashback is the backup client again") renamed it along
     with the product. The declarations were silently discarded, so those flex
     rows lost their intended alignment.

  2. `var(--ambert)` — a typo'd twin of `--amber`, defined once in the default
     :root. It RESOLVED, so nothing looked broken in the default skin. But
     `--amber` is deliberately re-toned per skin (#e0a83b on brand, #b9791a on
     the light "Porcelain" skin, darkened for contrast on white) and `--ambert`
     never was. Every amber consumer — the Distro Subscription tile icon
     included — stayed on the dark-skin yellow in every skin, which on Porcelain
     is light yellow on a light background.

  3. `var(--accent-line)` used by two rules and defined by none, so their
     border-color was dropped.

A stylesheet cannot fail a build, so these checks are the only thing standing
between a token typo and a tile that looks wrong on one skin for months.
"""
import re
from pathlib import Path

import pytest

CSS_PATH = Path(__file__).resolve().parents[1] / "webgui/frontend/src/styles.css"
CSS = CSS_PATH.read_text(encoding="utf-8")

DEFINED = set(re.findall(r'(--[a-zA-Z0-9_-]+)\s*:', CSS))
USED = set(re.findall(r'var\(\s*(--[a-zA-Z0-9_-]+)', CSS))


def test_every_custom_property_used_is_defined():
    """A var() with no definition and no fallback makes the whole declaration
    invalid, and the browser drops it without a word."""
    missing = sorted(v for v in USED - DEFINED if f"var({v}," not in CSS)
    assert not missing, (
        "these custom properties are used but never defined, so every rule "
        f"using them loses that declaration: {missing}"
    )


# Properties whose values are a closed set of keywords. A product name, a typo
# or a stray word here is invalid and silently discarded.
KEYWORD_PROPS = {
    "align-items": {"normal", "stretch", "center", "start", "end", "flex-start",
                    "flex-end", "baseline", "first baseline", "last baseline",
                    "safe center", "unsafe center", "self-start", "self-end",
                    "inherit", "initial", "unset", "revert"},
    "justify-content": {"normal", "flex-start", "flex-end", "center", "start",
                        "end", "left", "right", "space-between", "space-around",
                        "space-evenly", "stretch", "inherit", "initial", "unset"},
    "vertical-align": {"baseline", "sub", "super", "text-top", "text-bottom",
                       "middle", "top", "bottom", "inherit", "initial", "unset"},
    "text-align": {"left", "right", "center", "justify", "start", "end",
                   "match-parent", "inherit", "initial", "unset"},
}


@pytest.mark.parametrize("prop", sorted(KEYWORD_PROPS))
def test_keyword_properties_only_use_real_keywords(prop):
    bad = []
    for m in re.finditer(rf'\b{re.escape(prop)}\s*:\s*([^;}}]+)', CSS):
        val = m.group(1).strip()
        if val.startswith("var("):
            continue
        if val not in KEYWORD_PROPS[prop]:
            line = CSS[:m.start()].count("\n") + 1
            bad.append(f"line {line}: {prop}: {val}")
    assert not bad, (
        f"invalid {prop} values — the browser discards these declarations "
        f"silently:\n  " + "\n  ".join(bad)
    )


def _series(name):
    """(stem, number) for a token carrying exactly one run of digits, else None.

    --ramp3 -> ("--ramp#", 3) and --ramp3-bg -> ("--ramp#-bg", 3), so the step
    colour and its tint are two SEPARATE series rather than one with each number
    appearing twice. The digit run is matched wherever it sits, not only at the
    end: --ramp3-bg's number is in the middle, and keying off the suffix would
    leave those five tokens unexempted — which is exactly the shape that made
    this function necessary.

    Exactly one run, so a token with two numbers in it is not treated as a step
    of anything; there are none today and guessing which number indexes the
    series would be a worse answer than declining to.
    """
    runs = re.findall(r"[0-9]+", name)
    if len(runs) != 1:
        return None
    return (re.sub(r"[0-9]+", "#", name), int(runs[0]))


def test_no_custom_property_is_a_typo_of_another():
    """--ambert vs --amber. The typo RESOLVED, which is why it survived: it was
    defined once in the default scope and simply never re-toned per skin, so it
    pinned every consumer to one skin's colour.

    A deliberate numbered series is exempt: --ramp1 and --ramp2 differ by one
    character BY DESIGN, and the whole point of a ramp is that its steps are
    spelled alike. The exemption is narrow — same stem, both ending in digits —
    so --ambert/--amber, which carry no number, are still caught. The gap check
    below is what keeps the exemption from becoming a hole.
    """
    names = sorted(DEFINED)

    def edit1(a, b):
        if abs(len(a) - len(b)) > 1:
            return False
        if len(a) == len(b):
            return sum(x != y for x, y in zip(a, b)) == 1
        short, long = (a, b) if len(a) < len(b) else (b, a)
        return any(long[:i] + long[i + 1:] == short for i in range(len(long)))

    def same_series(a, b):
        sa, sb = _series(a), _series(b)
        return sa is not None and sb is not None and sa[0] == sb[0]

    pairs = [(a, b) for i, a in enumerate(names) for b in names[i + 1:]
             if edit1(a, b) and not same_series(a, b)]
    assert not pairs, (
        "these custom properties differ by one character, which is how --ambert "
        f"shadowed --amber for months: {pairs}"
    )


def test_a_numbered_series_has_no_gap():
    """The other half of the exemption above.

    --ramp1..--ramp5 are allowed to differ by one character because they are a
    ramp. That same leniency would wave through --ramp4 when the stylesheet only
    ever defines 1, 2, 3 and 5 — a consumer asking for the missing step gets an
    invalid declaration, dropped in silence, which is the exact failure mode this
    file exists for. A series is only exempt while it is complete.
    """
    series = {}
    for name in DEFINED:
        s = _series(name)
        if s:
            series.setdefault(s[0], set()).add(s[1])
    gaps = {}
    for stem, nums in series.items():
        if len(nums) < 2:
            continue  # --panel-2 beside --panel is a pair of names, not a ramp
        missing = sorted(set(range(min(nums), max(nums) + 1)) - nums)
        if missing:
            gaps[stem] = missing
    assert not gaps, (
        "these numbered token series skip a step, so var() for the missing one "
        f"resolves to nothing and its declaration is dropped: {gaps}"
    )


def test_the_flashback_rename_did_not_reach_css_values():
    """Pinned by name. `baseline` is a CSS keyword and a product name, and a
    global rename of the product hit the keyword in four places."""
    hits = [f"line {CSS[:m.start()].count(chr(10)) + 1}"
            for m in re.finditer(r':\s*flashback\s*[;}]', CSS)]
    assert not hits, (
        f"'flashback' is being used as a CSS value at {hits} — the rename hit "
        "the 'baseline' keyword, not just the product name"
    )
