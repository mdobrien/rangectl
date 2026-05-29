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


def _topo4(backend: LibvirtBackend, db: StateDB) -> Topology:
    """Topo 4: diamond DAG.

        router(eth1) --10.0.1.0/24-- web(eth1)
        router(eth2) --10.0.2.0/24-- db(eth1)
        web(eth2)    --10.0.3.0/24-- monitor(eth1)
        db(eth2)     --10.0.4.0/24-- monitor(eth2)

    Wave 1: router; wave 2: web, db; wave 3: monitor.
    """
    t = Topology("topo4", backend=backend, db=db)
    router = t.node("router", image="ubuntu-22.04", vcpu=1, memory=1024)
    web = t.node("web", image="ubuntu-22.04", vcpu=1, memory=1024,
                 depends_on=[router])
    db_node = t.node("db", image="ubuntu-22.04", vcpu=1, memory=1024,
                     depends_on=[router])
    monitor = t.node("monitor", image="ubuntu-22.04", vcpu=1, memory=1024,
                     depends_on=[web, db_node])

    t.link(router.eth1["10.0.1.1/24"], web.eth1["10.0.1.2/24"])
    t.link(router.eth2["10.0.2.1/24"], db_node.eth1["10.0.2.2/24"])
    t.link(web.eth2["10.0.3.1/24"], monitor.eth1["10.0.3.2/24"])
    t.link(db_node.eth2["10.0.4.1/24"], monitor.eth2["10.0.4.2/24"])
    return t


def _ssh_retry(rng, node_name: str, cmd: str, timeout: int = 60):
    """Run cmd on node, retrying through transient SSH failures after restore."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            r = rng[node_name].exec(cmd)
            if r.exit_code == 0:
                return r
            last = r
        except Exception as exc:
            last = exc
        time.sleep(2)
    raise AssertionError(
        f"ssh exec on {node_name} never succeeded within {timeout}s: {last!r}"
    )


def test_topo4_diamond_snapshot_restore(backend, db):
    topo = _topo4(backend, db)
    with topo.deploy() as rng:
        # All four nodes must have mgmt IPs and be SSH-reachable.
        assert rng["router"].mgmt_ip
        assert rng["web"].mgmt_ip
        assert rng["db"].mgmt_ip
        assert rng["monitor"].mgmt_ip
        log.info("mgmt IPs: router=%s web=%s db=%s monitor=%s",
                 rng["router"].mgmt_ip, rng["web"].mgmt_ip,
                 rng["db"].mgmt_ip, rng["monitor"].mgmt_ip)

        for name in ("router", "web", "db", "monitor"):
            r = rng[name].exec("hostname")
            assert r.exit_code == 0, f"{name} ssh failed: {r.stderr}"
            assert f"topo4-{name}" in r.stdout

        # Direct-link connectivity (no routing needed — endpoints share L2).
        # web -> router (10.0.1.1)
        ping = rng["web"].exec("ping -c 3 -W 2 10.0.1.1")
        assert ping.exit_code == 0, (
            f"web -> router failed:\n{ping.stdout}\n{ping.stderr}"
        )
        # db -> router (10.0.2.1)
        ping = rng["db"].exec("ping -c 3 -W 2 10.0.2.1")
        assert ping.exit_code == 0, (
            f"db -> router failed:\n{ping.stdout}\n{ping.stderr}"
        )
        # monitor -> web (10.0.3.1, on monitor.eth1's link)
        ping = rng["monitor"].exec("ping -c 3 -W 2 10.0.3.1")
        assert ping.exit_code == 0, (
            f"monitor -> web failed:\n{ping.stdout}\n{ping.stderr}"
        )
        # monitor -> db (10.0.4.1, on monitor.eth2's link)
        ping = rng["monitor"].exec("ping -c 3 -W 2 10.0.4.1")
        assert ping.exit_code == 0, (
            f"monitor -> db failed:\n{ping.stdout}\n{ping.stderr}"
        )

        # --- Snapshot / restore cycle ---
        r = rng["monitor"].exec("echo before | sudo tee /tmp/marker")
        assert r.exit_code == 0, f"marker write failed: {r.stderr}"

        rng.snapshot("baseline")
        log.info("snapshot 'baseline' created on all 4 nodes")

        r = rng["monitor"].exec("echo after | sudo tee /tmp/marker")
        assert r.exit_code == 0, f"marker overwrite failed: {r.stderr}"
        r = rng["monitor"].exec("cat /tmp/marker")
        assert r.exit_code == 0
        assert "after" in r.stdout, f"pre-restore marker not 'after': {r.stdout!r}"

        rng.restore("baseline")
        log.info("snapshot 'baseline' restored on all 4 nodes")

        # After restore the backend ensures the VM is running and SSH is
        # reachable. Still allow a small retry budget for the userland to
        # settle.
        r = _ssh_retry(rng, "monitor", "cat /tmp/marker", timeout=60)
        assert "before" in r.stdout, (
            f"post-restore marker not 'before' — restore did not revert state: "
            f"{r.stdout!r}"
        )
