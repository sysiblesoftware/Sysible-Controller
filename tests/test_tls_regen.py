"""
Self-signed TLS regeneration on controller-address change.

When the controller's hostname/IP changes, the self-signed cert's SAN goes stale
and agents that verify it hit a hostname mismatch. These tests lock in that the
cert is regenerated with the new SAN — and that an imported PKI cert is never
clobbered.
"""
import threading

import pytest
from cryptography import x509

from backend import tls_manager
import backend.db as db
from conftest import key_headers


@pytest.fixture
def tmp_certs(tmp_path, monkeypatch):
    cert = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    trust = tmp_path / "trust.crt"
    monkeypatch.setattr(tls_manager, "CERT_DIR", tmp_path)
    monkeypatch.setattr(tls_manager, "CERT_FILE", cert)
    monkeypatch.setattr(tls_manager, "KEY_FILE", key)
    monkeypatch.setattr(tls_manager, "TRUST_FILE", trust)
    return cert, key, trust


def _san(cert_path):
    c = x509.load_pem_x509_certificate(cert_path.read_bytes())
    ext = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    dns = list(ext.get_values_for_type(x509.DNSName))
    ips = [str(i) for i in ext.get_values_for_type(x509.IPAddress)]
    return dns, ips


# ---------------------------------------------------------------------------
# The primitive
# ---------------------------------------------------------------------------
def test_regenerate_covers_hostname_and_ip(tmp_certs):
    cert, key, trust = tmp_certs
    tls_manager.regenerate_self_signed(hostnames=["ctrl.corp.example", "10.5.5.5"])
    dns, ips = _san(cert)
    assert "ctrl.corp.example" in dns
    assert "10.5.5.5" in ips
    # localhost/loopback always retained so local health checks keep working.
    assert "localhost" in dns and "127.0.0.1" in ips
    # trust bundle == the self-signed leaf; key is 0600.
    assert trust.read_bytes() == cert.read_bytes()
    assert oct(key.stat().st_mode & 0o777) == "0o600"
    assert tls_manager.current_is_self_signed() is True


def test_regenerate_classifies_numeric_as_ip_not_dns(tmp_certs):
    cert, _, _ = tmp_certs
    tls_manager.regenerate_self_signed(hostnames=["192.168.1.50"])
    dns, ips = _san(cert)
    assert "192.168.1.50" in ips and "192.168.1.50" not in dns


# ---------------------------------------------------------------------------
# Config-change endpoint triggers regeneration
# ---------------------------------------------------------------------------
def test_config_change_flags_cert_stale_without_touching_live_cert(controller, superuser_headers, tmp_certs):
    cert, _, _ = tmp_certs
    # Seed a starting self-signed cert for a known address.
    tls_manager.regenerate_self_signed(hostnames=["old.example"])
    r = controller.post("/controller-config", headers=superuser_headers, json={
        "hostname": "new.example", "ip": "", "address_mode": "hostname", "port": 9000,
    })
    assert r.status_code == 200
    body = r.json()
    # An address change must NOT rewrite the LIVE cert: uvicorn keeps serving the
    # old cert until a restart, so overwriting the served/pinned file now would
    # leave every TLS client (the console over loopback, agents pinning
    # server.crt) verifying the still-served old cert against the new one and
    # failing the handshake (the HTTPSConnectionPool bug). It just flags staleness;
    # the restart-bearing regenerate action applies the new cert.
    assert body.get("cert_regenerated") is not True
    assert body.get("cert_stale") is True and body.get("cert_note")
    dns, _ = _san(cert)
    assert "old.example" in dns and "new.example" not in dns   # live cert untouched


def test_unchanged_address_does_not_regenerate(controller, superuser_headers, tmp_certs):
    tls_manager.regenerate_self_signed(hostnames=["same.example"])
    db.set_controller_config("same.example", "", "hostname", 9000)
    r = controller.post("/controller-config", headers=superuser_headers, json={
        "hostname": "same.example", "ip": "", "address_mode": "hostname", "port": 9000,
    })
    assert r.status_code == 200
    assert "cert_regenerated" not in r.json()   # address didn't change


