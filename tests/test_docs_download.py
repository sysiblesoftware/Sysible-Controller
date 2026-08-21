"""
Offline documentation download (web console BFF).

The left-rail "Documentation" item links to GET /api/docs/download, which serves
the bundled, self-contained HTML manual so an operator can save it and read it
without network access. Verify it is login-gated, streams the file, and rejects
an unknown selector.
"""
import pytest


def _login_override(srv):
    # require_login normally validates the session cookie against the controller;
    # override it so we can exercise the route without a full login round-trip.
    srv.app.dependency_overrides[srv.require_login] = lambda: "tester"


def test_docs_download_requires_login(bff):
    import webgui.server as srv
    srv.app.dependency_overrides.pop(srv.require_login, None)
    r = bff.get("/api/docs/download")
    # Unauthenticated → not a 200 file stream (401/403/redirect depending on
    # the login guard), never the document.
    assert r.status_code != 200


def test_docs_download_streams_full_manual(bff):
    import webgui.server as srv
    _login_override(srv)
    try:
        r = bff.get("/api/docs/download")
        assert r.status_code == 200, r.text
        assert "text/html" in r.headers.get("content-type", "")
        assert "attachment" in r.headers.get("content-disposition", "").lower()
        assert "Sysible_Controller_Documentation.html" in r.headers.get("content-disposition", "")
        assert len(r.content) > 1000        # the real manual, not an empty stub
    finally:
        srv.app.dependency_overrides.clear()


def test_docs_download_quickstart_selector(bff):
    import webgui.server as srv
    _login_override(srv)
    try:
        r = bff.get("/api/docs/download", params={"which": "quickstart"})
        assert r.status_code == 200
        assert "Quickstart" in r.headers.get("content-disposition", "")
    finally:
        srv.app.dependency_overrides.clear()


def test_docs_download_rejects_unknown_selector(bff):
    import webgui.server as srv
    _login_override(srv)
    try:
        r = bff.get("/api/docs/download", params={"which": "../../etc/passwd"})
        assert r.status_code == 404
    finally:
        srv.app.dependency_overrides.clear()


def test_docs_view_renders_inline_as_webpage(bff):
    """The rail 'Documentation' item opens /api/docs/view, which serves the manual
    with an INLINE disposition so the browser renders it as a webpage (not a save)."""
    import webgui.server as srv
    _login_override(srv)
    try:
        r = bff.get("/api/docs/view")
        assert r.status_code == 200, r.text
        assert "text/html" in r.headers.get("content-type", "")
        cd = r.headers.get("content-disposition", "").lower()
        assert "inline" in cd and "attachment" not in cd
        assert len(r.content) > 1000
    finally:
        srv.app.dependency_overrides.clear()


def test_docs_view_requires_login(bff):
    import webgui.server as srv
    srv.app.dependency_overrides.pop(srv.require_login, None)
    assert bff.get("/api/docs/view").status_code != 200
