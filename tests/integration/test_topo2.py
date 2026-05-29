from __future__ import annotations
import logging

import pytest

from rangectl import Topology
from rangectl.libvirt_backend import LibvirtBackend
from rangectl.state import StateDB
from tests.integration.conftest import pytestmark_skip

log = logging.getLogger(__name__)
pytestmark = pytestmark_skip


def _topo2(backend: LibvirtBackend, db: StateDB) -> Topology:
    """Topo 2: ubuntu-a -- vyos-router -- ubuntu-b across two subnets."""
    t = Topology("topo2", backend=backend, db=db)
    router = t.node("router", image="vyos", vcpu=1, memory=1024)
    a = t.node("ubuntu-a", image="ubuntu-22.04", vcpu=1, memory=1024,
               depends_on=[router])
    b = t.node("ubuntu-b", image="ubuntu-22.04", vcpu=1, memory=1024,
               depends_on=[router])
    # Subnet 10.0.1.0/24: ubuntu-a <-> router
    t.link(a.eth1["10.0.1.2/24"], router.eth1["10.0.1.1/24"])
    # Subnet 10.0.2.0/24: router <-> ubuntu-b
    t.link(router.eth2["10.0.2.1/24"], b.eth1["10.0.2.2/24"])
    return t


def test_topo2_routed_ping(backend, db):
    topo = _topo2(backend, db)
    with topo.deploy() as rng:
        assert rng["router"].mgmt_ip
        assert rng["ubuntu-a"].mgmt_ip
        assert rng["ubuntu-b"].mgmt_ip
        log.info("mgmt IPs: router=%s a=%s b=%s",
                 rng["router"].mgmt_ip,
                 rng["ubuntu-a"].mgmt_ip,
                 rng["ubuntu-b"].mgmt_ip)

        # Sanity: SSH works on the router. Use a plain linux command so
        # we exercise the SSH/kernel path without needing the VyOS CLI
        # wrapper (vbash) that's only loaded in interactive shells.
        out = rng["router"].exec("ip -br addr show")
        assert out.exit_code == 0, (
            f"router ip addr failed: stdout={out.stdout!r} "
            f"stderr={out.stderr!r}"
        )
        log.info("router interfaces:\n%s", out.stdout)
        # The bootstrap renamed e<i+2> -> eth<i> and pinned hw-id, so the
        # router should expose eth0/eth1/eth2 with the IPs we configured.
        assert "eth0" in out.stdout and "192.168.100.1/24" in out.stdout
        assert "eth1" in out.stdout and "10.0.1.1/24" in out.stdout
        assert "eth2" in out.stdout and "10.0.2.1/24" in out.stdout

        # Sanity: SSH works on ubuntu hosts.
        for name in ("ubuntu-a", "ubuntu-b"):
            r = rng[name].exec("hostname")
            assert r.exit_code == 0
            assert f"topo2-{name}" in r.stdout

        # Install routes on the Ubuntu hosts to reach the far subnet through
        # the router. cloud-init's network-config doesn't know about the far
        # subnet — easier to just add the route post-deploy.
        rng["ubuntu-a"].exec("sudo ip route add 10.0.2.0/24 via 10.0.1.1")
        rng["ubuntu-b"].exec("sudo ip route add 10.0.1.0/24 via 10.0.2.1")

        # First hop: ubuntu-a -> router (10.0.1.1)
        ping1 = rng["ubuntu-a"].exec("ping -c 3 -W 2 10.0.1.1")
        assert ping1.exit_code == 0, (
            f"ubuntu-a -> router failed:\nstdout={ping1.stdout}\n"
            f"stderr={ping1.stderr}"
        )

        # Cross-subnet: ubuntu-a -> ubuntu-b (10.0.2.2) through the router.
        ping2 = rng["ubuntu-a"].exec("ping -c 3 -W 2 10.0.2.2")
        assert ping2.exit_code == 0, (
            f"ubuntu-a -> ubuntu-b through router failed:\n"
            f"stdout={ping2.stdout}\nstderr={ping2.stderr}"
        )

        # Reverse direction.
        ping3 = rng["ubuntu-b"].exec("ping -c 3 -W 2 10.0.1.2")
        assert ping3.exit_code == 0, (
            f"ubuntu-b -> ubuntu-a through router failed:\n"
            f"stdout={ping3.stdout}\nstderr={ping3.stderr}"
        )
