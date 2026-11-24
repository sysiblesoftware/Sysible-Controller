"""A tool-grid card is separable from the page it sits on, in every skin.

The screen was reported as hard to look at. The type was never the problem --
title 13.3:1, caption 6.2:1, section heading 5.5:1 all pass AA. The structure
was:

                  card face vs page    card edge vs page
    base               1.09:1               1.33:1
    brand              1.10:1               1.42:1
    light              1.13:1               1.16:1

Cards the same brightness as their page, edged in something barely different
from either, over a topographic field that runs behind everything.

The numbers below are the point. A dark theme cannot reach WCAG 1.4.11's 3:1
for a boundary without the edge reading as a wireframe, so this pins what was
actually chosen rather than a standard it cannot meet -- and the next person to
adjust it gets an answer instead of an opinion.

Tokens are resolved the way a browser would, per skin, because the FIRST
attempt at this fix broke the palette block's selector and the checks of the
day carried on reading the wrong block and passing.
"""
import pathlib
import re

import pytest

CSS = (pathlib.Path(__file__).resolve().parents[1]
       / "webgui" / "frontend" / "src" / "styles.css").read_text(encoding="utf-8")
NC = re.sub(r"/\*.*?\*/", "\n", CSS, flags=re.S)
SKINS = [("base", None), ("brand", None), ("brand", "light")]
TILE = ("--tile-face", "--tile-edge", "--tile-face-hover", "--tile-edge-hover")


def _resolve(skin, theme=None):
    """Tokens in force for :root, honouring EVERY arm of a comma selector."""
    out = {}
    for m in re.finditer(r"([^{}]*)\{([^{}]*)\}", NC):
        arms = [" ".join(a.split()) for a in m.group(1).split(",")]
        if any(a == ":root" or f'data-skin="{skin}"' in a
               or (theme and f'data-theme="{theme}"' in a) for a in arms):
            out.update(dict(re.findall(r"(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{3,8})\s*;", m.group(2))))
    return out


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


def _name(skin, theme):
    return skin + ("+" + theme if theme else "")


@pytest.mark.parametrize("skin,theme", SKINS)
def test_every_skin_defines_the_tile_tokens(skin, theme):
    """They were added to one skin first, once. var(--tile-face) resolving to
    nothing leaves a TRANSPARENT card, which is worse than a low-contrast one."""
    t = _resolve(skin, theme)
    missing = [x for x in TILE if x not in t]
    assert not missing, f"{_name(skin, theme)} resolves {missing} to nothing"


@pytest.mark.parametrize("skin,theme", SKINS)
def test_the_edge_is_what_makes_a_card_a_card(skin, theme):
    """The one number that moved most: 1.16-1.42 before, and on a light ground
    the edge is ALL the separation there is -- a white card cannot lift off a
    near-white page."""
    t = _resolve(skin, theme)
    r = _ratio(t["--tile-edge"], t["--bg"])
    assert r >= 2.4, f"{_name(skin, theme)}: edge is {r:.2f}:1 against the page"


@pytest.mark.parametrize("skin,theme", [(s, th) for s, th in SKINS if th != "light"])
def test_a_dark_card_also_lifts_off_its_page(skin, theme):
    t = _resolve(skin, theme)
    r = _ratio(t["--tile-face"], t["--bg"])
    assert r >= 1.18, f"{_name(skin, theme)}: face is {r:.2f}:1, was 1.09:1"


@pytest.mark.parametrize("skin,theme", SKINS)
def test_the_lift_did_not_cost_the_text(skin, theme):
    """Raising the card toward the text colour is the obvious way to get
    separation and the wrong one."""
    t = _resolve(skin, theme)
    for tok in ("--text", "--text-dim"):
        r = _ratio(t[tok], t["--tile-face"])
        assert r >= 4.5, f"{_name(skin, theme)}: {tok} is {r:.2f}:1 on the card"


@pytest.mark.parametrize("skin,theme", SKINS)
def test_hover_still_moves(skin, theme):
    """A hover state equal to the resting one is a control that stopped
    answering."""
    t = _resolve(skin, theme)
    assert t["--tile-face-hover"] != t["--tile-face"], _name(skin, theme)
    assert t["--tile-edge-hover"] != t["--tile-edge"], _name(skin, theme)


def test_the_shared_background_field_is_left_alone():
    """body::before draws a PLATFORM brand element -- the portal, the sign-in
    page, Flashback and the Visualizer all render the same field. Turning it
    down to calm this one screen would leave this app disagreeing with the rest
    about what the background is."""
    before = NC[NC.index("body::before"):][:400]
    assert "opacity" not in before, "the shared brand field was turned down"


def test_the_rule_is_scoped_to_the_tool_grid():
    """.tile is this grid's card. Styling .tile bare would reach anything else
    that ever borrows the class."""
    assert ".tool-grid .tile {" in NC
    m = re.search(r"\n\.tile\s*\{[^}]*--tile-(face|edge)", NC)
    assert not m, "the tile colours were applied to .tile globally"
