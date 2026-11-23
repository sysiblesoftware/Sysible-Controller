"""Every token the stylesheet uses resolves under the skin the app actually sets.

I broke this and shipped it. The palette block's selector is two arms:

    :root,
    :root[data-skin="base"] {

The bare `:root` is what makes those tokens apply under EVERY skin. Moving the
spacing scale out of the brand block, I anchored the insert on
`:root[data-skin="base"] {` -- which matched the SECOND LINE of that selector --
and spliced the new block between the two arms. The palette block kept only its
attribute arm, index.html hard-codes data-skin="brand", and so it stopped
applying at all: 36 tokens went undefined, --font and --topo among them. The
reported symptom was "why did you change the font on controller".

Nothing caught it. The console's own scale test reads its blocks with
_block(":root,"), which found the text and then took the first `{` after it --
by then my block's -- so it carried on checking the wrong block and passing.
Two guards agreeing with each other about the wrong object.

So this one does not read blocks by name. It resolves tokens the way a browser
would for the skin index.html sets, and asks whether anything is left dangling.
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1] / "webgui" / "frontend"
CSS = (ROOT / "src" / "styles.css").read_text(encoding="utf-8")
INDEX = (ROOT / "index.html").read_text(encoding="utf-8")


def shipped_skin():
    """The skin the app sets on every load -- not the one the file lists first."""
    m = re.search(r'setAttribute\(\s*["\']data-skin["\']\s*,\s*["\'](\w+)["\']', INDEX)
    assert m, "index.html no longer pins a skin; this test needs to know which one"
    return m.group(1)


def _resolved(skin):
    """Tokens in force for :root when data-skin is `skin`, in source order.

    Every ARM of a comma selector is considered separately, which is the whole
    point: a block reached only through its bare `:root` arm is exactly what was
    lost."""
    nc = re.sub(r"/\*.*?\*/", "\n", CSS, flags=re.S)
    out = {}
    for m in re.finditer(r"([^{}]*)\{([^{}]*)\}", nc):
        arms = [" ".join(a.split()) for a in m.group(1).split(",")]
        if any(a == ":root" or f'data-skin="{skin}"' in a for a in arms):
            out.update(dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", m.group(2))))
    return out


def test_the_shipped_skin_is_knowable():
    assert shipped_skin() in ("base", "brand"), shipped_skin()


def test_no_token_is_left_dangling():
    """A var() with no fallback that resolves to nothing is not a styling bug
    with a wrong colour -- the declaration is dropped entirely, so the element
    falls back to the browser's own default. That is what changed the font."""
    resolved = _resolved(shipped_skin())
    dangling = sorted(
        t for t in set(re.findall(r"var\((--[\w-]+)\s*\)", CSS))   # no fallback
        if t not in resolved)
    assert not dangling, (
        f"these tokens do not resolve under data-skin=\"{shipped_skin()}\", so "
        f"every rule using them is dropped:\n  " + "\n  ".join(dangling))


@pytest.mark.parametrize("token", ["--font", "--bg", "--text", "--topo", "--border"])
def test_the_load_bearing_ones_by_name(token):
    """Named individually so the failure says WHICH one, and because a dangling
    --font does not look like a CSS fault to the person reporting it. It looks
    like somebody changed the font."""
    assert token in _resolved(shipped_skin()), \
        f"{token} is undefined in the skin the app ships"


def test_a_palette_block_keeps_its_bare_root_arm():
    """The specific damage. A block written `:root, :root[data-skin="x"]` is
    reached by every skin through the first arm; splitting them silently
    restricts it to one."""
    nc = re.sub(r"/\*.*?\*/", "\n", CSS, flags=re.S)
    for m in re.finditer(r"([^{}]*)\{([^{}]*)\}", nc):
        body = m.group(2)
        if "--font:" not in body:
            continue
        arms = [" ".join(a.split()) for a in m.group(1).split(",")]
        assert ":root" in arms, (
            "the block defining --font is no longer reachable through a bare "
            f":root arm; its selector is {arms}")
        return
    pytest.fail("no block defines --font")
