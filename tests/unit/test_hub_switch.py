"""Unit tests for Phase 20 — hub & switch L2 node types.

Design: scratch/issues/20260609-2-phase20-hub-switch-design.md (D1-D8).
A switch is a user-named Linux bridge (MAC learning on); a hub is the same
bridge with per-port ``learning off flood on``. L2 nodes never boot — they
sprint through the state machine during infra setup, before any VM.
"""
from __future__ import annotations

import pytest

from rangectl import Topology
from rangectl.engine import Engine, _l2_veth_names, _mac_for
from rangectl.topology import L2Node, Link, LinkEndpoint, Range
from rangectl.types import CycleError, InterfaceSpec, NodeState, OSType


# --- SDK surface (D3) --------------------------------------------------------

def test_switch_returns_l2_node():
    t = Topology("lab")
    sw = t.switch("core")
    assert isinstance(sw, L2Node)
    assert sw.os_type is OSType.SWITCH
    assert sw.is_l2
    assert sw.bridge_name == "sw-core"
    assert t._nodes["core"] is sw


def test_hub_returns_l2_node():
    t = Topology("lab")
    hub = t.hub("mon")
    assert hub.os_type is OSType.HUB
    assert hub.is_l2
    assert hub.bridge_name == "hub-mon"


def test_vm_node_is_not_l2():
    t = Topology("lab")
    vm = t.node("a", image="ubuntu")
    assert not vm.is_l2


def test_lazy_ports_created_on_access():
    t = Topology("lab")
    sw = t.switch("core")
    p = sw.port0
    assert isinstance(p, InterfaceSpec)
    assert p.node_name == "core"
    assert p.interface_name == "port0"
    assert sw.port0 is p  # same spec on re-access


def test_ports_cap_enforced():
    t = Topology("lab")
    sw = t.switch("core", ports=2)
    sw.port0
    sw.port1
    with pytest.raises(ValueError, match="port"):
        sw.port2


def test_no_cap_allows_any_port_index():
    t = Topology("lab")
    sw = t.switch("core")
    assert sw.port47.interface_name == "port47"


def test_eth_attr_rejected_on_l2():
    t = Topology("lab")
    sw = t.switch("core")
    with pytest.raises(AttributeError):
        sw.eth1


def test_l2_rejects_image_container_ready_when():
    t = Topology("lab")
    with pytest.raises(TypeError):
        t.switch("core", image="ubuntu")
    with pytest.raises(TypeError):
        t.hub("mon", container="alpine")
    with pytest.raises(TypeError):
        t.switch("core", ready_when=object())


def test_bridge_name_too_long_raises():
    # IFNAMSIZ caps device names at 15 chars: "sw-" + 13 chars = 16.
    t = Topology("lab")
    with pytest.raises(ValueError, match="15"):
        t.switch("a-very-long-switch-name")
    t.switch("twelve-chars")  # sw- + 12 = 15, exactly at the cap


def test_range_lifecycle_exposes_switch_and_hub(backend, db):
    class Lab(Range):
        name = "l2lab"

        def define_nodes(self):
            self.a = self.node("a", image="ubuntu")
            self.sw = self.switch("core")
            self.hb = self.hub("mon")

        def define_network(self):
            self.link(self.a.eth1["10.0.1.1/24"], self.sw.port0)

        def verify(self):
            pass

    lab = Lab()
    lab.deploy(backend=backend, db=db)
    assert "sw-core" in backend.bridges
    assert "hub-mon" in backend.bridges


# --- deploy: bridges, boot skip, state sprint (D1, D5, D8) -------------------

def _vm_l2_topo(name="lab", l2="switch"):
    t = Topology(name)
    a = t.node("a", image="ubuntu")
    dev = t.switch("core") if l2 == "switch" else t.hub("core")
    t.link(a.eth1["10.0.1.1/24"], dev.port0)
    return t


def test_deploy_creates_l2_bridge_before_vm_boot(backend, db):
    Engine(backend, db).deploy(_vm_l2_topo())
    names = [c[0] for c in backend.calls]
    bridge_calls = [i for i, c in enumerate(backend.calls)
                    if c[0] == "create_bridge" and c[1][0] == "sw-core"]
    assert bridge_calls, "sw-core bridge never created"
    assert bridge_calls[0] < names.index("create_vm")


