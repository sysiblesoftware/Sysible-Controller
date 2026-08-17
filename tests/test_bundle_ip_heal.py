"""Agent bundles self-heal a stale DHCP controller IP (bundle_addresses),
without touching the shared resolver the TLS cert-change logic relies on."""
from backend import agent_bundle


def test_heals_stale_private_ip(monkeypatch):
    # Saved private IP is no longer a live NIC (DHCP lease changed) -> use current NIC.
    monkeypatch.setattr(agent_bundle, "detect_local_ips", lambda: ["192.0.2.50"])
    assert agent_bundle.bundle_addresses({"address_mode": "ip", "ip": "192.168.1.10"}) == ["192.0.2.50"]


def test_keeps_valid_saved_ip(monkeypatch):
    # Saved IP is still one of the box's NICs -> keep it verbatim.
    monkeypatch.setattr(agent_bundle, "detect_local_ips", lambda: ["192.168.1.10", "192.0.2.50"])
    assert agent_bundle.bundle_addresses({"address_mode": "ip", "ip": "192.168.1.10"}) == ["192.168.1.10"]


def test_preserves_public_nat_ip(monkeypatch):
    # A public/NAT IP is never a local NIC by design -> must be preserved, not clobbered.
    monkeypatch.setattr(agent_bundle, "detect_local_ips", lambda: ["192.0.2.50"])
    assert agent_bundle.bundle_addresses({"address_mode": "ip", "ip": "8.8.8.8"}) == ["8.8.8.8"]


def test_all_mode_uses_live_nics(monkeypatch):
    monkeypatch.setattr(agent_bundle, "detect_local_ips", lambda: ["192.0.2.50", "192.0.2.51"])
    assert agent_bundle.bundle_addresses({"address_mode": "all", "ip": ""}) == ["192.0.2.50", "192.0.2.51"]
