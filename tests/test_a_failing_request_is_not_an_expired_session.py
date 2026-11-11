"""An upstream that did not answer is not a session that expired.

Reported twice, as two bugs, which were one:

    "When I click enroll a host it logs me out of controller"
    "Why is there a controller login page that just says sign in on SLOP?
     It should redirect to the SLOP login if needed."

The chain. Enrollment opens an SSH connection to another machine, installs a
key and enrolls an agent; the BFF gave the hop behind it 30 seconds, which that
work routinely exceeds, so it returned 502. SLOP's gateway turns a 502 from an
upstream into a STYLED HTML PAGE -- `handle_errors` in its Caddyfile, a card
reading "that module is not answering". And api.js treated any HTML answer as
proof the session had expired, because the gateway's SIGN-IN page is also HTML.

So: fire the unauthorized handler, reload the page. The operator is signed out
in the middle of an enrollment that was working, and the reload lands on the
console's own sign-in form -- which under SSO can only ever answer 403. Two
reports, one `if`.

The signal that separates them is the STATUS. The gateway's sign-in page is
200; its error page is 502/503/504.

Run: pytest tests/
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
API = (REPO / "webgui" / "frontend" / "src" / "api.js").read_text(encoding="utf-8")
APP = (REPO / "webgui" / "frontend" / "src" / "App.jsx").read_text(encoding="utf-8")
SERVER = (REPO / "webgui" / "server.py").read_text(encoding="utf-8")
CADDY = Path("/home/user/slop/gateway/Caddyfile")


def _bounce_condition():
    """The `if` that decides a response means the session is gone."""
    # To end of line, not [^)]* -- the condition contains ctype.includes("html"),
    # whose own ")" ended the match early and made this read as "the branch is
    # gone" no matter what the branch said.
    m = re.search(r"^\s*if \(res\.redirected \|\|.*$", API, re.M)
    assert m, "the session-expiry branch is gone or no longer in one expression"
    return m.group(0)


def test_only_a_successful_html_response_reads_as_a_sign_in_page():
    """res.ok is the whole fix. Without it, every 502 behind the gateway signs
    the operator out."""
    cond = _bounce_condition()
    assert "res.ok" in cond, (
        "an HTML response is treated as an expired session regardless of "
        f"status, so the gateway's own error page logs the operator out: {cond}")


def test_a_failing_html_response_is_turned_into_a_sentence():
    """JSON.parse fails on an HTML page and the whole document lands in
    `detail`, so the console renders a page of markup where a sentence
    belongs."""
    assert re.search(r"!res\.ok && ctype\.includes\(\"html\"\)", API), \
        "a non-ok HTML body is still handed to the caller as its error text"
    assert "gatewayError" in API, \
        "nothing marks an error that came from the gateway rather than the app"


def test_the_bff_does_not_give_up_before_the_browser_does():
    """The 502 that started all this. The browser allows 120s for an
    enrollment; the hop behind it allowed 30."""
    m = re.search(r"/remote/enroll-ssh\", request,.*?\)", SERVER, re.S)
    assert m, "the enroll proxy call is no longer in the expected shape"
    t = re.search(r"timeout=(\d+)", m.group(0))
    assert t and int(t.group(1)) >= 120, (
        f"the BFF gives up on an enrollment after {t.group(1) if t else 30}s; "
        f"the browser waits 120s, so this turns a working enrollment into a 502")


@pytest.mark.skipif(not CADDY.is_file(), reason="the SLOP checkout is not beside this one")
def test_the_gateway_really_does_answer_with_html():
    """The premise. If the gateway stopped doing this the fix above would be
    guarding against nothing, and this test would be the only thing to say so."""
    text = CADDY.read_text(encoding="utf-8")
    assert "handle_errors" in text
    m = re.search(r"handle_errors \{(.*?)\n\t\}", text, re.S)
    assert m, "handle_errors is no longer in the expected shape"
    body = m.group(1)
    assert "text/html" in body, "the gateway's error page is no longer HTML"
    assert re.search(r"\[502, 503, 504\]", body), \
        "the statuses the gateway renders as HTML have changed"


# ---- and no console sign-in page under SSO --------------------------------
def test_sso_sends_the_browser_to_the_platform_sign_in():
    """Asked for directly: "it should redirect to the SLOP login if needed. It
    should all be the same." SLOP owns the accounts, so there is nothing for
    this console to ask."""
    assert "ssoBounceOnce" in APP, "nothing navigates to the platform sign-in"
    m = re.search(r"function ssoBounceOnce\(\) \{(.*?)\n\}", APP, re.S)
    assert m, "ssoBounceOnce is not in the expected shape"
    assert "window.location.assign" in m.group(1), \
        "it does not actually navigate; a button on a card is not a redirect"
    assert "platformLoginUrl()" in m.group(1)


def test_the_redirect_carries_where_the_operator_was_going():
    m = re.search(r"function platformLoginUrl\(\) \{(.*?)\n\}", APP, re.S)
    assert m, "platformLoginUrl is gone"
    assert "next=" in m.group(1), \
        "the platform sign-in is not told where to return to"
    assert "encodeURIComponent" in m.group(1), \
        "the return path is not escaped, so a query string truncates it"


def test_it_bounces_once_and_then_explains_itself():
    """A console that cannot establish a session for an account that IS signed
    in to SLOP would bounce between the two forever, with the reason never on
    screen long enough to read."""
    m = re.search(r"function ssoBounceOnce\(\) \{(.*?)\n\}", APP, re.S)
    body = m.group(1)
    assert "sessionStorage" in body, "nothing remembers that we already bounced"
    assert "return false" in body, \
        "there is no path that declines to bounce, so the loop has no end"
    assert "setSsoBlocked(true)" in APP, "there is no card to land on"


def test_a_successful_sign_in_forgets_the_bounce():
    """Otherwise the free trip to the sign-in is spent once per browser tab,
    and every later expiry lands on the card instead."""
    assert re.search(r"sessionStorage\.removeItem\(_SSO_BOUNCED\)", APP), \
        "the bounce flag is never cleared, so it only ever works once"


def test_the_local_login_form_is_not_the_fallback_for_an_unknown_mode():
    """If /api/auth-mode cannot even be ASKED, falling through to the local form
    is right for a standalone controller and a dead end behind the gateway. A
    redirected or HTML answer means a gateway was in the way."""
    # The CATCH, specifically. Matching from `const m = await api.authMode()`
    # to the first `}` lands on the end of the `if (m && m.sso)` block instead,
    # which says nothing about what happens when the call itself fails.
    m = re.search(r"\} catch \(e\) \{(.*?)\n        \}", APP, re.S)
    assert m, "the auth-mode failure branch is no longer in the expected shape"
    body = m.group(1)
    assert "gatewayError" in body or "e.status === 401" in body, \
        "a failed auth-mode check still falls straight through to the local form"
    assert "ssoBounceOnce" in body, \
        "a gateway-shaped failure does not reach the platform sign-in"


# ---- and the same thing, in a browser -------------------------------------
# The checks above read the source. These drive the REAL req() against a server
# that answers the two ways SLOP's gateway answers, because "res.ok appears in
# the condition" and "a 502 no longer signs you out" are not the same claim.
import functools
import http.server
import json
import shutil
import socketserver
import subprocess
import threading

FE = REPO / "webgui" / "frontend"
ESBUILD = FE / "node_modules" / ".bin" / "esbuild"

_CHROMIUM = next((c for c in ("/opt/pw-browsers/chromium", "/usr/bin/chromium",
                              "/usr/bin/chromium-browser", "/usr/bin/google-chrome")
                  if Path(c).is_file()), None)


def _have_playwright():
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False


_GATEWAY_ERROR = (b"<!doctype html><html><body><div class=card>"
                  b"<h1>That module is not answering</h1></div></body></html>")
_SIGNIN_PAGE = (b"<!doctype html><html><body><form>Sign in to SLOP</form>"
                b"</body></html>")

_MAIN_JS = """
import { api, setUnauthorizedHandler } from "./api.js";
window.__fired = 0;
setUnauthorizedHandler(() => { window.__fired += 1; });
window.__probe = async (kind) => {
  const before = window.__fired;
  try {
    const d = await api.me(kind);
    return { ok: true, fired: window.__fired - before };
  } catch (e) {
    return { ok: false, message: e.message, status: e.status,
             gatewayError: !!e.gatewayError, fired: window.__fired - before };
  }
};
"""


@pytest.fixture(scope="module")
def probe():
    """Drive api.js in Chromium against gateway-shaped answers."""
    if not ESBUILD.is_file():
        pytest.skip("esbuild is not installed")
    if not _have_playwright() or not _CHROMIUM:
        pytest.skip("no chromium to drive")
    import tempfile
    from playwright.sync_api import sync_playwright

    d = Path(tempfile.mkdtemp())
    (d / "src").mkdir()
    src = (FE / "src" / "api.js").read_text(encoding="utf-8")
    # me() takes no argument in the real client; the probe needs to choose which
    # stub endpoint to hit. Patched in the COPY, so the shipped client is what
    # every other assertion in this file reads.
    src = src.replace('  me: () => req("/api/me"),', '  me: (k) => req("/api/" + (k || "me")),')
    assert "me: (k) =>" in src, "api.me is no longer in the expected shape"
    (d / "src" / "api.js").write_text(src, encoding="utf-8")
    (d / "src" / "main.js").write_text(_MAIN_JS, encoding="utf-8")
    (d / "node_modules").symlink_to(FE / "node_modules")
    (d / "index.html").write_text(
        '<!doctype html><html><head><meta charset="utf-8"><title>t</title></head>'
        '<body><script src="./app.js"></script></body></html>', encoding="utf-8")
    r = subprocess.run([str(ESBUILD), str(d / "src" / "main.js"), "--bundle",
                        "--format=iife", f"--outfile={d / 'app.js'}",
                        "--log-level=error"],
                       capture_output=True, text=True, cwd=str(FE))
    assert r.returncode == 0, r.stderr

    class H(http.server.SimpleHTTPRequestHandler):
        def _send(self, status, ctype, body):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/api/gateway-error":
                return self._send(502, "text/html; charset=utf-8", _GATEWAY_ERROR)
            if self.path == "/api/signin-page":
                return self._send(200, "text/html; charset=utf-8", _SIGNIN_PAGE)
            if self.path == "/api/good":
                return self._send(200, "application/json",
                                  json.dumps({"username": "admin"}).encode())
            return super().do_GET()

        def log_message(self, *a):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(H, directory=str(d)))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    out = {}
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(executable_path=_CHROMIUM)
            pg = b.new_page()
            pg.goto(f"http://127.0.0.1:{port}/index.html", wait_until="load")
            pg.wait_for_timeout(300)
            # The sign-in case LAST: it is supposed to reload the page, which
            # tears down the context the probe runs in.
            for kind in ("good", "gateway-error", "signin-page"):
                try:
                    out[kind] = pg.evaluate("(k) => window.__probe(k)", kind)
                except Exception as e:                       # noqa: BLE001
                    out[kind] = {"navigated": True, "error": str(e)}
            b.close()
    finally:
        srv.shutdown()
        shutil.rmtree(d, ignore_errors=True)
    return out


def test_a_working_request_still_works(probe):
    assert probe["good"]["ok"] is True
    assert probe["good"]["fired"] == 0


def test_the_gateways_error_page_does_not_sign_the_operator_out(probe):
    """THE BUG. A 502 rendered as HTML by the gateway used to fire the
    unauthorized handler and reload the page, mid-enrollment."""
    r = probe["gateway-error"]
    assert r.get("fired") == 0, (
        "a 502 from the gateway still signs the operator out: "
        f"{r}")
    assert r["status"] == 502, r
    assert r["gatewayError"] is True


def test_the_gateways_error_page_is_reported_as_a_sentence(probe):
    """Not as a page of markup in the error banner."""
    msg = probe["gateway-error"]["message"]
    assert "<" not in msg, f"raw HTML reached the console's error banner: {msg[:120]!r}"
    assert "502" in msg


def test_a_real_expired_session_is_still_caught(probe):
    """The other half. The gateway answers an expired session with its sign-in
    page as 200 text/html, and that must still bounce."""
    r = probe["signin-page"]
    if r.get("navigated"):
        pytest.skip("the forced reload tore the context down, which is the "
                    "behaviour this asserts")
    assert r["fired"] == 1, f"an expired session no longer bounces: {r}"
    assert r["status"] == 401