def test_l2_bridge_recorded_in_bridges_table(backend, db):
    Engine(backend, db).deploy(_vm_l2_topo(l2="hub"))
    rows = db.list_bridges("lab")
    hub_rows = [r for r in rows if r["name"] == "hub-core"]
    assert len(hub_rows) == 1
    assert hub_rows[0]["bridge_type"] == "hub"


def test_vm_l2_link_attaches_tap_to_l2_bridge_no_data_bridge(backend, db):
    Engine(backend, db).deploy(_vm_l2_topo())
    # No per-link data bridge: only mgmt + the switch bridge exist.
    created = [c[1][0] for c in backend.calls if c[0] == "create_bridge"]
    assert "sw-core" in created
    assert not any("data" in b or "br0" in b for b in created)
    # The VM side attaches straight onto the switch bridge.
    attaches = [(a[1], ) for a, _ in backend.calls_of("attach_interface")]
    assert ("sw-core",) in attaches
    # Domain XML wires eth1 to the switch bridge too.
    spec = next(iter(backend.vms.values()))
    eth1 = [i for i in spec.interfaces if i.interface_name == "eth1"][0]
    assert eth1.bridge == "sw-core"


def test_l2_node_skips_boot_and_mgmt(backend, db):
    Engine(backend, db).deploy(_vm_l2_topo())
    # No VM-lifecycle calls for the switch: 1 VM total, 1 overlay, no exec.
    assert len(backend.vms) == 1
    assert len(backend.calls_of("create_overlay")) == 1
    # No mgmt interface on the L2 node (D8): every attach with the mgmt MAC
    # belongs to node "a".
    mgmt_macs = [a[2] for a, _ in backend.calls_of("attach_interface")]
    assert _mac_for("lab", "core", "mgmt") not in mgmt_macs
    row = [n for n in db.list_nodes("lab") if n["name"] == "core"][0]
    assert row["mgmt_ip"] is None
    assert row["vm_id"] is None
    assert row["state"] == NodeState.RUNNING.value
    assert row["os_type"] == "switch"


def test_l2_node_state_object_reaches_running(backend, db):
    t = _vm_l2_topo()
    Engine(backend, db).deploy(t)
    assert t._nodes["core"].state is NodeState.RUNNING


def test_depends_on_switch_works(backend, db):
    t = Topology("lab")
    sw = t.switch("core")
    a = t.node("a", image="ubuntu", depends_on=[sw])
    t.link(a.eth1["10.0.1.1/24"], sw.port0)
    rng = Engine(backend, db).deploy(t)
    assert "a" in rng._nodes


def test_l2_nodes_have_no_livenode(backend, db):
    rng = Engine(backend, db).deploy(_vm_l2_topo())
    assert "a" in rng._nodes
    assert "core" not in rng._nodes


# --- hub mechanics (D2) ------------------------------------------------------

def test_hub_ports_get_learning_off_flood_on(backend, db):
    Engine(backend, db).deploy(_vm_l2_topo(l2="hub"))
    flags = backend.calls_of("set_port_flags")
    assert flags, "hub port never got learning/flood flags"
    (port,), kw = flags[0]
    assert port == "tap-vm-1"
    assert kw == {"learning": False, "flood": True}


def test_switch_ports_do_not_get_flags(backend, db):
    Engine(backend, db).deploy(_vm_l2_topo(l2="switch"))
    assert backend.calls_of("set_port_flags") == []


# --- L2 <-> L2 links (D4) ----------------------------------------------------

def _l2_l2_topo(name="lab"):
    t = Topology(name)
    sw = t.switch("core")
    hub = t.hub("mon")
    a = t.node("a", image="ubuntu")
    t.link(a.eth1["10.0.1.1/24"], sw.port0)
    t.link(sw.port1, hub.port0)
    return t


