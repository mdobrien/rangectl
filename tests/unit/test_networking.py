from __future__ import annotations

import pytest

from rangectl.networking import (
    allocate_mgmt_ip,
    bridge_name,
    mgmt_bridge_name,
    mgmt_host_ip,
)


def test_allocate_mgmt_ip_index_zero():
    assert allocate_mgmt_ip("192.168.100.0/24", 0) == "192.168.100.1"


def test_allocate_mgmt_ip_sequential():
    assert allocate_mgmt_ip("192.168.100.0/24", 1) == "192.168.100.2"
    assert allocate_mgmt_ip("192.168.100.0/24", 9) == "192.168.100.10"


def test_allocate_mgmt_ip_different_subnet():
    assert allocate_mgmt_ip("192.168.101.0/24", 0) == "192.168.101.1"


def test_allocate_mgmt_ip_does_not_collide_with_host():
    # .254 reserved for host; allocating up to index 252 should be safe
    assert allocate_mgmt_ip("192.168.100.0/24", 252) == "192.168.100.253"
    with pytest.raises(ValueError):
        allocate_mgmt_ip("192.168.100.0/24", 253)  # would land on .254


def test_mgmt_host_ip():
    assert mgmt_host_ip("192.168.100.0/24") == "192.168.100.254"
    assert mgmt_host_ip("192.168.101.0/24") == "192.168.101.254"


def test_bridge_name():
    n0 = bridge_name("mytopo", 0)
    n5 = bridge_name("mytopo", 5)
    assert n0.startswith("rl-") and n0.endswith("-0")
    assert n5.startswith("rl-") and n5.endswith("-5")
    # Same topology name produces the same prefix.
    assert n0.rsplit("-", 1)[0] == n5.rsplit("-", 1)[0]
    # Different topologies don't collide.
    assert bridge_name("other", 0) != n0
    # Fits in Linux IFNAMSIZ-1 (15).
    assert len(n0) <= 15 and len(n5) <= 15


def test_mgmt_bridge_name():
    assert mgmt_bridge_name("mytopo").startswith("rlmgt-")
    assert len(mgmt_bridge_name("mytopo")) <= 15
    # Stable + collision-resistant.
    assert mgmt_bridge_name("mytopo") == mgmt_bridge_name("mytopo")
    assert mgmt_bridge_name("mytopo") != mgmt_bridge_name("othertopo")
