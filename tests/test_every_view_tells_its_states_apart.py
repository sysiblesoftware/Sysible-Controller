"""A screen must not render one state in the words of another.

From the UI/UX audit. Twenty-one Controller views were checked for the three
states a screen can be in before it has anything to show -- loading, empty,
failed -- and two rendered one of them as another:

  * FleetQuery: a query that matched nothing produced an empty table under a
    heading. "No host matches this" and "the query did not work" looked
    identical, which is the one distinction the screen existed to make.

  * LiveActivity: `activity.length === 0` rendered "No activity recorded yet."
    That is also what it rendered for the four seconds BEFORE the first fetch
    came back, so a log about to fill read as a log that never would.

The second is the more instructive: the empty state was there, correct, and
WRONG, because nothing separated "nothing" from "not yet". A state is only
worth rendering if it cannot be confused with a different one.

Run: pytest tests/
"""
import re
from pathlib import Path

import pytest

VIEWS = Path(__file__).resolve().parents[1] / "webgui" / "frontend" / "src" / "views"


def _src(name: str) -> str:
    return (VIEWS / name).read_text(encoding="utf-8")


# ---- the two that were wrong ----------------------------------------------
def test_a_query_that_matched_nothing_says_so():
    s = _src("FleetQuery.jsx")
    assert "res.rows.length === 0" in s, \
        "FleetQuery no longer distinguishes an empty result from a rendered one"
    assert "none matched" in s, "nothing tells the operator the query DID work"


def test_a_query_with_no_hosts_at_all_says_something_different():
    """Nothing to query and nothing matched are different problems with
    different fixes: enrol a host, or change the query."""
    s = _src("FleetQuery.jsx")
    assert "No managed hosts to query yet" in s
    assert "res.count === 0" in s, "both cases render the same words"


def test_the_activity_log_does_not_call_loading_empty():
    s = _src("LiveActivity.jsx")
    assert "const [loaded, setLoaded]" in s, \
        "LiveActivity has no first-fetch flag, so empty and loading are one state"
    # The loading branch must come BEFORE the empty one, or it never runs.
    loading = s.index("!loaded ?")
    empty = s.index("activity.length === 0 ?")
    assert loading < empty, \
        "the empty check runs first, so 'No activity recorded yet' still wins while loading"


def test_the_flag_is_set_even_when_the_fetch_fails():
    """A failed fetch is an answer. Leaving the flag false would spin for ever
    on the one case where the operator most needs to be told something."""
    s = _src("LiveActivity.jsx")
    assert re.search(r"finally\s*\{\s*setLoaded\(true\)", s), \
        "setLoaded is not in a finally; an error leaves the view loading for ever"


def test_switching_tabs_starts_loading_again():
    """The Controller Log tab fetches separately. Without resetting the flag it
    would render the PREVIOUS tab's emptiness as its own."""
    s = _src("LiveActivity.jsx")
    assert "setLoaded(false);\n    load();" in s, \
        "the tab effect does not reset the flag before fetching"


def test_the_log_tab_was_not_left_behind():
    s = _src("LiveActivity.jsx")
    assert '!loaded ? "Loading' in s, "the log tab still shows (empty) while loading"


# ---- and the rule, for every view ------------------------------------------
# Forms have nothing to be empty OF. Named here rather than pattern-matched so
# that adding a view cannot quietly join the exemption list.
_FORMS = {"Login.jsx", "Setup.jsx", "ForcePasswordChange.jsx"}


def _views():
    return sorted(p.name for p in VIEWS.glob("*.jsx"))


def test_this_test_is_looking_at_something():
    v = _views()
    assert len(v) >= 15, f"only {len(v)} views found; the path has moved: {v}"
    assert _FORMS <= set(v), f"the exempt forms are gone: {_FORMS - set(v)}"


@pytest.mark.parametrize("name", [v for v in _views()])
def test_a_view_that_can_be_empty_says_when_it_is(name):
    if name in _FORMS:
        pytest.skip("a form has nothing to be empty of")
    s = _src(name)
    if not re.search(r"\.map\(", s):
        pytest.skip("renders no list")
    # Three spellings, all real. A view can render the empty state itself, word
    # it in prose, or hand the words to a shared component -- ResultsPane takes
    # `empty=` and renders them in a className="empty" of its own. Matching only
    # the first would fail two views that do this properly, which is a test
    # teaching people to stop sharing the component.
    assert re.search(r'className="empty"|\bempty=|No \w[^"<]{0,40}(yet|found|match)', s), (
        f"{name} renders a list with no empty state: an operator cannot tell "
        f"'nothing here' from 'it did not load'")
