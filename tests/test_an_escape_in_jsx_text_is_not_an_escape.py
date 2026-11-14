"""`\\u2026` between JSX tags is seven characters, not an ellipsis.

This has now cost twice in one codebase, in two languages, for the same
reason: a backslash escape written where the surrounding syntax does not
process escapes.

  * a triple-quoted `_CSS` block in Python is NOT a raw string, so `\\25B8` is OCTAL
    escape -- it became \\x15B8, and the console rendered a control character
    where a triangle was meant.

  * JSX TEXT is literal. `<span>Loading\\u2026</span>` renders the backslash,
    the u and the four digits, to the operator. Inside a JS string --
    `{"Loading\\u2026"}` -- the same sequence is a real escape and works, which
    is what makes the broken form look right.

Neither fails a build, neither fails a type check, and both look correct in a
diff. So: no `\\uXXXX` in JSX text. Write the character.

Run: pytest tests/
"""
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "webgui" / "frontend" / "src"
FILES = sorted(SRC.rglob("*.jsx"))

# Text between > and <, i.e. what the browser renders. Attribute values and
# anything inside {braces} are JavaScript, where an escape is an escape.
_TEXT = re.compile(r">([^<>{}]+)<")


def test_this_test_is_looking_at_something():
    assert len(FILES) >= 10, f"only {len(FILES)} jsx files found under {SRC}"
    assert any(_TEXT.search(f.read_text(encoding="utf-8")) for f in FILES), \
        "the text-node pattern matched nothing anywhere; it has stopped working"


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_unicode_escape_sits_in_rendered_text(path):
    bad = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for chunk in _TEXT.findall(line):
            if re.search(r"\\u[0-9a-fA-F]{4}", chunk):
                bad.append(f"line {lineno}: {chunk.strip()[:70]}")
    assert not bad, (
        f"{path.name} writes a \\uXXXX escape in JSX text, which renders the "
        f"backslash to the operator \u2014 write the character itself:\n  "
        + "\n  ".join(bad))
