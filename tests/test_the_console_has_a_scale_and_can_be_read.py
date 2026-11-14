"""Sizes come from a scale, colours from tokens, and both themes are legible.

From the UI/UX audit. Three findings, and one defect that only measuring
found.

THE SCALES. The stylesheet used sixteen font sizes across 94 declarations --
10, 10.5, 11, 11.5, 12, 12.5, 13, 13.5, 14, 15, 16, 17, 18, 20, 22, 26 --
with nine of them inside a 4px band. Sizes that close do not read as
different; they read as unaligned. Ten border radii and fifteen gap values
told the same story. They are six, four and five steps now, and every old
value moved to its nearest step, so nothing shifted more than 2px.

THE COLOURS. Twenty hex values sat in rules rather than tokens. Six of them
were FALLBACKS that disagreed with the token they backed -- `var(--red,
#eb4d4b)` where --red is #e5534b -- so if the token ever went missing the
console would quietly render a different palette instead of failing.

AND THE DEFECT. `.btn { background: var(--btn); color: #fff }` is correct in
dark theme, where --btn is #1a2233. In LIGHT theme --btn is #eef2f8: white on
near-white, 1.12:1. Every default button in the console had a label that could
not be seen, in a theme that ships and that nothing tested. Four rules had it.
Three more put white on a coloured fill, which fails AA on all of ours.

So the contrast checks below run over BOTH themes. That is the part worth
keeping: a token pair is only correct in the theme you looked at.

Run: pytest tests/
"""
import re
from pathlib import Path

import pytest

CSS = (Path(__file__).resolve().parents[1] / "webgui" / "frontend" / "src"
       / "styles.css").read_text(encoding="utf-8")
NC = re.sub(r"/\*.*?\*/", " ", CSS, flags=re.S)   # comments are never rules


