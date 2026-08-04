"""Regression tests for the request-body size cap and the enroll flood guard."""
import asyncio

import backend.app as A


def test_enroll_rate_limit(monkeypatch):
    monkeypatch.setenv("SYSIBLE_ENROLL_RATE_MAX", "3")
    monkeypatch.setenv("SYSIBLE_ENROLL_RATE_WINDOW", "60")
    A._ENROLL_RATE.clear()
    ip = "203.0.113.7"
    assert A._enroll_rate_limited(ip) == 0
    assert A._enroll_rate_limited(ip) == 0
    assert A._enroll_rate_limited(ip) == 0
    assert A._enroll_rate_limited(ip) > 0            # 4th request over the cap
    assert A._enroll_rate_limited("198.51.100.9") == 0   # a different source IP is unaffected
    assert A._enroll_rate_limited("") == 0               # no IP → never limited


def test_enroll_rate_limit_disabled(monkeypatch):
    monkeypatch.setenv("SYSIBLE_ENROLL_RATE_MAX", "0")
    A._ENROLL_RATE.clear()
    for _ in range(50):
        assert A._enroll_rate_limited("203.0.113.7") == 0


def _drive(mw, scope, body_chunks):
    sent = []

    async def receive():
        if body_chunks:
            chunk = body_chunks.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(body_chunks)}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(mw(scope, receive, send))
    return sent


def test_body_limit_rejects_large_content_length():
    called = {"v": False}

    async def inner(scope, receive, send):
        called["v"] = True

    mw = A._BodyLimitMiddleware(inner, max_bytes=10)
    scope = {"type": "http", "headers": [(b"content-length", b"1000")]}
    sent = _drive(mw, scope, [])
    assert sent and sent[0]["type"] == "http.response.start" and sent[0]["status"] == 413
    assert called["v"] is False          # short-circuited before the app ran


def test_body_limit_allows_small_body():
    called = {"v": False}

    async def inner(scope, receive, send):
        called["v"] = True

    mw = A._BodyLimitMiddleware(inner, max_bytes=1000)
    scope = {"type": "http", "headers": [(b"content-length", b"5")]}
    _drive(mw, scope, [b"hello"])
    assert called["v"] is True


def test_body_limit_disabled_passes_through():
    called = {"v": False}

    async def inner(scope, receive, send):
        called["v"] = True

    mw = A._BodyLimitMiddleware(inner, max_bytes=0)   # 0 → disabled
    scope = {"type": "http", "headers": [(b"content-length", b"999999999")]}
    _drive(mw, scope, [])
    assert called["v"] is True
