"""The console served under a reverse-proxy PATH PREFIX (SLOP's /controller/).

Reported: "Web portal on sysible controller doesn't seem to be working in SLOP."
The console came up BLANK behind the gateway. Nothing in the app was broken — it
was the addressing. SLOP mounts each app under a prefix and strips it before
proxying, so the app keeps serving its own root paths; but the BROWSER still has
to ask for /controller/assets/... and /controller/api/.... The console was built
for a root origin and emitted absolute "/assets/..." and "/api/...", which at the
gateway resolve to the SLOP PORTAL, not here. Script, stylesheet and every API
call 404'd, so nothing rendered.

Two halves, both covered here:
  * the front end asks relatively (vite base "./") and works its prefix out at
    RUN time, so one build is right at either address — it used to come from
    SYSIBLE_BASE_PATH at build time, which nothing ever set;
  * the BFF turns the gateway's X-Forwarded-Prefix into a <base href>, which
    pins that resolution even on a path that is not the mount root.

X-Forwarded-Prefix decides where the browser fetches the console's own SCRIPT
from, and it is client-settable whenever this app is reachable directly — so its
validation is a security boundary, not tidiness, and most of these tests are
about what it REFUSES.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FRONTEND = REPO / "webgui" / "frontend"


def _head(html: str) -> str:
    return html.split("</head>", 1)[0]


# ---- the front end must not hard-code the origin root ----------------------
def test_the_build_defaults_to_a_relative_asset_base():
    """An absolute /assets/... is the whole bug: at the gateway it asks the
    portal for this console's script. The DEFAULT has to be relative, because
    the default is what every install script and the Dockerfile actually use —
    SYSIBLE_BASE_PATH was there all along and nothing ever set it."""
    cfg = (FRONTEND / "vite.config.js").read_text()
    m = re.search(r'process\.env\.SYSIBLE_BASE_PATH\s*\|\|\s*"([^"]+)"', cfg)
    assert m, "vite.config.js no longer derives its base from SYSIBLE_BASE_PATH"
    assert m.group(1) == "./", (
        f"the base defaults to {m.group(1)!r}; an unset SYSIBLE_BASE_PATH — which is "
        f"every shipped build — must produce relative asset URLs")


def test_the_built_bundle_does_not_hard_code_the_origin_root():
    """The artifact itself, not the recipe. This is the file the browser reads."""
    built = FRONTEND / "dist" / "index.html"
    if not built.exists():
        pytest.skip("frontend/dist not built in this checkout")
    html = built.read_text()
    for m in re.finditer(r'(?:href|src)="(/[^/][^"]*)"', html):
        pytest.fail(f"built index.html points at {m.group(1)} — behind the gateway "
                    f"that asks the SLOP portal for this console's own asset")


def test_the_html_shell_has_no_root_absolute_urls():
    html = (FRONTEND / "index.html").read_text()
    for m in re.finditer(r'(?:href|src)="(/[^/][^"]*)"', html):
        pytest.fail(f"index.html points at {m.group(1)}, which resolves to the "
                    f"proxy's root rather than this app")


def test_every_browser_url_goes_through_the_prefix_helper():
    """api.js is the one place URLs are built. A raw fetch(path) there sends the
    request to the origin root and skips the prefix entirely."""
    api = (FRONTEND / "src" / "api.js").read_text()
    assert "export function apiUrl" in api, "the prefix helper is gone"
    assert "fetch(apiUrl(path), opts)" in api, \
        "req() no longer routes its URL through appUrl — every JSON call would skip the prefix"
    assert "document.baseURI" in api, \
        "the prefix is back to a BUILD-time value — one build cannot be right both " \
        "at the controller's own root and under the gateway's /controller/"
    # Any remaining builder that returns a bare "/api/..." string is handed
    # straight to <a href>, window.open() or fetch() and bypasses req().
    for m in re.finditer(r'^\s*\w+Url:\s*\([^)]*\)\s*=>\s*[`"](/api/[^`"]*)', api, re.M):
        pytest.fail(f"URL builder returns bare {m.group(1)} — it must go through apiUrl()")


# ---- the BFF pins it with <base href> --------------------------------------
def test_no_prefix_header_serves_the_page_unchanged(bff):
    """Direct access is the normal case and must not grow a <base>."""
    r = bff.get("/")
    assert r.status_code == 200
    if "frontend not built" in r.text:
        pytest.skip("frontend/dist not built in this checkout")
    assert "<base " not in r.text


def test_the_gateways_prefix_becomes_a_base_href(bff):
    r = bff.get("/", headers={"X-Forwarded-Prefix": "/controller"})
    if "frontend not built" in r.text:
        pytest.skip("frontend/dist not built in this checkout")
    assert r.status_code == 200
    assert '<base href="/controller/">' in _head(r.text), \
        "without this, a relative ./assets/... resolves against the request path, " \
        "not the mount point"
    # It must land before the first relative URL, or it governs nothing.
    head = _head(r.text)
    assert head.index("<base ") < head.index("./assets/"), \
        "<base> must precede the asset tags it is supposed to resolve"


def test_a_trailing_slash_does_not_double_up(bff):
    r = bff.get("/", headers={"X-Forwarded-Prefix": "/controller/"})
    if "frontend not built" in r.text:
        pytest.skip("frontend/dist not built in this checkout")
    assert '<base href="/controller/">' in r.text
    assert "//" not in r.text.split('<base href="', 1)[1].split('"', 1)[0]


@pytest.mark.parametrize("hostile", [
    "//evil.example",                 # protocol-relative: the browser reads a HOST
    "https://evil.example",           # absolute URL
    "http://evil.example/controller",
    "javascript:alert(1)",
    "/a/../../etc",                   # traversal
    "/x\\evil.example",               # backslash
    '/x"><script>alert(1)</script>',  # break out of the attribute
    "/x onload=alert(1)",             # inject an attribute
    "/",                              # the root is not a mount point
    "",
])
def test_a_hostile_prefix_is_refused(bff, hostile):
    """This header chooses the origin the browser loads the console's SCRIPT
    from. Anything but a plain absolute path is ignored outright — the page then
    behaves exactly as it does at a root origin."""
    r = bff.get("/", headers={"X-Forwarded-Prefix": hostile})
    if "frontend not built" in r.text:
        pytest.skip("frontend/dist not built in this checkout")
    assert r.status_code == 200
    assert "<base " not in r.text, f"{hostile!r} was accepted as a mount prefix"
    assert "evil.example" not in r.text
    assert "alert(1)" not in r.text
