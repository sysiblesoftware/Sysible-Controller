"""In a container, the controller must advertise the operator's reachable address
(SYSIBLE_CONTROLLER_ADDR), never its own docker-bridge NIC IP (172.x).

Regression for: "Regenerate agent bundle" pointing agents at 172.26.0.3 because
bundle_addresses() self-healed the saved LAN IP to the container's bridge IP.
"""
import backend.agent_bundle as ab


def test_container_detect_uses_advertised_addr(monkeypatch):
    monkeypatch.setenv("SYSIBLE_CONTAINER", "1")
    monkeypatch.setenv("SYSIBLE_CONTROLLER_ADDR", "192.168.8.249")
    assert ab.detect_local_ips() == ["192.168.8.249"]


def test_container_bundle_addresses_does_not_selfheal_to_bridge(monkeypatch):
    monkeypatch.setenv("SYSIBLE_CONTAINER", "1")
    monkeypatch.setenv("SYSIBLE_CONTROLLER_ADDR", "192.168.8.249")
    monkeypatch.setattr(ab, "detect_local_ips", lambda: ["172.26.0.3"])
    cfg = {"address_mode": "ip", "ip": "192.168.8.249", "port": 9000}
    assert ab.bundle_addresses(cfg) == ["192.168.8.249"]


def test_host_bundle_addresses_still_selfheals(monkeypatch):
    monkeypatch.delenv("SYSIBLE_CONTAINER", raising=False)
    monkeypatch.setattr(ab, "_is_container", lambda: False)
    monkeypatch.setattr(ab, "detect_local_ips", lambda: ["10.0.0.50"])
    cfg = {"address_mode": "ip", "ip": "10.0.0.9", "port": 9000}
    assert ab.bundle_addresses(cfg) == ["10.0.0.50"]
