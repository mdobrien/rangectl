from __future__ import annotations
import logging

import pytest

from rangectl import Topology
from rangectl.libvirt_backend import LibvirtBackend
from rangectl.state import StateDB
from tests.integration.conftest import pytestmark_skip

log = logging.getLogger(__name__)
pytestmark = pytestmark_skip


def _red_team(backend: LibvirtBackend, db: StateDB) -> Topology:
    """red-team: attacker -- vyos-router -- target across two subnets."""
    t = Topology("red-team", backend=backend, db=db)
    router = t.node("router", image="vyos", vcpu=1, memory=1024)
    attacker = t.node("attacker", image="ubuntu-22.04", vcpu=1, memory=1024,
                      depends_on=[router])
    target = t.node("target", image="ubuntu-22.04", vcpu=1, memory=1024,
                    depends_on=[router])
    t.link(attacker.eth1["10.0.1.2/24"], router.eth1["10.0.1.1/24"])
    t.link(router.eth2["10.0.2.1/24"], target.eth1["10.0.2.2/24"])
    return t


def _blue_team(backend: LibvirtBackend, db: StateDB) -> Topology:
    """blue-team: siem -- sensor, single subnet, no router."""
    t = Topology("blue-team", backend=backend, db=db)
    siem = t.node("siem", image="ubuntu-22.04", vcpu=1, memory=1024)
    sensor = t.node("sensor", image="ubuntu-22.04", vcpu=1, memory=1024)
    t.link(siem.eth1["172.16.0.1/24"], sensor.eth1["172.16.0.2/24"])
    return t


def test_topo6_multi_topology_isolation(backend, db):
    red_topo = _red_team(backend, db)
    blue_topo = _blue_team(backend, db)

    red_rng = red_topo.deploy()
    try:
        blue_rng = blue_topo.deploy()
        try:
            # Both topologies should appear in the registry, on distinct mgmt
            # subnets and distinct mgmt bridges (key isolation invariant).
            listed = {row["name"]: row for row in db.list_topologies()}
            assert "red-team" in listed and "blue-team" in listed, listed
            red_subnet = listed["red-team"]["mgmt_subnet"]
            blue_subnet = listed["blue-team"]["mgmt_subnet"]
            red_bridge = listed["red-team"]["mgmt_bridge"]
            blue_bridge = listed["blue-team"]["mgmt_bridge"]
            assert red_subnet != blue_subnet, (red_subnet, blue_subnet)
            assert red_bridge != blue_bridge, (red_bridge, blue_bridge)
            log.info("red-team mgmt %s on %s; blue-team mgmt %s on %s",
                     red_subnet, red_bridge, blue_subnet, blue_bridge)

            # red-team internal: cross-subnet ping through the router (same as
            # topo2 — Ubuntu hosts need explicit routes for the far subnet).
            red_rng["attacker"].exec("sudo ip route add 10.0.2.0/24 via 10.0.1.1")
            red_rng["target"].exec("sudo ip route add 10.0.1.0/24 via 10.0.2.1")
            red_ping = red_rng["attacker"].exec("ping -c 3 -W 2 10.0.2.2")
            assert red_ping.exit_code == 0, (
                f"red-team internal ping failed:\n"
                f"stdout={red_ping.stdout}\nstderr={red_ping.stderr}"
            )

            # blue-team internal: same-subnet ping over the shared link bridge.
            blue_ping = blue_rng["siem"].exec("ping -c 3 -W 2 172.16.0.2")
            assert blue_ping.exit_code == 0, (
                f"blue-team internal ping failed:\n"
                f"stdout={blue_ping.stdout}\nstderr={blue_ping.stderr}"
            )

            # Isolation: an attacker on the red-team mgmt bridge must not be
            # able to reach a blue-team mgmt IP. Distinct bridges with no
            # inter-bridge route → ARP/route resolution fails.
            siem_mgmt = blue_rng["siem"].mgmt_ip
            sensor_mgmt = blue_rng["sensor"].mgmt_ip
            log.info("Isolation probe: red-team/attacker -> blue-team/siem(%s), sensor(%s)",
                     siem_mgmt, sensor_mgmt)
            iso1 = red_rng["attacker"].exec(f"ping -c 1 -W 2 {siem_mgmt}")
            assert iso1.exit_code != 0, (
                f"ISOLATION BREACH: attacker reached siem({siem_mgmt}):\n"
                f"stdout={iso1.stdout}\nstderr={iso1.stderr}"
            )
            iso2 = red_rng["target"].exec(f"ping -c 1 -W 2 {sensor_mgmt}")
            assert iso2.exit_code != 0, (
                f"ISOLATION BREACH: target reached sensor({sensor_mgmt}):\n"
                f"stdout={iso2.stdout}\nstderr={iso2.stderr}"
            )

            # Destroy red-team while blue-team keeps running. Blue must still
            # work (no shared state on the data plane).
            red_topo.destroy()
            red_rng = None  # don't double-destroy in finally

            still_listed = {row["name"] for row in db.list_topologies()}
            assert still_listed == {"blue-team"}, still_listed

            blue_ping2 = blue_rng["siem"].exec("ping -c 3 -W 2 172.16.0.2")
            assert blue_ping2.exit_code == 0, (
                f"blue-team broke after red-team destroy:\n"
                f"stdout={blue_ping2.stdout}\nstderr={blue_ping2.stderr}"
            )
        finally:
            blue_topo.destroy()
    finally:
        if red_rng is not None:
            try:
                red_topo.destroy()
            except Exception as exc:  # best-effort cleanup
                log.warning("red-team cleanup raised: %s", exc)

    # After both destroyed, no leftover rows.
    assert db.list_topologies() == []
