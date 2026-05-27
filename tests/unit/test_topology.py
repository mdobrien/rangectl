from __future__ import annotations

import pytest

from rangectl import Topology
from rangectl.topology import LiveNode, Range
from rangectl.types import InterfaceSpec


def test_topology_deploy_returns_range(backend, db):
    t = Topology("topo1", backend=backend, db=db)
    t.node("a", image="ubuntu", vcpu=1, memory=1024)
    rng = t.deploy()
    assert isinstance(rng, Range)
    assert "a" in rng._nodes
    assert isinstance(rng["a"], LiveNode)
    # Range should be wired up with engine/db/backend for snapshot/restore/logs.
    assert rng._engine is t._engine
    assert rng._db is db
    assert rng._backend is backend


def test_topology_deploy_without_backend_raises():
    t = Topology("nope")
    t.node("a", image="ubuntu")
    with pytest.raises(RuntimeError):
        t.deploy()


def test_topology_deploy_context_manager_destroys(backend, db):
    t = Topology("ctx", backend=backend, db=db)
    t.node("a", image="ubuntu", vcpu=1, memory=1024)
    with t.deploy() as rng:
        assert rng["a"].name == "a"
    # On exit the engine should have destroyed the topology.
    assert db.get_topology("ctx") is None


def test_topology_destroy_via_engine(backend, db):
    t = Topology("destroyme", backend=backend, db=db)
    t.node("a", image="ubuntu")
    t.deploy()
    t.destroy()
    assert db.get_topology("destroyme") is None


def test_topology_destroy_without_deploy_raises():
    t = Topology("undeployed")
    t.node("a", image="ubuntu")
    with pytest.raises(RuntimeError):
        t.destroy()


def test_topology_node_interface_access():
    t = Topology("ifaces")
    n = t.node("a", image="ubuntu")
    iface = n.eth0
    assert isinstance(iface, InterfaceSpec)
    assert iface.node_name == "a"
    assert iface.interface_name == "eth0"
    # Accessing again returns the same cached spec.
    assert n.eth0 is iface
    # Different interface name yields a different spec.
    iface1 = n.eth1
    assert iface1.interface_name == "eth1"
    assert iface1 is not iface


def test_topology_node_interface_ip_binding():
    t = Topology("ips")
    n = t.node("a", image="ubuntu")
    bound = n.eth0["10.0.1.5/24"]
    assert bound.ip == "10.0.1.5"
    assert bound.cidr == "24"
    assert bound.node_name == "a"
    assert bound.interface_name == "eth0"


def test_topology_export_yaml(tmp_path):
    import yaml
    t = Topology("exp")
    a = t.node("router", image="vyos", vcpu=2, memory=2048)
    b = t.node("target", image="ubuntu", depends_on=[a])
    t.link(a.eth1["10.0.1.1/24"], b.eth1["10.0.1.2/24"])

    out = tmp_path / "topo.yaml"
    t.export(str(out))
    assert out.exists()
    data = yaml.safe_load(out.read_text())
    assert data["name"] == "exp"
    assert {n["name"] for n in data["nodes"]} == {"router", "target"}
    router_data = next(n for n in data["nodes"] if n["name"] == "router")
    assert router_data["image"] == "vyos"
    assert router_data["vcpu"] == 2
    assert router_data["memory"] == 2048
    target_data = next(n for n in data["nodes"] if n["name"] == "target")
    assert target_data["depends_on"] == ["router"]
    assert len(data["links"]) == 1
    assert data["links"][0]["node_a"] == "router"
    assert data["links"][0]["ip_a"] == "10.0.1.1/24"
    assert data["links"][0]["ip_b"] == "10.0.1.2/24"


def test_topology_export_without_deploy(tmp_path):
    """Export must work before deploy (R11)."""
    t = Topology("predeploy")
    t.node("a", image="ubuntu")
    out = tmp_path / "topo.yaml"
    t.export(str(out))
    assert out.exists()


def test_topology_from_yaml_roundtrip(tmp_path):
    t = Topology("rt")
    a = t.node("router", image="vyos", vcpu=2, memory=2048, os="linux")
    b = t.node("target", image="ubuntu", depends_on=[a])
    t.link(a.eth1["10.0.1.1/24"], b.eth1["10.0.1.2/24"])

    out = tmp_path / "topo.yaml"
    t.export(str(out))

    t2 = Topology.from_yaml(str(out))
    assert t2.name == "rt"
    assert set(t2._nodes.keys()) == {"router", "target"}
    assert t2._nodes["router"].vcpu == 2
    assert t2._nodes["router"].memory == 2048
    # depends_on resolved to actual Node references
    assert t2._nodes["target"].depends_on == [t2._nodes["router"]]
    # Links round-tripped with IPs.
    assert len(t2._links) == 1
    lnk = t2._links[0]
    assert lnk.if_a.ip == "10.0.1.1"
    assert lnk.if_a.cidr == "24"
    assert lnk.if_b.ip == "10.0.1.2"
    assert lnk.if_b.cidr == "24"


def test_topology_from_yaml_no_links(tmp_path):
    t = Topology("solo")
    t.node("only", image="ubuntu")
    out = tmp_path / "solo.yaml"
    t.export(str(out))
    t2 = Topology.from_yaml(str(out))
    assert list(t2._nodes.keys()) == ["only"]
    assert t2._links == []
