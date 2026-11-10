"""System Administration was a wall of 21 paragraphs.

Each tile carried the tool's FULL description: 3,168 characters of body copy on
one screen, median 145 and longest 249 — roughly 500 words in a tile grid. And
the tiles were sized for it: a 46px icon, 18/20 padding and a 118px floor that
the long ones blew straight through, so no two rows were the same height.

The long text could not simply be deleted. It is also the SEARCH corpus, and the
search box advertises terms like "raid" that appear in no tool's NAME — only in
Storage Administration's description. So it moved off the tile rather than out of
the file: `short` is what the tile shows, `desc` still backs the search and the
tool's own page, and `group` sorts the 21 into five sections instead of one
undifferentiated grid.

Verified by rendering the real markup against the real stylesheet in Chromium:
tiles come out 100px, uniform, with no horizontal overflow at 390px.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
RUNNER = REPO / "webgui" / "frontend" / "src" / "views" / "ToolRunner.jsx"
CSS = REPO / "webgui" / "frontend" / "src" / "styles.css"
SRC = RUNNER.read_text()


def _tools():
    """Split on the entry boundary rather than trying to match a whole object.

    The first version of this used a regex that ended at `\n  }` — which the
    file never contains, since each entry closes on its `desc:` line. It parsed
    NOTHING, and the suite still looked green: every per-tool check here is
    parametrized over this list, and parametrizing over an empty list generates
    no tests at all. Hence the hard count assertion below — a parser that stops
    working must fail loudly, not quietly stop testing.
    """
    block = re.search(r"const TOOLS = \[(.*?)\n\];", SRC, re.S)
    assert block, "the tool catalog is gone"
    out = []
    for chunk in re.split(r"\n\s*\{\s*name:", block.group(1)):
        if "group:" not in chunk:
            continue
        chunk = "name:" + chunk
        name = re.search(r'name:\s*"((?:[^"\\]|\\.)*)"', chunk)
        group = re.search(r'group:\s*"([^"]+)"', chunk)
        short = re.search(r'short:\s*"((?:[^"\\]|\\.)*)"', chunk)
        desc = re.search(r'desc:\s*"((?:[^"\\]|\\.)*)"', chunk)
        if not (name and group):
            continue
        out.append({"name": name.group(1), "group": group.group(1),
                    "short": short.group(1) if short else None,
                    "desc": desc.group(1) if desc else None,
                    "hidden": "hidden: true" in chunk})
    return out


def _groups():
    g = re.search(r"const GROUPS = \[(.*?)\];", SRC, re.S)
    assert g, "GROUPS is gone — the grid would render nothing"
    return re.findall(r'"([^"]+)"', g.group(1))


TOOLS = _tools()
GROUPS = _groups()

# At import, so a broken parser is a COLLECTION error rather than a suite that
# quietly shrinks to nothing. Every per-tool test below is parametrized over
# TOOLS; with an empty list pytest generates zero cases and reports success.
assert len(TOOLS) >= 20, (
    f"only parsed {len(TOOLS)} tools out of the catalog — the parser in this "
    f"file has stopped matching, and every per-tool check would silently vanish"
)


def test_the_catalog_still_parses():
    assert len(TOOLS) >= 20, f"only parsed {len(TOOLS)} tools"


class TestTheTileCarriesALabelNotAParagraph:
    @pytest.mark.parametrize("t", TOOLS, ids=lambda t: t["name"])
    def test_every_tool_has_a_short_label(self, t):
        assert t["short"], f"{t['name']} has no short label, so its tile falls back to prose"

    @pytest.mark.parametrize("t", TOOLS, ids=lambda t: t["name"])
    def test_the_label_is_one_line(self, t):
        """60 characters is about one line in a quarter-width tile. The old
        descriptions ran to 249."""
        assert len(t["short"]) <= 60, (
            f"{t['name']}: {len(t['short'])} chars — that wraps, and wrapping is "
            f"what made the rows ragged"
        )

    def test_the_grid_is_far_lighter_than_it_was(self):
        """The whole complaint, as a number."""
        total = sum(len(t["short"]) for t in TOOLS)
        assert total < 1100, f"{total} characters across the tiles; it was 3,168"

    @pytest.mark.parametrize("t", TOOLS, ids=lambda t: t["name"])
    def test_the_label_does_not_end_in_a_full_stop(self, t):
        """It is a label, not a sentence; a trailing stop reads as truncated prose."""
        assert not t["short"].endswith("."), t["short"]


class TestNothingSearchableWasLost:
    """Moving the long text off the tile must not narrow what is findable."""

    @pytest.mark.parametrize("t", TOOLS, ids=lambda t: t["name"])
    def test_the_long_description_is_still_there(self, t):
        assert t["desc"], f"{t['name']} lost its description — the search corpus"
        assert len(t["desc"]) > len(t["short"]), \
            f"{t['name']}: the description is no longer the fuller text"

    def test_the_search_reads_name_short_and_description(self):
        assert re.search(r"t\.name \+ \" \" \+ t\.short \+ \" \" \+ t\.desc", SRC), \
            "the search no longer covers all three, so advertised terms stop matching"

    @pytest.mark.parametrize("term", ["raid", "selinux", "leapp", "chrony", "fstab", "nftables"])
    def test_a_term_the_search_box_advertises_still_matches(self, term):
        """These appear in NO tool name. If the description ever stops being
        searched, the search box starts lying."""
        hay = " ".join((t["name"] + " " + (t["short"] or "") + " " + (t["desc"] or ""))
                       for t in TOOLS).lower()
        assert term in hay, f"nothing matches {term!r} any more"


class TestTheTwentyOneAreSorted:
    @pytest.mark.parametrize("t", TOOLS, ids=lambda t: t["name"])
    def test_every_tool_is_in_a_known_group(self, t):
        """A tool whose group is not in GROUPS renders in no section at all —
        it would simply vanish from the page."""
        assert t["group"] in GROUPS, (
            f"{t['name']} is in {t['group']!r}, which is not one of {GROUPS}"
        )

    def test_no_group_is_empty(self):
        used = {t["group"] for t in TOOLS if not t["hidden"]}
        for g in GROUPS:
            assert g in used, f"{g!r} has no visible tools — an empty heading"

    def test_no_group_is_as_long_as_the_old_flat_list(self):
        from collections import Counter
        c = Counter(t["group"] for t in TOOLS if not t["hidden"])
        assert max(c.values()) <= 8, f"grouping did not break up the wall: {dict(c)}"

    def test_searching_flattens_the_grid(self):
        """Sections while searching are mostly empty headings; the point of a
        search is the matches, not where they live."""
        assert "searching" in SRC and "? (filtered.length" in SRC


class TestTheTilesAreSizedForALabel:
    CSS_TEXT = CSS.read_text()

    def test_the_paragraph_era_floor_is_gone(self):
        assert ".tool-grid .tile { min-height: 118px; }" not in self.CSS_TEXT

    def test_the_icon_no_longer_sets_the_row_height(self):
        """At 46px it was taller than the two lines of text beside it, so a
        one-line tile was mostly empty square."""
        m = re.search(r"\.tool-grid \.tile-ico \{[^}]*width:\s*(\d+)px", self.CSS_TEXT)
        assert m, "the tool grid no longer sizes its own icon"
        assert int(m.group(1)) <= 34, f"icon is {m.group(1)}px"

    def test_it_fits_more_than_three_across_on_a_wide_screen(self):
        """At three columns, 20 tiles is seven rows."""
        m = re.search(r"\.tool-grid \{[^}]*repeat\((\d+), 1fr\)", self.CSS_TEXT)
        assert m and int(m.group(1)) >= 4, "still three columns"

    @pytest.mark.parametrize("w", [1400, 1040, 700])
    def test_it_steps_down_for_narrow_screens(self, w):
        assert f"max-width: {w}px" in self.CSS_TEXT, \
            f"no breakpoint at {w}px — four columns would survive onto a phone"


class TestTheIconsUseTheBrandRamp:
    """The grid had twelve hues — coral, amber, rose, copper, crimson, purple —
    with no relationship to the brand, so colour was decoration. It is now a
    blue-to-green blend between the two Sysible brand colours, one step per
    section, which makes colour carry the grouping instead."""

    CSS_TEXT = CSS.read_text()
    STEPS = 5

    @pytest.mark.parametrize("n", range(1, STEPS + 1))
    def test_each_step_has_a_class(self, n):
        assert f".ico-ramp{n}" in self.CSS_TEXT

    def test_every_tool_uses_a_ramp_step(self):
        off = [t["name"] for t in TOOLS
               if not re.fullmatch(r"ico-ramp[1-5]", _cls_of(t["name"]) or "")]
        assert not off, f"these are still on an off-brand hue: {off}"

    def test_colour_follows_the_group(self):
        """Two tools in the same section must not differ in colour, or the ramp
        stops meaning anything."""
        by_group = {}
        for t in TOOLS:
            by_group.setdefault(t["group"], set()).add(_cls_of(t["name"]))
        for g, classes in by_group.items():
            assert len(classes) == 1, f"{g} uses {sorted(classes)}"

    def test_each_group_has_its_own_step(self):
        used = {_cls_of(t["name"]) for t in TOOLS if not t["hidden"]}
        assert len(used) == len(GROUPS), \
            f"{len(GROUPS)} sections but {len(used)} colours: {sorted(used)}"

    def test_the_endpoints_are_the_brand_colours(self):
        assert "--ramp1: #5580ee" in self.CSS_TEXT, "the ramp no longer starts at brand blue"
        assert "--ramp5: #43a047" in self.CSS_TEXT, "the ramp no longer ends at brand green"

    def test_the_light_theme_has_its_own_values(self):
        """The dark values reach only 2.5-2.8:1 against their own tint on white.
        An icon is a graphical object, so 3:1 is the floor."""
        light = re.search(r':root\[data-theme="light"\]\s*\{(.*?)\n\}',
                          self.CSS_TEXT, re.S)
        assert light and "--ramp1:" in light.group(1), \
            "the light theme inherits the dark ramp and fails contrast"

    @pytest.mark.parametrize("theme,panel", [("dark", "#171c26"), ("light", "#ffffff")])
    def test_every_step_clears_three_to_one(self, theme, panel):
        vals = _ramp_values(self.CSS_TEXT, theme)
        assert len(vals) == self.STEPS, f"{theme}: found {len(vals)} ramp values"
        for name, hexv in sorted(vals.items()):
            r = _contrast(hexv, _mix(hexv, panel, 0.15))
            assert r >= 3.0, f"{theme} {name} {hexv} is {r:.2f}:1 on its own tint"


def _cls_of(name):
    m = re.search(r'name:\s*"' + re.escape(name) + r'".*?cls:\s*"([^"]+)"',
                  SRC, re.S)
    return m.group(1) if m else None


def _ramp_values(css, theme):
    """The ramp declarations for a theme, found structurally.

    A selector regex is the wrong tool here: the dark block is a GROUPED
    selector (`:root,\n:root[data-skin="base"] {`), so matching `:root\s*\{`
    skips it entirely and the check silently finds nothing to test. Walk out
    from each declaration to its own block instead.
    """
    out = {}
    for m in re.finditer(r"--ramp\d:\s*#[0-9a-f]{6}", css):
        open_brace = css.rfind("{", 0, m.start())
        close_brace = css.find("}", m.end())
        if open_brace < 0 or close_brace < 0:
            continue
        prev_close = css.rfind("}", 0, open_brace)
        selector = css[prev_close + 1:open_brace]
        is_light = 'data-theme="light"' in selector
        if is_light != (theme == "light"):
            continue
        out.update(re.findall(r"--(ramp\d):\s*(#[0-9a-f]{6})",
                              css[open_brace:close_brace]))
    return out


def _srgb_to_lin(c):
    c /= 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _lum(h):
    r, g, b = (_srgb_to_lin(int(h[i:i + 2], 16)) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _mix(fg, bg, a):
    return "#%02x%02x%02x" % tuple(
        round(int(fg[i:i + 2], 16) * a + int(bg[i:i + 2], 16) * (1 - a)) for i in (1, 3, 5))


def _contrast(a, b):
    l1, l2 = sorted((_lum(a), _lum(b)), reverse=True)
    return (l1 + 0.05) / (l2 + 0.05)
