from __future__ import annotations

import pytest

from rangectl import Topology
from rangectl.backend import HostResources
from rangectl.engine import Engine
from rangectl.topology import LiveNode, Range
from rangectl.types import NodeState, ResourceError


def _single_node_topo(name: str = "single") -> Topology:
    t = Topology(name)
    t.node("a", image="ubuntu", vcpu=1, memory=1024)
    return t


def _two_node_topo(name: str = "pair") -> Topology:
    t = Topology(name)
    a = t.node("a", image="ubuntu", vcpu=1, memory=1024)
    b = t.node("b", image="ubuntu", vcpu=1, memory=1024)
    t.link(a.eth1["10.0.0.1/24"], b.eth1["10.0.0.2/24"])
    return t


def _dependency_chain_topo(name: str = "chain") -> Topology:
    t = Topology(name)
    a = t.node("a", image="ubuntu", vcpu=1, memory=1024)
    t.node("b", image="ubuntu", vcpu=1, memory=1024, depends_on=[a])
    return t


def test_deploy_single_node(backend, db):
    engine = Engine(backend, db)
    rng = engine.deploy(_single_node_topo())
    assert isinstance(rng, Range)
    assert "a" in rng._nodes
    assert isinstance(rng["a"], LiveNode)
    assert rng["a"].name == "a"
    assert rng["a"].topology_name == "single"


def test_deploy_records_backend_calls(backend, db):
    engine = Engine(backend, db)
    engine.deploy(_single_node_topo())
    names = [c[0] for c in backend.calls]
    # mgmt bridge first
    assert names.index("create_bridge") < names.index("create_overlay")
    assert "create_overlay" in names
    assert "create_vm" in names
    assert "start" in names
    assert "attach_interface" in names
    # overlay before vm
    assert names.index("create_overlay") < names.index("create_vm")
    # vm created before started
    assert names.index("create_vm") < names.index("start")


def test_deploy_saves_topology_to_db(backend, db):
    engine = Engine(backend, db)
    engine.deploy(_single_node_topo("saved"))
    row = db.get_topology("saved")
    assert row is not None
    assert row["name"] == "saved"
    assert row["mgmt_subnet"]
    assert row["mgmt_bridge"] == "rangectl-mgmt-saved"


def test_deploy_saves_nodes_to_db(backend, db):
    engine = Engine(backend, db)
    engine.deploy(_single_node_topo("withnode"))
    cur = db._conn.execute(
        "SELECT name, state, mgmt_ip FROM nodes WHERE topology_name=?",
        ("withnode",),
    )
    rows = cur.fetchall()
    assert len(rows) == 1
    name, state, mgmt_ip = rows[0]
    assert name == "a"
    assert state == NodeState.RUNNING.value
    assert mgmt_ip


def test_deploy_two_nodes_with_link(backend, db):
    engine = Engine(backend, db)
    engine.deploy(_two_node_topo())
    # one topology bridge + one mgmt bridge
    bridges = [c[1][0] for c in backend.calls if c[0] == "create_bridge"]
    assert "rangectl-mgmt-pair" in bridges
    # one extra link bridge
    assert len([b for b in bridges if b != "rangectl-mgmt-pair"]) == 1
    # each node attaches mgmt + topology interfaces = 4 attach_interface calls
    attaches = backend.calls_of("attach_interface")
    assert len(attaches) == 4


def test_deploy_wave_ordering(backend, db):
    engine = Engine(backend, db)
    engine.deploy(_dependency_chain_topo())
    # find create_vm call order — node a must precede node b
    vm_specs = [c[1][0] for c in backend.calls if c[0] == "create_vm"]
    names = [s.name for s in vm_specs]
    assert names.index("chain-a") < names.index("chain-b")


def test_deploy_assigns_mgmt_ips(backend, db):
    engine = Engine(backend, db)
    rng = engine.deploy(_two_node_topo("ips"))
    ip_a = rng["a"].mgmt_ip
    ip_b = rng["b"].mgmt_ip
    assert ip_a and ip_b
    assert ip_a != ip_b
    assert ip_a.startswith("192.168.")
    assert ip_b.startswith("192.168.")


def test_destroy_cleans_up(backend, db):
    engine = Engine(backend, db)
    topo = _two_node_topo("delclean")
    engine.deploy(topo)
    backend.calls.clear()
    engine.destroy(topo)
    names = [c[0] for c in backend.calls]
    assert names.count("stop") == 2
    assert names.count("destroy") == 2
    # both topology bridge + mgmt bridge deleted
    assert names.count("delete_bridge") >= 2


def test_destroy_removes_from_db(backend, db):
    engine = Engine(backend, db)
    topo = _single_node_topo("gone")
    engine.deploy(topo)
    engine.destroy(topo)
    assert db.get_topology("gone") is None


def test_deploy_insufficient_resources(backend, db):
    backend.host_resources_result = HostResources(
        total_vcpu=1, total_memory_mb=512, total_disk_mb=500_000,
        available_vcpu=1, available_memory_mb=512, available_disk_mb=500_000,
    )
    engine = Engine(backend, db)
    topo = _two_node_topo("noresources")
    with pytest.raises(ResourceError):
        engine.deploy(topo)
    # no VMs created
    assert backend.calls_of("create_vm") == []


def test_range_context_manager_destroys(backend, db):
    engine = Engine(backend, db)
    topo = _single_node_topo("ctx")
    rng = engine.deploy(topo)
    # Wire the range's topology destroy through engine for the test
    rng.topology.destroy = lambda: engine.destroy(topo)  # type: ignore
    with rng:
        pass
    assert db.get_topology("ctx") is None