def _block(selector: str) -> dict:
    i = NC.find(selector)
    assert i >= 0, f"no {selector} block"
    body = NC[NC.index("{", i) + 1: NC.index("}", i)]
    return {m.group(1): m.group(2).strip()
            for m in re.finditer(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", body)}


DARK = _block(':root[data-skin="brand"]')
LIGHT = _block(':root[data-theme="light"]')
BASE = _block(":root,")


def tok(theme: dict, name: str) -> str:
    """A token's value in a theme, falling back through the layers as CSS does."""
    for layer in (theme, DARK, BASE):
        if name in layer and layer[name].startswith("#"):
            return layer[name].split()[0]
    raise AssertionError(f"{name} resolves to no colour")


# ---- the scales exist and are scales ---------------------------------------
TYPE = ["--fs-xs", "--fs-sm", "--fs-md", "--fs-lg", "--fs-xl", "--fs-2xl"]
RADII = ["--r-xs", "--r-sm", "--r-md", "--r-pill"]
SPACE = ["--sp-0", "--sp-1", "--sp-2", "--sp-3", "--sp-4"]


@pytest.mark.parametrize("name", TYPE + RADII + SPACE)
def test_every_scale_step_is_defined(name):
    assert name in DARK, f"{name} is not defined in the rendered token block"


def test_the_type_steps_are_far_enough_apart_to_read_as_different():
    """The point of the exercise. 12 and 12.5 are not two sizes."""
    px = [float(DARK[n].rstrip("px")) for n in TYPE]
    assert px == sorted(px), f"the type scale is not in order: {px}"
    gaps = [b - a for a, b in zip(px, px[1:])]
    assert min(gaps) >= 1.5, f"two steps are {min(gaps)}px apart: {px}"


def test_the_scales_did_not_quietly_grow_back():
    assert len(TYPE) <= 7, "a seventh type step has appeared"
    assert len(RADII) <= 4 and len(SPACE) <= 5


# ---- nothing bypasses them -------------------------------------------------
# A circle, or a per-corner list (two to four lengths, any of which may be a
# bare 0 -- "0 3px 3px 0" is a tab, not a scale value).
_RADIUS_OK = re.compile(
    r"border-radius:\s*(50%|[0-9.]+(?:px)?(?:\s+[0-9.]+(?:px)?){1,3})\s*[;}]")


@pytest.mark.parametrize("prop", ["font-size", "gap"])
def test_no_literal_size_is_left(prop):
    bad = re.findall(prop + r":\s*([0-9.]+px)\s*[;}]", NC)
    assert not bad, f"{prop} is still written as a literal {len(bad)} times: {sorted(set(bad))}"


def test_no_literal_radius_is_left():
    """50% circles and corner-specific radii are not scale values."""
    all_decl = re.findall(r"border-radius:\s*([^;}]+)[;}]", NC)
    bad = [d.strip() for d in all_decl
           if "var(--" not in d and not _RADIUS_OK.match(f"border-radius: {d};")]
    assert not bad, f"literal radii left: {bad}"


def test_colours_come_from_tokens():
    """The terminal's black ground is the one literal that is not a palette
    decision -- a terminal is black."""
    bad = []
    for m in re.finditer(r"([^{}]*)\{([^{}]*)\}", NC):
        sel, body = m.group(1).strip(), m.group(2)
        if sel.startswith(":root") and "--" in body:
            continue        # a token block is where values belong
        for hexv in re.findall(r"#[0-9a-fA-F]{3,8}\b", body):
            if hexv.lower() == "#000" and "term-host" in sel:
                continue
            bad.append(f"{sel.strip()[:40]}: {hexv}")
    assert not bad, "hard-coded colours in rules:\n  " + "\n  ".join(bad)


def test_no_fallback_disagrees_with_the_token_it_backs():
    """`var(--red, #eb4d4b)` where --red is #e5534b. It can only fire if the
    token is deleted, and then it renders a DIFFERENT palette rather than
    failing where someone would notice."""
    bad = []
    for name, fallback in re.findall(r"var\((--[a-z0-9-]+),\s*(#[0-9a-fA-F]{3,8})\)", NC):
        try:
            actual = tok(DARK, name)
        except AssertionError:
            continue        # genuinely undefined: the fallback IS the value
        if actual.lower() != fallback.lower():
            bad.append(f"var({name}, {fallback}) but {name} is {actual}")
    assert not bad, "\n  ".join(bad)


# ---- and both themes can be read -------------------------------------------
def _lum(h):
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    def chan(c):
        c = int(c, 16) / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (chan(h[i:i + 2]) for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


THEMES = {"dark": DARK, "light": LIGHT}


@pytest.mark.parametrize("theme", sorted(THEMES))
def test_a_button_label_can_be_seen(theme):
    """THE one this test was written for: 1.12:1 in light theme."""
    t = THEMES[theme]
    r = contrast(tok(t, "--btn-text"), tok(t, "--btn"))
    assert r >= 4.5, f"{theme}: button label is {r:.2f}:1 on its own button"


@pytest.mark.parametrize("theme,fill", [(t, f) for t in sorted(THEMES)
                                        for f in ("--accent", "--red")])
def test_text_on_a_coloured_fill_clears_aa(theme, fill):
    t = THEMES[theme]
    r = contrast(tok(t, "--on-accent"), tok(t, fill))
    assert r >= 4.5, f"{theme}: --on-accent on {fill} is {r:.2f}:1"


@pytest.mark.parametrize("theme,ink,floor", [
    (t, i, f) for t in sorted(THEMES)
    for i, f in (("--text", 7.0), ("--text-dim", 4.5), ("--accent", 4.5),
                 ("--accent2", 4.5))])
def test_text_is_readable_on_the_ground(theme, ink, floor):
    t = THEMES[theme]
    r = contrast(tok(t, ink), tok(t, "--bg"))
    assert r >= floor, f"{theme}: {ink} is {r:.2f}:1 on --bg (needs {floor})"


def test_the_accent_is_the_same_hue_in_both_themes():
    """Green in dark and blue in light is two brands with a toggle."""
    import colorsys
    hues = {}
    for name, t in THEMES.items():
        h = tok(t, "--accent").lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
        hues[name] = colorsys.rgb_to_hls(r, g, b)[0] * 360
    spread = max(hues.values()) - min(hues.values())
    assert spread < 30, f"the accent hue differs by {spread:.0f} degrees: {hues}"


# ---- breakpoints -----------------------------------------------------------
# Four, and four is the honest number: the tools grid runs 4 -> 3 -> 2 -> 1
# columns, which needs three steps of its own, and the widest layout needs one
# above those. It was eight -- 700, 720, 820, 900, 1040, 1180, 1300, 1400 --
# with pairs twenty pixels apart. Those are not a system; they are one-off
# patches, each added to fix one screen and each now a place where some other
# screen reflows for no reason.
#
# var() does not work inside @media, so these cannot be tokens. This test is
# the only thing holding the set together.
_BREAKPOINTS = {720, 900, 1180, 1400}


def test_the_breakpoints_are_the_declared_set():
    used = {int(x) for x in re.findall(r"@media[^{]*max-width:\s*(\d+)px", CSS)}
    assert used, "no max-width media queries found; this test has stopped looking"
    extra = used - _BREAKPOINTS
    assert not extra, (
        f"{sorted(extra)} are not in the breakpoint set {sorted(_BREAKPOINTS)}. "
        f"A one-off width fixes one screen and moves another.")


def test_the_breakpoints_are_far_enough_apart_to_be_separate():
    """700 and 720 were two breakpoints twenty pixels apart."""
    bp = sorted(_BREAKPOINTS)
    gaps = [b - a for a, b in zip(bp, bp[1:])]
    assert min(gaps) >= 150, f"two breakpoints are {min(gaps)}px apart: {bp}"


def test_every_declared_breakpoint_is_actually_used():
    """A set that lists widths nothing uses is a set nobody maintains."""
    used = {int(x) for x in re.findall(r"@media[^{]*max-width:\s*(\d+)px", CSS)}
    unused = _BREAKPOINTS - used
    assert not unused, f"{sorted(unused)} is declared but no rule uses it"
