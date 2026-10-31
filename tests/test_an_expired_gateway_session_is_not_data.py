"""An expired SLOP session must not be parsed as an API response.

The gateway answers an unauthenticated request — XHR included — with
`redir * /login?next={uri} 302` (gateway/Caddyfile), fetch FOLLOWS it, and the
IdP serves its sign-in page as 200 text/html. So "signed out at SLOP" arrives
at the console as a successful 200, not a 401.

The console's fetch wrapper only treated 401 as unauthenticated. A 200 with an
HTML body was JSON.parsed, the parse failed, and the page came back as
`{ detail: "<!doctype html>…" }` — a successful API result. api.me() then looked
like a signed-in user with every field undefined, and api.auth-mode() like a
console with no SSO, so the app fell through to its OWN login form, which under
SSO answers 403 to every credential. SLOP's session TTL is 12 hours, so this is
what a tab left open overnight did.

This is a browser-side contract with no JS test runner in the repo, so it is
pinned as source — the way tests/test_firewall_docs_describe_the_new_tab.py and
the other console-contract tests in here do it.
"""
from pathlib import Path

import pytest

API_JS = Path(__file__).resolve().parents[1] / "webgui" / "frontend" / "src" / "api.js"
DIST = Path(__file__).resolve().parents[1] / "webgui" / "frontend" / "dist"


@pytest.fixture(scope="module")
def src():
    return API_JS.read_text()


def test_a_followed_redirect_is_treated_as_an_expired_session(src):
    assert "res.redirected" in src, (
        "fetch exposes .redirected when it followed the gateway's 302 — that is "
        "the signal that the response did not come from the API at all")


def test_html_where_json_was_expected_is_treated_as_an_expired_session(src):
    assert 'ctype.includes("html")' in src, (
        "the gateway's sign-in page arrives as 200 text/html; every /api/* path "
        "this client calls answers JSON")


def test_it_does_not_rely_on_the_status_code_alone(src):
    """The check has to sit on the SUCCESS path. Guarding it behind res.ok being
    false, or behind a 401, puts it exactly where the bug was."""
    i_401 = src.index('res.status === 401')
    i_guard = src.index("res.redirected")
    i_notok = src.index("if (!res.ok)")
    assert i_401 < i_guard < i_notok, (
        "the redirect/HTML guard must run after the 401 check and BEFORE the "
        "!res.ok branch, i.e. on a 200")


def test_it_bounces_to_the_platform_sign_in_rather_than_the_local_form(src):
    """Firing the unauthorized handler alone is not enough under SSO: the app's
    own login form answers 403 to every credential. Only a top-level navigation
    gets redirected to the real sign-in."""
    # Slice from the guard to the end of its own block, so the assertion does
    # not depend on what happens to follow it in the function.
    start = src.index("res.redirected")
    guard = src[start:start + src[start:].index("\n  }\n")]
    assert "_fireUnauthorized()" in guard, guard
    assert "window.location.reload()" in guard, guard


def test_a_raw_response_is_not_judged_on_its_content_type(src):
    """`raw` callers are downloads and streams — a blob, a tarball, an HTML
    document. Only the JSON path can treat HTML as a sign-in page."""
    assert "!raw && ctype.includes" in src


def test_the_built_console_carries_the_guard():
    if not DIST.is_dir():
        pytest.skip("webgui/frontend/dist not built in this checkout")
    blobs = list(DIST.rglob("*.js"))
    assert blobs, "no built JS found"
    assert any("Your session has expired" in b.read_text(errors="ignore") for b in blobs), \
        "rebuild the console (npm run build) — dist is what ships"
