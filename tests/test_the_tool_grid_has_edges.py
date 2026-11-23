"""The tool grid's cards are separable from the page they sit on.

Reported as the screen being hard to look at. The type was never the problem --
title 13.3:1, caption 6.2:1, section heading 5.5:1 all passed AA comfortably.
What failed was the structure:

    tile surface vs page   1.09:1
    tile border  vs page   1.33:1      (WCAG 1.4.11 asks 3:1 of a boundary)

Cards the same brightness as their page, edged in something barely darker than
either, over a topographic field that runs behind everything. Low-contrast
containers on a textured ground leave the eye to do all the grouping itself.

A dark theme cannot reach a true 3:1 edge without reading as a wireframe
(#5a6375 is still 2.91:1), so this pins what was actually chosen -- surface and
edge together -- rather than a standard it cannot meet. The numbers are the
point: next time someone adjusts this, the question "is it still separable" has
an answer instead of an opinion.
"""
import pathlib
import re

import pytest

CSS = (pathlib.Path(__file__).resolve().parents[1]
       / "webgui" / "frontend" / "src" / "styles.css").read_text(encoding="utf-8")
TILE_TOKENS = ("--tile-face", "--tile-edge", "--tile-face-hover", "--tile-edge-hover")


def _srgb(c):
    c = c / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _lum(h):
    h = h.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _srgb(r) + 0.7152 * _srgb(g) + 0.0722 * _srgb(b)


def _ratio(a, b):
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _skins():
    """Every block that sets its own --bg: one ground, one set of cards."""
    out = {}
    for m in re.finditer(r"([^{}]*)\{([^{}]*)\}", CSS):
        body = m.group(2)
        if "--bg:" not in body:
            continue
        sel = (m.group(1).strip().splitlines() or ["?"])[-1].strip()
        toks = dict(re.findall(r"(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{3,8})\s*;", body))
        if "--bg" in toks:
            out[sel] = toks
    return out


def test_there_is_more_than_one_skin_to_check():
    """A guard on the guard: if the extraction stops matching, every test below
    passes by finding nothing."""
    assert len(_skins()) >= 3, _skins().keys()


@pytest.mark.parametrize("sel", list(_skins()))
def test_every_skin_defines_the_tile_tokens(sel):
    """THE NEAR MISS. These were first added to the base skin alone, which
    leaves `background: var(--tile-face)` resolving to nothing under the others
    -- transparent cards, which is worse than the low-contrast ones."""
    toks = _skins()[sel]
    missing = [t for t in TILE_TOKENS if t not in toks]
    assert not missing, f"{sel} sets its own --bg but not {missing}"


@pytest.mark.parametrize("sel", list(_skins()))
def test_a_card_is_separable_from_its_page(sel):
    toks = _skins()[sel]
    bg = toks["--bg"]
    face, edge = toks["--tile-face"], toks["--tile-edge"]
    # Where it started: 1.09 and 1.33. Both of these are what the eye reads as
    # "this is a thing sitting on that".
    assert _ratio(face, bg) >= 1.12, (
        f"{sel}: card face {face} is {_ratio(face, bg):.2f}:1 against {bg}")
    assert _ratio(edge, bg) >= 2.4, (
        f"{sel}: card edge {edge} is {_ratio(edge, bg):.2f}:1 against {bg} "
        f"(was 1.33:1, which is what made this screen tiring)")


@pytest.mark.parametrize("sel", list(_skins()))
def test_the_lift_did_not_cost_the_text(sel):
    """Raising the card toward the text colour is the obvious way to get
    separation and the wrong one. Body copy stays above AA on the new face."""
    toks = _skins()[sel]
    face = toks["--tile-face"]
    for name in ("--text", "--text-dim"):
        if name not in toks:
            continue
        assert _ratio(toks[name], face) >= 4.5, (
            f"{sel}: {name} is {_ratio(toks[name], face):.2f}:1 on the card face")


def test_hover_still_moves():
    """A hover state that no longer differs from the resting one is a control
    that stopped answering."""
    for sel, toks in _skins().items():
        if "--tile-face-hover" not in toks:
            continue
        assert toks["--tile-face-hover"] != toks["--tile-face"], sel
        assert toks["--tile-edge-hover"] != toks["--tile-edge"], sel


def test_the_field_is_settled_on_this_page_not_turned_down_everywhere():
    """body::before draws a PLATFORM brand element -- the portal, the sign-in
    page, Flashback and the Visualizer all render the same field. Calming it
    globally would leave this one app disagreeing with the others about what the
    background is, so it is scoped to the page that measured badly."""
    assert ".tools-page::before" in CSS, "the field is not settled anywhere"
    body_before = CSS[CSS.index("body::before"):][:400]
    assert "opacity" not in body_before, \
        "the shared brand field was turned down globally instead of per page"

def test_the_scale_is_not_owned_by_a_palette():
    """Radius and spacing lived inside :root[data-skin="brand"], the block that
    holds the Sysible colours. Nothing about a corner radius changes when the
    palette does -- but the ownership was real: under data-skin="base",
    `gap: var(--sp-3)` and `border-radius: var(--r-md)` resolved to nothing, and
    that skin rendered this grid as one continuous slab of square, gapless cards.

    Found by rendering it, not by reading it. index.html hard-codes
    data-skin="brand" on every load, so `base` never runs and the broken layout
    had nothing to break.
    """
    for tok in ("--sp-3", "--r-md"):
        owner = None
        for m in re.finditer(r"([^{}]*)\{([^{}]*)\}", CSS):
            if re.search(rf"{tok}\s*:", m.group(2)):
                owner = (m.group(1).strip().splitlines() or ["?"])[-1].strip()
                break
        assert owner == ":root", (
            f"{tok} is defined in {owner!r}; a skin that does not redefine it "
            f"renders without it")
