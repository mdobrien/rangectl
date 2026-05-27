from __future__ import annotations
import logging

import pytest

from rangectl import Topology
from rangectl.libvirt_backend import LibvirtBackend
from rangectl.state import StateDB
from tests.integration.conftest import pytestmark_skip

log = logging.getLogger(__name__)
pytestmark = pytestmark_skip


def _topo1(backend: LibvirtBackend, db: StateDB) -> Topology:
    """Topo 1: two Ubuntu VMs connected via a single point-to-point link."""
    t = Topology("topo1", backend=backend, db=db)
    a = t.node("ubuntu-a", image="ubuntu-22.04", vcpu=1, memory=1024)
    b = t.node("ubuntu-b", image="ubuntu-22.04", vcpu=1, memory=1024)
    # Use eth1 for the topology link — eth0 is implicitly reserved for mgmt.
    t.link(a.eth1["10.0.1.1/24"], b.eth1["10.0.1.2/24"])
    return t


def test_topo1_boots_and_pings(backend, db):
    topo = _topo1(backend, db)
    with topo.deploy() as rng:
        # Each node has a discoverable mgmt IP.
        assert rng["ubuntu-a"].mgmt_ip
        assert rng["ubuntu-b"].mgmt_ip
        log.info("ubuntu-a mgmt %s, ubuntu-b mgmt %s",
                 rng["ubuntu-a"].mgmt_ip, rng["ubuntu-b"].mgmt_ip)

        # Confirm SSH works on both nodes.
        result_a = rng["ubuntu-a"].exec("hostname")
        assert result_a.exit_code == 0, f"hostname on a failed: {result_a.stderr}"
        assert "topo1-ubuntu-a" in result_a.stdout

        result_b = rng["ubuntu-b"].exec("hostname")
        assert result_b.exit_code == 0, f"hostname on b failed: {result_b.stderr}"
        assert "topo1-ubuntu-b" in result_b.stdout

        # Topology link must carry traffic between nodes.
        ping = rng["ubuntu-a"].exec("ping -c 3 -W 2 10.0.1.2")
        assert ping.exit_code == 0, (
            f"ping a->b failed:\nstdout={ping.stdout}\nstderr={ping.stderr}"
        )

        ping_back = rng["ubuntu-b"].exec("ping -c 3 -W 2 10.0.1.1")
        assert ping_back.exit_code == 0, (
            f"ping b->a failed:\nstdout={ping_back.stdout}\nstderr={ping_back.stderr}"
        )