def test_l2_l2_link_uses_veth_pair(backend, db):
    Engine(backend, db).deploy(_l2_l2_topo())
    veths = backend.calls_of("create_veth_pair")
    assert len(veths) == 1
    (va, vb, br_a, br_b), _ = veths[0]
    assert (br_a, br_b) == ("sw-core", "hub-mon")
    assert va == _l2_veth_names("lab", 1)[0]
    assert vb == _l2_veth_names("lab", 1)[1]
    assert len(va) <= 15 and len(vb) <= 15


def test_l2_l2_hub_side_veth_gets_flags(backend, db):
    Engine(backend, db).deploy(_l2_l2_topo())
    flagged = [a[0] for a, _ in backend.calls_of("set_port_flags")]
    _, vb = _l2_veth_names("lab", 1)
    assert vb in flagged          # hub end gets learning off flood on
    assert len(flagged) == 1      # switch end does not


def test_l2_l2_link_creates_no_data_bridge(backend, db):
    Engine(backend, db).deploy(_l2_l2_topo())
    created = [c[1][0] for c in backend.calls if c[0] == "create_bridge"]
    assert sorted(b for b in created if b.startswith(("sw-", "hub-"))) == \
        ["hub-mon", "sw-core"]
    assert not any(b.startswith("data") for b in created)


# --- cycle detection (D7) ----------------------------------------------------

def test_l2_cycle_aborts_naming_loop(backend, db):
    t = Topology("lab")
    s1, s2, s3 = t.switch("s1"), t.switch("s2"), t.switch("s3")
    t.link(s1.port0, s2.port0)
    t.link(s2.port1, s3.port0)
    t.link(s3.port1, s1.port1)
    with pytest.raises(CycleError) as exc:
        Engine(backend, db).deploy(t)
    msg = str(exc.value)
    assert "s1" in msg and "s2" in msg and "s3" in msg


def test_parallel_l2_links_abort(backend, db):
    t = Topology("lab")
    s1, s2 = t.switch("s1"), t.switch("s2")
    t.link(s1.port0, s2.port0)
    t.link(s1.port1, s2.port1)
    with pytest.raises(CycleError):
        Engine(backend, db).deploy(t)


def test_switch_chain_does_not_abort(backend, db):
    t = Topology("lab")
    s1, s2, s3 = t.switch("s1"), t.switch("s2"), t.switch("s3")
    t.link(s1.port0, s2.port0)
    t.link(s2.port1, s3.port0)
    Engine(backend, db).deploy(t)  # no raise


def test_vm_links_do_not_count_for_cycles(backend, db):
    # A VM with NICs on two switches is not an L2 loop (the guest does not
    # bridge its interfaces).
    t = Topology("lab")
    s1, s2 = t.switch("s1"), t.switch("s2")
    a = t.node("a", image="ubuntu")
    t.link(a.eth1["10.0.1.1/24"], s1.port0)
    t.link(a.eth2["10.0.2.1/24"], s2.port0)
    Engine(backend, db).deploy(t)  # no raise


def test_cycle_abort_leaves_no_state(backend, db):
    t = Topology("lab")
    s1, s2 = t.switch("s1"), t.switch("s2")
    t.link(s1.port0, s2.port0)
    t.link(s1.port1, s2.port1)
    with pytest.raises(CycleError):
        Engine(backend, db).deploy(t)
    assert db.get_topology("lab") is None


# --- impairment interplay (D6) ------------------------------------------------

def test_impair_vm_l2_targets_single_tap(backend, db):
    t = _vm_l2_topo()
    rng = Engine(backend, db).deploy(t)
    rng.link("a", "core").impair(latency="100ms")
    assert backend.tc_taps() == {"tap-vm-1"}


def test_impair_outbound_toward_l2_raises(backend, db):
    t = _vm_l2_topo()
    rng = Engine(backend, db).deploy(t)
    with pytest.raises(ValueError, match="L2"):
        rng.link("a", "core").impair(latency="100ms", outbound="core")


def test_impair_outbound_vm_side_works(backend, db):
    t = _vm_l2_topo()
    rng = Engine(backend, db).deploy(t)
    rng.link("a", "core").impair(latency="100ms", outbound="a")
    assert backend.tc_taps() == {"tap-vm-1"}


