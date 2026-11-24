"""A rail section heading reads as a label, not as another nav item.

Reported as wanting differentiation between OPERATE and the Quick System
Actions under it -- "maybe a bold or something".

The heading was already bold (600) and uppercase and smaller. What it had was
11px against 14px, one step of grey apart, 14px of padding, and nothing else
between them, so the rail read as one continuous run.

THE FIX IS NOT TO MAKE IT LOUDER. The rule above .rail-group has said for a
long time that these are "quiet on purpose: a reading aid, not a control...
never the thing the eye lands on first", and that is right -- a section label
that competes with its own contents is a worse rail, not a better one. The
colour is therefore unchanged. What was missing was SEPARATION: a rule above
the group, more air, and wider tracking.

Both halves are pinned below, because the obvious next edit is to brighten it.
"""
import pathlib
import re

import pytest

CSS = (pathlib.Path(__file__).resolve().parents[1]
       / "webgui" / "frontend" / "src" / "styles.css").read_text(encoding="utf-8")
NC = re.sub(r"/\*.*?\*/", "\n", CSS, flags=re.S)


def _rule(selector):
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", NC)
    assert m, f"{selector} is gone"
    return dict((k.strip(), v.strip()) for k, v in
                (d.split(":", 1) for d in m.group(1).split(";") if ":" in d))


def _skins():
    out = []
    for skin, theme in (("base", None), ("brand", None), ("brand", "light")):
        t = {}
        for m in re.finditer(r"([^{}]*)\{([^{}]*)\}", NC):
            arms = [" ".join(a.split()) for a in m.group(1).split(",")]
            if any(a == ":root" or f'data-skin="{skin}"' in a
                   or (theme and f'data-theme="{theme}"' in a) for a in arms):
                t.update(dict(re.findall(r"(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{3,8})\s*;", m.group(2))))
        out.append((skin + ("+" + theme if theme else ""), t))
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


def test_a_group_is_separated_from_the_one_above_it():
    """The actual fix. Without a rule between them the eye has only a few
    pixels of padding to go on."""
    g = _rule(".rail-group")
    assert "border-top" in g and "none" not in g["border-top"], \
        "nothing separates a group from the items above it"
    assert "margin-top" in g, "the groups are not spaced apart"


def test_the_first_group_has_no_rule_above_it():
    """It would read as an underline on the brand mark."""
    g = _rule(".rail-group:first-child")
    assert g.get("border-top") == "none", g
    assert g.get("margin-top") == "0", g


def test_it_still_reads_as_a_label_not_a_word():
    g = _rule(".rail-group")
    assert g.get("text-transform") == "uppercase"
    assert float(g["letter-spacing"].rstrip("em")) >= 0.1, g["letter-spacing"]
    assert int(g["font-weight"]) >= 700, g["font-weight"]
    assert g["font-size"] == "var(--fs-xs)", "a heading as large as an item is an item"


def _declared_colour(selector):
    """The token each rule ACTUALLY paints with.

    Not --text-faint by assumption: the first version of this test compared the
    two tokens directly, so changing the rule's own `color:` to var(--text)
    brightened the heading and the test went on passing. Found by mutation.
    """
    rule = _rule(selector)
    val = rule.get("color", "")
    m = re.search(r"var\((--[\w-]+)", val)
    assert m, f"{selector} paints with {val!r}, which this cannot resolve"
    return m.group(1)


@pytest.mark.parametrize("name,t", _skins())
def test_the_heading_stays_quieter_than_what_it_labels(name, t):
    """The rule's own long-standing reason, kept. A label that outshouts its
    contents is a worse rail -- and brightening this is the obvious next edit,
    which is why it is pinned."""
    bg = t["--rail-bg"]
    head = _ratio(t[_declared_colour(".rail-group")], bg)
    item = _ratio(t[_declared_colour(".rail-item")], bg)
    assert head < item, (
        f"{name}: the heading ({head:.2f}:1) is no longer quieter than the "
        f"items it introduces ({item:.2f}:1)")


@pytest.mark.parametrize("name,t", _skins())
def test_but_the_heading_is_still_readable(name, t):
    """Quiet is not invisible. AA for small text."""
    r = _ratio(t[_declared_colour(".rail-group")], t["--rail-bg"])
    assert r >= 4.5, f"{name}: the heading is {r:.2f}:1 on the rail"
