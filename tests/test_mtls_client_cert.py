"""Optional mutual-TLS client certificate on the API client (client/api.py).

The controller can be started with --mtls to require client certs; the BFF/CLI
present one when SYSIBLE_CLIENT_CERT (+ optional SYSIBLE_CLIENT_KEY) is set. With
neither var set the session must be unchanged (no cert), so a non-mTLS controller
is unaffected.
"""
import importlib

import pytest


def _reload_api(monkeypatch, cert=None, key=None):
    for var in ("SYSIBLE_CLIENT_CERT", "SYSIBLE_CLIENT_KEY"):
        monkeypatch.delenv(var, raising=False)
    if cert is not None:
        monkeypatch.setenv("SYSIBLE_CLIENT_CERT", str(cert))
    if key is not None:
        monkeypatch.setenv("SYSIBLE_CLIENT_KEY", str(key))
    import client.api as api
    return importlib.reload(api)


def test_no_client_cert_by_default(monkeypatch):
    api = _reload_api(monkeypatch)
    assert api._SESSION.cert is None


def test_cert_and_key_pair(monkeypatch, tmp_path):
    cert = tmp_path / "client.crt"; cert.write_text("x")
    key = tmp_path / "client.key"; key.write_text("y")
    api = _reload_api(monkeypatch, cert=cert, key=key)
    assert api._SESSION.cert == (str(cert), str(key))


def test_cert_only_when_key_absent(monkeypatch, tmp_path):
    cert = tmp_path / "bundle.pem"; cert.write_text("x")
    api = _reload_api(monkeypatch, cert=cert)
    assert api._SESSION.cert == str(cert)


def test_missing_cert_file_is_ignored(monkeypatch, tmp_path):
    api = _reload_api(monkeypatch, cert=tmp_path / "nope.crt")
    assert api._SESSION.cert is None


@pytest.fixture(autouse=True)
def _restore_api(monkeypatch):
    # Leave client.api reloaded in its default (no-cert) state for other tests.
    yield
    for var in ("SYSIBLE_CLIENT_CERT", "SYSIBLE_CLIENT_KEY"):
        monkeypatch.delenv(var, raising=False)
    import client.api as api
    importlib.reload(api)