def test_impair_l2_l2_targets_both_veth_ends(backend, db):
    t = _l2_l2_topo()
    rng = Engine(backend, db).deploy(t)
    rng.link("core", "mon").impair(latency="50ms")
    va, vb = _l2_veth_names("lab", 1)
    assert backend.tc_taps() == {va, vb}


def test_impair_l2_l2_outbound_raises(backend, db):
    t = _l2_l2_topo()
    rng = Engine(backend, db).deploy(t)
    with pytest.raises(ValueError, match="L2"):
        rng.link("core", "mon").impair(latency="50ms", outbound="mon")


def test_clear_l2_l2(backend, db):
    t = _l2_l2_topo()
    rng = Engine(backend, db).deploy(t)
    link = rng.link("core", "mon")
    link.impair(latency="50ms")
    backend.calls.clear()
    link.clear()
    dels = [c for c in backend.tc_cmds() if "del" in c]
    assert len(dels) == 2
    assert link.impairments == {"core": {}, "mon": {}}


def test_default_impairments_on_vm_l2_link(backend, db):
    t = Topology("lab")
    a = t.node("a", image="ubuntu")
    sw = t.switch("core")
    t.link(a.eth1["10.0.1.1/24"], sw.port0, latency="75ms")
    Engine(backend, db).deploy(t)
    cmds = backend.tc_cmds()
    assert any("75ms" in c for c in cmds)
    assert backend.tc_taps() == {"tap-vm-1"}


def test_impairments_property_vm_l2(backend, db):
    t = _vm_l2_topo()
    rng = Engine(backend, db).deploy(t)
    link = rng.link("a", "core")
    link.impair(latency="100ms")
    # Symmetric semantics collapse to the single VM TAP; the L2 side carries
    # no device, so it stays clean.
    assert link.impairments == {"a": {"latency": "100ms"}, "core": {}}


# --- link down/up with L2 endpoints -------------------------------------------

def test_link_down_up_vm_hub_reapplies_flags(backend, db):
    t = _vm_l2_topo(l2="hub")
    rng = Engine(backend, db).deploy(t)
    link = rng.link("a", "core")
    link.down()
    assert "hub-core" not in backend.bridges
    backend.calls.clear()
    link.up()
    assert "hub-core" in backend.bridges
    flags = backend.calls_of("set_port_flags")
    assert flags and flags[0][0][0] == "tap-vm-1"
    assert flags[0][1] == {"learning": False, "flood": True}


def test_link_down_up_l2_l2_recreates_veth(backend, db):
    t = _l2_l2_topo()
    rng = Engine(backend, db).deploy(t)
    link = rng.link("core", "mon")
    va, vb = _l2_veth_names("lab", 1)
    link.down()
    assert backend.calls_of("delete_device")[0][0] == (va,)
    backend.calls.clear()
    link.up()
    pairs = backend.calls_of("create_veth_pair")
    assert pairs and pairs[0][0] == (va, vb, "sw-core", "hub-mon")
    # Hub end re-flagged after recreate.
    assert vb in [a[0] for a, _ in backend.calls_of("set_port_flags")]


# --- LinkEndpoint resolution (D6 unit) -----------------------------------------

def test_link_endpoint_resolves_tap_lazily(backend):
    ep = LinkEndpoint(node_name="a", vm_id="vm-9", mac="52:54:00:00:00:01")
    assert ep.resolve(backend) == "tap-vm-9"
    # Lazy: resolution goes through the backend each time (never cached).
    assert len(backend.calls_of("_find_tap_for_mac")) == 1
    ep.resolve(backend)
    assert len(backend.calls_of("_find_tap_for_mac")) == 2


def test_link_endpoint_static_dev_skips_backend(backend):
    ep = LinkEndpoint(node_name="sw", is_l2=True, dev="l2a1234")
    assert ep.resolve(backend) == "l2a1234"
    assert backend.calls_of("_find_tap_for_mac") == []


