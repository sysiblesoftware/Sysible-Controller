"""CSRF Origin/Referer backstop + Secure-cookie default on the BFF web console.

SameSite=Strict is the primary CSRF control (the session cookie is never attached to a
cross-site request). `csrf_origin_guard` is a defense-in-depth backstop: a mutating /api
request whose Origin (or Referer) host isn't ours is refused before the route runs, and
the session cookie defaults to Secure so it can't ride a plain-HTTP request.
"""
import os
import tempfile

os.environ.setdefault("SYSIBLE_API_KEY", "test-csrf-key")
os.environ.setdefault("SYSIBLE_DATA_DIR", tempfile.mkdtemp(prefix="sysible-csrf-"))

from fastapi.testclient import TestClient  # noqa: E402
import webgui.server as w  # noqa: E402

client = TestClient(w.app)
_MUTATING = "/api/controller-restart"  # a superuser POST route; the guard runs before it


def test_cross_origin_mutating_request_rejected():
    r = client.post(_MUTATING, headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and "Cross-origin" in r.text


def test_cross_origin_via_referer_rejected():
    r = client.post(_MUTATING, headers={"referer": "https://evil.example/attack"})
    assert r.status_code == 403 and "Cross-origin" in r.text


def test_same_origin_mutating_passes_guard():
    # Passes the CSRF guard; auth then rejects (401), but NOT with the cross-origin 403.
    r = client.post(_MUTATING, headers={"origin": "http://testserver", "host": "testserver"})
    assert "Cross-origin" not in r.text


def test_no_origin_header_passes_guard():
    # Non-browser / loopback tooling sends no Origin; SameSite=Strict remains the control.
    r = client.post(_MUTATING)
    assert "Cross-origin" not in r.text


def test_get_is_not_csrf_checked():
    r = client.get("/api/whoami", headers={"origin": "https://evil.example"})
    assert "Cross-origin" not in r.text


def test_session_cookie_secure_default_on():
    # Enterprise-safe default: Secure is ON unless SYSIBLE_WEBGUI_ALLOW_INSECURE_COOKIE=1.
    assert w._HTTPS_ONLY is True
