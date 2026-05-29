from __future__ import annotations
import logging
import time

import pytest

from rangectl import Topology
from rangectl.libvirt_backend import LibvirtBackend
from rangectl.state import StateDB
from tests.integration.conftest import pytestmark_skip

log = logging.getLogger(__name__)
pytestmark = pytestmark_skip


def _topo5(backend: LibvirtBackend, db: StateDB) -> Topology:
    """Topo 5: same shape as Topo 2 (ubuntu-a -- router -- ubuntu-b)."""
    t = Topology("topo5", backend=backend, db=db)
    router = t.node("router", image="vyos", vcpu=1, memory=1024)
    a = t.node("ubuntu-a", image="ubuntu-22.04", vcpu=1, memory=1024,
               depends_on=[router])
    b = t.node("ubuntu-b", image="ubuntu-22.04", vcpu=1, memory=1024,
               depends_on=[router])
    t.link(a.eth1["10.0.1.2/24"], router.eth1["10.0.1.1/24"])
    t.link(router.eth2["10.0.2.1/24"], b.eth1["10.0.2.2/24"])
    return t


def _ping(node, target: str, count: int = 1, wait: int = 3):
    return node.exec(f"ping -c {count} -W {wait} {target}")


def _ping_with_retry(node, target: str, attempts: int = 10, delay: float = 2.0):
    """Poll ping until success or the budget runs out. Returns the final ExecResult."""
    last = None
    for i in range(attempts):
        last = _ping(node, target, count=1, wait=3)
        if last.exit_code == 0:
            log.info("ping %s succeeded on attempt %d", target, i + 1)
            return last
        log.info("ping %s attempt %d failed (rc=%d), retrying...",
                 target, i + 1, last.exit_code)
        time.sleep(delay)
    return last


def test_topo5_link_toggle(backend, db):
    topo = _topo5(backend, db)
    with topo.deploy() as rng:
        # Sanity: mgmt reachable on all three nodes.
        assert rng["router"].mgmt_ip
        assert rng["ubuntu-a"].mgmt_ip
        assert rng["ubuntu-b"].mgmt_ip

        # Add the same cross-subnet routes Topo 2 installs.
        rng["ubuntu-a"].exec("sudo ip route add 10.0.2.0/24 via 10.0.1.1")
        rng["ubuntu-b"].exec("sudo ip route add 10.0.1.0/24 via 10.0.2.1")

        # Baseline: ubuntu-a -> ubuntu-b through router should work.
        baseline = _ping(rng["ubuntu-a"], "10.0.2.2", count=3, wait=2)
        assert baseline.exit_code == 0, (
            f"baseline ping a->b failed:\n"
            f"stdout={baseline.stdout}\nstderr={baseline.stderr}"
        )

        # Bring down the router <-> ubuntu-b link.
        link = rng.link("router", "ubuntu-b")
        log.info("Bringing down link router<->ubuntu-b (bridge=%s)",
                 link._bridge_name)
        link.down()
        assert not link._is_up

        # Now the cross-subnet path is broken — ubuntu-a cannot reach ubuntu-b.
        # Use a short single-shot ping; assert failure.
        down_ping = _ping(rng["ubuntu-a"], "10.0.2.2", count=1, wait=3)
        assert down_ping.exit_code != 0, (
            "ping a->b should FAIL after link.down() but it succeeded:\n"
            f"stdout={down_ping.stdout}\nstderr={down_ping.stderr}"
        )
        log.info("link-down ping correctly failed (rc=%d)", down_ping.exit_code)

        # Restore the link.
        log.info("Bringing link back up")
        link.up()
        assert link._is_up

        # Connectivity should come back. The bridge was recreated and TAPs
        # re-enslaved; give it a moment via retry loop.
        up_ping = _ping_with_retry(rng["ubuntu-a"], "10.0.2.2",
                                   attempts=10, delay=2.0)
        assert up_ping.exit_code == 0, (
            "ping a->b should SUCCEED after link.up() but kept failing:\n"
            f"stdout={up_ping.stdout}\nstderr={up_ping.stderr}"
        )