def test_regenerate_endpoint_on_demand(controller, superuser_headers, tmp_certs, monkeypatch):
    # The on-demand button: regenerate for the CURRENT address even though the
    # config was already saved at that value (the auto-on-change path wouldn't fire).
    monkeypatch.setattr(tls_manager, "restart_backend", lambda *a, **k: None)
    cert, _, _ = tmp_certs
    tls_manager.regenerate_self_signed(hostnames=["stale.example"])
    db.set_controller_config("ondemand.example", "", "hostname", 9000)
    r = controller.post("/controller-config/tls/regenerate-self-signed", headers=superuser_headers)
    assert r.status_code == 200 and r.json().get("restarting") is True
    dns, _ = _san(cert)
    assert "ondemand.example" in dns and "stale.example" not in dns


def test_regenerate_endpoint_refuses_pki(controller, superuser_headers, tmp_certs, monkeypatch):
    monkeypatch.setattr(tls_manager, "restart_backend", lambda *a, **k: None)
    monkeypatch.setattr(tls_manager, "current_is_self_signed", lambda: False)
    db.set_controller_config("x.example", "", "hostname", 9000)
    r = controller.post("/controller-config/tls/regenerate-self-signed", headers=superuser_headers)
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Restart must survive the backend's own cgroup teardown
# ---------------------------------------------------------------------------
def test_restart_backend_uses_detached_timer(monkeypatch):
    # A cert regen is useless if the restart that picks it up runs inside
    # sysible-backend's own cgroup — systemd kills the restarter along with
    # the service and it never comes back. Lock in that we schedule the
    # restart through a detached systemd-run timer instead.
    calls = []
    done = threading.Event()

    def fake_call(args, **kw):
        calls.append(list(args))
        done.set()
        return 0

    def fake_popen(*a, **k):
        calls.append(("POPEN", a, k))
        done.set()

    monkeypatch.setattr(tls_manager.subprocess, "call", fake_call)
    monkeypatch.setattr(tls_manager.subprocess, "Popen", fake_popen)

    tls_manager.restart_backend(delay_seconds=0)
    assert done.wait(timeout=5), "restart thread never fired"
    assert calls[0][0] == "systemd-run"
    assert "systemctl" in calls[0] and "restart" in calls[0]
    assert calls[0][-1] == tls_manager.SERVICE_NAME
    # systemd-run succeeded, so the in-cgroup Popen fallback must NOT run.
    assert not any(c[0] == "POPEN" for c in calls)


def test_restart_backend_falls_back_without_systemd_run(monkeypatch):
    # Hosts without systemd-run still get restarted, detached into a new
    # session so the client isn't a direct child of the dying uvicorn.
    calls = []
    done = threading.Event()

    def fake_call(args, **kw):
        raise FileNotFoundError("systemd-run missing")

    def fake_popen(args, **kw):
        calls.append((list(args), kw))
        done.set()

    monkeypatch.setattr(tls_manager.subprocess, "call", fake_call)
    monkeypatch.setattr(tls_manager.subprocess, "Popen", fake_popen)

    tls_manager.restart_backend(delay_seconds=0)
    assert done.wait(timeout=5), "fallback restart never fired"
    args, kw = calls[0]
    assert args == ["systemctl", "restart", tls_manager.SERVICE_NAME]
    assert kw.get("start_new_session") is True


def test_pki_cert_is_not_clobbered(controller, superuser_headers, tmp_certs, monkeypatch):
    # If a custom (non-self-signed) cert is installed, an address change must NOT
    # regenerate over it — only surface a note.
    monkeypatch.setattr(tls_manager, "current_is_self_signed", lambda: False)
    db.set_controller_config("a.example", "", "hostname", 9000)
    r = controller.post("/controller-config", headers=superuser_headers, json={
        "hostname": "b.example", "ip": "", "address_mode": "hostname", "port": 9000,
    })
    assert r.status_code == 200
    body = r.json()
    assert body.get("cert_regenerated") is not True
    assert "PKI" in (body.get("cert_note") or "")