def test_link_endpoint_l2_without_dev_resolves_none(backend):
    ep = LinkEndpoint(node_name="sw", is_l2=True)
    assert ep.resolve(backend) is None


def test_yaml_roundtrip_preserves_l2_nodes(tmp_path):
    t = Topology("lab")
    a = t.node("a", image="ubuntu")
    sw = t.switch("core")
    t.link(a.eth1["10.0.1.1/24"], sw.port0)
    path = str(tmp_path / "topo.yaml")
    t.export(path)
    restored = Topology.from_yaml(path)
    core = restored._nodes["core"]
    assert isinstance(core, L2Node)
    assert core.os_type is OSType.SWITCH
    assert len(restored._links) == 1


# --- StateDB ------------------------------------------------------------------

def test_list_bridges(db):
    db.save_topology("lab", "running", "10.255.1.0/24", "mgmt-br")
    db._conn.execute(
        "INSERT INTO bridges (topology_name, name, bridge_type) VALUES (?,?,?)",
        ("lab", "sw-core", "switch"))
    db._conn.commit()
    rows = db.list_bridges("lab")
    assert rows == [{"id": rows[0]["id"], "topology_name": "lab",
                     "name": "sw-core", "subnet": None,
                     "bridge_type": "switch", "vlan_aware": 0}]


# --- Range.connect rebuild (D8) -------------------------------------------------

