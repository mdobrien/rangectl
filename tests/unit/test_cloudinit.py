"""Unit tests for cloud-init seed generation (rangectl/cloudinit.py).

Regression focus: network-config MUST emit the interface MAC as a quoted YAML
string. An all-numeric MAC whose octets are each <= 59 (e.g. 52:54:00:35:18:16)
is parsed by YAML 1.1 as a base-60 (sexagesimal) integer when unquoted, which
makes cloud-init/netplan see an int instead of a string and crash device
matching ("'int' object has no attribute 'lower'"), so the interface is never
configured. This was the node-b SSH-timeout root cause (issue 20260529-9).
"""
import yaml

from rangectl.cloudinit import _network_config

# The exact MAC that broke node b in topology "nsinetnone": every octet is a
# digit string <= 59, so YAML reads it as 52*60^5 + 54*60^4 + 35*60^2 + 18*60
# + 16 = 41135167096 unless it is quoted.
SEXAGESIMAL_MAC = "52:54:00:35:18:16"
HEX_MAC = "52:54:00:af:f5:b6"  # has hex letters -> always a string (node a's case)


def test_sexagesimal_mac_parses_as_int_when_unquoted():
    """Guards the assumption behind the fix: an unquoted all-numeric MAC really
    is mis-parsed as a base-60 integer by the YAML loader."""
    parsed = yaml.safe_load(f"macaddress: {SEXAGESIMAL_MAC}")
    assert parsed["macaddress"] == 41135167096
    assert isinstance(parsed["macaddress"], int)


def test_network_config_macaddress_is_quoted_string():
    """_network_config must emit the MAC so it round-trips through YAML as the
    original string, not an integer — for both sexagesimal and hex MACs."""
    for mac in (SEXAGESIMAL_MAC, HEX_MAC):
        cfg = _network_config([
            {"mac": mac, "ip": "192.168.100.2", "cidr": "24",
             "gateway": "192.168.100.254"},
        ])
        loaded = yaml.safe_load(cfg)
        match = loaded["ethernets"]["if0"]["match"]
        assert match["macaddress"] == mac, f"MAC {mac} did not round-trip"
        assert isinstance(match["macaddress"], str)


def test_network_config_emits_addresses_and_gateway():
    """Sanity: the static address and default route are still present."""
    cfg = _network_config([
        {"mac": SEXAGESIMAL_MAC, "ip": "192.168.100.2", "cidr": "24",
         "gateway": "192.168.100.254"},
        {"mac": HEX_MAC, "ip": "10.0.1.2", "cidr": "24", "gateway": None},
    ])
    loaded = yaml.safe_load(cfg)
    if0 = loaded["ethernets"]["if0"]
    assert if0["addresses"] == ["192.168.100.2/24"]
    assert if0["routes"][0]["via"] == "192.168.100.254"
    # Second iface has no gateway -> no routes block.
    assert "routes" not in loaded["ethernets"]["if1"]