def _seed_l2_range(tmp_path, monkeypatch, with_l2_l2=False):
    import json as _json
    from pathlib import Path as _Path

    from rangectl import topology as topo_mod

    db_file = str(tmp_path / "rangectl.db")
    range_dir = str(tmp_path / "ranges")
    monkeypatch.setattr(topo_mod, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(topo_mod, "_netns_exists", lambda ns: True)
    monkeypatch.setattr(topo_mod.cgroup, "is_frozen", lambda name: False)
    monkeypatch.setattr(topo_mod.supervisor, "DEFAULT_RANGE_DIR", range_dir)

    from rangectl.state import StateDB
    db = StateDB(db_file)
    db.save_topology("lab", "running", "10.255.1.0/24", "mgmt-br")
    db.save_node(topology_name="lab", name="a", image="ubuntu", vcpu=1,
                 memory_mb=1024, os_type="linux", state="running",
                 mgmt_ip="10.255.1.1", vm_id="lab-a")
    db.save_node(topology_name="lab", name="core", image="", vcpu=0,
                 memory_mb=0, os_type="hub", state="running")
    db._conn.execute(
        "INSERT INTO links (topology_name, node_a, iface_a, ip_a, node_b, "
        "iface_b, ip_b, bridge_name) VALUES (?,?,?,?,?,?,?,?)",
        ("lab", "a", "eth1", "10.0.1.1", "core", "port0", None, "hub-core"))
    if with_l2_l2:
        db.save_node(topology_name="lab", name="edge", image="", vcpu=0,
                     memory_mb=0, os_type="switch", state="running")
        db._conn.execute(
            "INSERT INTO links (topology_name, node_a, iface_a, ip_a, node_b, "
            "iface_b, ip_b, bridge_name) VALUES (?,?,?,?,?,?,?,?)",
            ("lab", "edge", "port0", None, "core", "port1", None, None))
    db._conn.commit()
    db.close()

    rp = _Path(range_dir) / "lab"
    rp.mkdir(parents=True, exist_ok=True)
    sock = rp / "libvirt-sock"
    sock.write_text("")
    (rp / "range.json").write_text(_json.dumps({
        "pid": 4242, "netns_name": "rangectl-lab",
        "veth_host": "mgh0001", "veth_ns": "mgp0001",
        "host_ip": "10.255.1.254", "subnet": "10.255.1.0/24",
        "libvirt_socket": str(sock),
    }))
    monkeypatch.setattr(
        "rangectl.libvirt_backend.LibvirtBackend.reconnect_vm",
        lambda self, *a, **k: None)
    return db_file, range_dir


def test_connect_rebuilds_l2_nodes(tmp_path, monkeypatch):
    db_file, range_dir = _seed_l2_range(tmp_path, monkeypatch)
    rng = Range.connect("lab", db_path=db_file, range_dir=range_dir)
    core = rng.topology._nodes["core"]
    assert isinstance(core, L2Node)
    assert core.os_type is OSType.HUB
    assert core.bridge_name == "hub-core"
    assert "core" not in rng._nodes  # no LiveNode (nothing to SSH/power)
    assert "a" in rng._nodes


def test_connect_rebuilds_vm_l2_link_endpoints(tmp_path, monkeypatch):
    db_file, range_dir = _seed_l2_range(tmp_path, monkeypatch)
    rng = Range.connect("lab", db_path=db_file, range_dir=range_dir)
    link = rng.link("a", "core")
    ep_a, ep_core = link._endpoints
    assert ep_a.vm_id == "lab-a"
    assert ep_a.mac == _mac_for("lab", "a", "eth1")
    assert ep_a.hub is True            # attached to a hub bridge
    assert ep_core.is_l2 and ep_core.resolve(None) is None
    with pytest.raises(ValueError, match="L2"):
        link.impair(latency="10ms", outbound="core")


def test_connect_rebuilds_l2_l2_veth_endpoints(tmp_path, monkeypatch):
    db_file, range_dir = _seed_l2_range(tmp_path, monkeypatch, with_l2_l2=True)
    rng = Range.connect("lab", db_path=db_file, range_dir=range_dir)
    link = rng.link("edge", "core")
    va, vb = _l2_veth_names("lab", 1)
    assert link._veth_pair == (va, vb)
    ep_edge, ep_core = link._endpoints
    assert ep_edge.dev == va and ep_edge.bridge == "sw-edge"
    assert ep_core.dev == vb and ep_core.bridge == "hub-core"
    assert ep_core.hub is True and ep_edge.hub is False


# --- CLI (D8) -------------------------------------------------------------------

def test_cli_status_renders_l2_without_power_query(monkeypatch, capsys):
    from rangectl import cli

    class _DB:
        def list_nodes(self, name):
            return [
                {"name": "a", "image": "ubuntu", "os_type": "linux",
                 "vcpu": 1, "memory_mb": 1024, "mgmt_ip": "10.255.1.1",
                 "state": "running"},
                {"name": "core", "image": "", "os_type": "switch",
                 "vcpu": 0, "memory_mb": 0, "mgmt_ip": None,
                 "state": "running"},
            ]

    class _Node:
        status = "running"

    class _Rng:
        name = "lab"
        _db = _DB()
        queried: list[str] = []

        def __getitem__(self, name):
            _Rng.queried.append(name)
            return _Node()

    monkeypatch.setattr(cli.Range, "connect", staticmethod(lambda n: _Rng()))
    assert cli.main(["status", "lab"]) == 0
    out = capsys.readouterr().out
    assert "switch" in out
    assert "core" in out
    assert "core" not in _Rng.queried  # no power/SSH query for L2 nodes


def test_cli_net_lists_l2_bridges(monkeypatch, capsys):
    import subprocess

    from rangectl import cli

    class _DB:
        def list_nodes(self, name):
            return [{"name": "core", "os_type": "hub", "mgmt_ip": None}]

        def list_bridges(self, name):
            return [
                {"name": "mgmt-br", "bridge_type": "mgmt", "subnet": None},
                {"name": "hub-core", "bridge_type": "hub", "subnet": None},
            ]

    class _Rng:
        name = "lab"
        _db = _DB()

    monkeypatch.setattr(cli.Range, "connect", staticmethod(lambda n: _Rng()))
    monkeypatch.setattr(cli, "_require_range_info", lambda n: {
        "netns_name": "rangectl-lab", "subnet": "10.255.1.0/24",
        "veth_host": "mgh1", "veth_ns": "mgp1"})

    def fake_run(cmd, **kw):
        class R:
            returncode = 0
            stdout = ("3: tapX@if2: <UP> mtu 1500 master hub-core state "
                      "forwarding priority 32\n")
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert cli.main(["net", "lab"]) == 0
    out = capsys.readouterr().out
    assert "hub-core" in out
    assert "(hub)" in out
    assert "tapX" in out
