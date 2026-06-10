"""Unit tests for Phase 25 — native VLAN support (802.1Q switches).

Spec: scratch/issues/20260609-4-phase25-vlan-support.md. A ``vlan_aware=True``
switch gets kernel bridge VLAN filtering; ports are configured as access
(PVID untagged) or trunk (tagged, multi-VID, optional native). Hubs are never
vlan-aware. Unconfigured ports keep the kernel default PVID 1.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from rangectl import Topology
from rangectl.engine import Engine, _l2_veth_names
from rangectl.libvirt_backend import LibvirtBackend
from rangectl.topology import L2Node, Range
from rangectl.types import OSType, PortSpec


# --- SDK surface: switch(vlan_aware=) / hub rejection ------------------------

def test_switch_vlan_aware_flag():
    t = Topology("lab")
    sw = t.switch("core", vlan_aware=True)
    assert isinstance(sw, L2Node)
    assert sw.vlan_aware is True
    assert sw.bridge_name == "sw-core"


def test_switch_defaults_not_vlan_aware():
    t = Topology("lab")
    assert t.switch("core").vlan_aware is False


def test_hub_vlan_aware_rejected():
    t = Topology("lab")
    with pytest.raises(ValueError, match="vlan"):
        t.hub("mon", vlan_aware=True)


def test_range_lifecycle_switch_vlan_aware_and_hub_rejection():
    class Lab(Range):
        name = "vlab"

        def verify(self):
            pass

    lab = Lab()
    sw = lab.switch("core", vlan_aware=True)
    assert sw.vlan_aware is True
    with pytest.raises(ValueError, match="vlan"):
        lab.hub("mon", vlan_aware=True)


# --- port spec API: access() / trunk() ---------------------------------------

def test_access_returns_port_spec_with_config():
    t = Topology("lab")
    sw = t.switch("core", vlan_aware=True)
    p = sw.port0.access(10)
    assert isinstance(p, PortSpec)
    assert p is sw.port0  # same lazily-created spec, usable in link()
    assert p.vlan == {"mode": "access", "vids": [10], "native": None}


def test_trunk_with_native_config():
    t = Topology("lab")
    sw = t.switch("core", vlan_aware=True)
    p = sw.port2.trunk(10, 20, native=99)
    assert p.vlan == {"mode": "trunk", "vids": [10, 20], "native": 99}


def test_trunk_without_native():
    t = Topology("lab")
    sw = t.switch("core", vlan_aware=True)
    assert sw.port1.trunk(30).vlan == {"mode": "trunk", "vids": [30],
                                       "native": None}


def test_port_is_access_xor_trunk():
    t = Topology("lab")
    sw = t.switch("core", vlan_aware=True)
    sw.port0.access(10)
    with pytest.raises(ValueError, match="access"):
        sw.port0.trunk(10, 20)
    sw.port1.trunk(10)
    with pytest.raises(ValueError, match="trunk"):
        sw.port1.access(10)
    sw.port2.access(10)
    with pytest.raises(ValueError):
        sw.port2.access(20)


def test_vid_bounds():
    t = Topology("lab")
    sw = t.switch("core", vlan_aware=True)
    with pytest.raises(ValueError, match="VID"):
        sw.port0.access(0)
    with pytest.raises(ValueError, match="VID"):
        sw.port1.access(4095)
    with pytest.raises(ValueError, match="VID"):
        sw.port2.trunk(10, -3)
    sw.port3.access(1)
    sw.port4.trunk(4094)


def test_native_vid_validated():
    t = Topology("lab")
    sw = t.switch("core", vlan_aware=True)
    with pytest.raises(ValueError, match="VID"):
        sw.port0.trunk(10, native=4095)


def test_trunk_requires_at_least_one_vid():
    t = Topology("lab")
    sw = t.switch("core", vlan_aware=True)
    with pytest.raises(ValueError, match="VID"):
        sw.port0.trunk()


def test_access_on_plain_switch_rejected():
    t = Topology("lab")
    sw = t.switch("core")
    with pytest.raises(ValueError, match="vlan_aware"):
        sw.port0.access(10)
    with pytest.raises(ValueError, match="vlan_aware"):
        sw.port1.trunk(10, 20)


def test_access_on_hub_port_rejected():
    t = Topology("lab")
    hub = t.hub("mon")
    with pytest.raises(ValueError, match="vlan_aware"):
        hub.port0.access(10)


# --- engine: vlan_filtering + port programming --------------------------------

def _vlan_topo(name="vlab", native=None):
    """web/db on access(10)/access(20), router on trunk(10, 20[, native])."""
    t = Topology(name)
    web = t.node("web", image="ubuntu")
    dbn = t.node("db", image="ubuntu")
    rtr = t.node("router", image="ubuntu")
    sw = t.switch("core", vlan_aware=True)
    t.link(web.eth1["10.0.10.2/24"], sw.port0.access(10))
    t.link(dbn.eth1["10.0.20.2/24"], sw.port1.access(20))
    t.link(rtr.eth1["10.0.99.1/24"], sw.port2.trunk(10, 20, native=native))
    return t


def test_deploy_enables_vlan_filtering_after_bridge_create(backend, db):
    Engine(backend, db).deploy(_vlan_topo())
    names = [c[0] for c in backend.calls]
    vf = backend.calls_of("set_vlan_filtering")
    assert vf == [(("sw-core",), {"enabled": True})]
    create_idx = [i for i, c in enumerate(backend.calls)
                  if c[0] == "create_bridge" and c[1][0] == "sw-core"][0]
    assert names.index("set_vlan_filtering") > create_idx


def test_plain_switch_gets_no_vlan_filtering(backend, db):
    t = Topology("lab")
    a = t.node("a", image="ubuntu")
    sw = t.switch("core")
    t.link(a.eth1["10.0.1.1/24"], sw.port0)
    Engine(backend, db).deploy(t)
    assert backend.calls_of("set_vlan_filtering") == []


def _tap(rng, node_name: str) -> str:
    """MockBackend names a VM's TAP tap-<vm_id>; node->vm_id assignment is
    boot-order dependent (parallel boot), so derive it from the live node."""
    return f"tap-{rng[node_name]._vm_id}"


def test_access_and_trunk_ports_programmed_on_taps(backend, db):
    rng = Engine(backend, db).deploy(_vlan_topo())
    assert backend.port_vlans[_tap(rng, "web")] == {
        "mode": "access", "vids": [10], "native": None}
    assert backend.port_vlans[_tap(rng, "db")] == {
        "mode": "access", "vids": [20], "native": None}
    assert backend.port_vlans[_tap(rng, "router")] == {
        "mode": "trunk", "vids": [10, 20], "native": None}


def test_trunk_native_programmed(backend, db):
    rng = Engine(backend, db).deploy(_vlan_topo(native=99))
    assert backend.port_vlans[_tap(rng, "router")]["native"] == 99


def test_unconfigured_port_left_at_default_pvid(backend, db):
    t = _vlan_topo()
    extra = t.node("extra", image="ubuntu")
    t.link(extra.eth1["10.0.30.2/24"], t._nodes["core"].port3)
    rng = Engine(backend, db).deploy(t)
    assert _tap(rng, "extra") not in backend.port_vlans


def test_l2_l2_veth_ends_programmed(backend, db):
    t = Topology("lab")
    s1 = t.switch("s1", vlan_aware=True)
    s2 = t.switch("s2", vlan_aware=True)
    a = t.node("a", image="ubuntu")
    t.link(a.eth1["10.0.10.2/24"], s1.port0.access(10))
    t.link(s1.port1.trunk(10, 20), s2.port0.trunk(10, 20))
    Engine(backend, db).deploy(t)
    va, vb = _l2_veth_names("lab", 1)
    assert backend.port_vlans[va] == {"mode": "trunk", "vids": [10, 20],
                                      "native": None}
    assert backend.port_vlans[vb] == {"mode": "trunk", "vids": [10, 20],
                                      "native": None}


# --- persistence ---------------------------------------------------------------

def test_bridges_row_persists_vlan_aware(backend, db):
    Engine(backend, db).deploy(_vlan_topo())
    row = [b for b in db.list_bridges("vlab") if b["name"] == "sw-core"][0]
    assert row["vlan_aware"] == 1


def test_plain_l2_bridges_persist_vlan_aware_zero(backend, db):
    t = Topology("lab")
    a = t.node("a", image="ubuntu")
    sw = t.switch("core")
    t.link(a.eth1["10.0.1.1/24"], sw.port0)
    Engine(backend, db).deploy(t)
    row = [b for b in db.list_bridges("lab") if b["name"] == "sw-core"][0]
    assert row["vlan_aware"] == 0


def test_links_rows_persist_port_vlan_json(backend, db):
    Engine(backend, db).deploy(_vlan_topo())
    links = db.list_links("vlab")
    by_a = {lk["node_a"]: lk for lk in links}
    assert json.loads(by_a["web"]["vlan_b"]) == {
        "mode": "access", "vids": [10], "native": None}
    assert by_a["web"]["vlan_a"] is None  # the VM side has no port config
    assert json.loads(by_a["router"]["vlan_b"]) == {
        "mode": "trunk", "vids": [10, 20], "native": None}


# --- link down/up re-apply -------------------------------------------------------

def test_link_down_up_reapplies_vlan_filtering_and_port_vlans(backend, db):
    t = _vlan_topo()
    rng = Engine(backend, db).deploy(t)
    link = rng.link("web", "core")
    link.down()
    backend.calls.clear()
    backend.port_vlans.clear()
    link.up()
    assert (("sw-core",), {"enabled": True}) in \
        backend.calls_of("set_vlan_filtering")
    assert backend.port_vlans[_tap(rng, "web")] == {
        "mode": "access", "vids": [10], "native": None}


def test_l2_l2_link_up_reapplies_veth_vlans_without_refilter(backend, db):
    t = Topology("lab")
    s1 = t.switch("s1", vlan_aware=True)
    s2 = t.switch("s2", vlan_aware=True)
    a = t.node("a", image="ubuntu")
    t.link(a.eth1["10.0.10.2/24"], s1.port0.access(10))
    t.link(s1.port1.trunk(10), s2.port0.trunk(10))
    rng = Engine(backend, db).deploy(t)
    link = rng.link("s1", "s2")
    link.down()
    backend.calls.clear()
    backend.port_vlans.clear()
    link.up()
    va, vb = _l2_veth_names("lab", 1)
    assert backend.port_vlans[va]["vids"] == [10]
    assert backend.port_vlans[vb]["vids"] == [10]
    # Bridges were not deleted (veth link) — no vlan_filtering re-toggle.
    assert backend.calls_of("set_vlan_filtering") == []


# --- Range.connect rebuild --------------------------------------------------------

def _seed_vlan_range(tmp_path, monkeypatch):
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
    db.save_topology("vlab", "running", "10.255.1.0/24", "mgmt-br")
    db.save_node(topology_name="vlab", name="web", image="ubuntu", vcpu=1,
                 memory_mb=1024, os_type="linux", state="running",
                 mgmt_ip="10.255.1.1", vm_id="vlab-web")
    db.save_node(topology_name="vlab", name="core", image="", vcpu=0,
                 memory_mb=0, os_type="switch", state="running")
    db._conn.execute(
        "INSERT INTO bridges (topology_name, name, bridge_type, vlan_aware) "
        "VALUES (?,?,?,?)", ("vlab", "sw-core", "switch", 1))
    db._conn.execute(
        "INSERT INTO links (topology_name, node_a, iface_a, ip_a, node_b, "
        "iface_b, ip_b, bridge_name, vlan_a, vlan_b) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("vlab", "web", "eth1", "10.0.10.2", "core", "port0", None, "sw-core",
         None, _json.dumps({"mode": "access", "vids": [10], "native": None})))
    db._conn.commit()
    db.close()

    rp = _Path(range_dir) / "vlab"
    rp.mkdir(parents=True, exist_ok=True)
    sock = rp / "libvirt-sock"
    sock.write_text("")
    (rp / "range.json").write_text(_json.dumps({
        "pid": 4242, "netns_name": "rangectl-vlab",
        "veth_host": "mgh0001", "veth_ns": "mgp0001",
        "host_ip": "10.255.1.254", "subnet": "10.255.1.0/24",
        "libvirt_socket": str(sock),
    }))
    monkeypatch.setattr(
        "rangectl.libvirt_backend.LibvirtBackend.reconnect_vm",
        lambda self, *a, **k: None)
    return db_file, range_dir


def test_connect_rebuilds_vlan_aware_switch(tmp_path, monkeypatch):
    db_file, range_dir = _seed_vlan_range(tmp_path, monkeypatch)
    rng = Range.connect("vlab", db_path=db_file, range_dir=range_dir)
    core = rng.topology._nodes["core"]
    assert isinstance(core, L2Node)
    assert core.vlan_aware is True


def test_connect_rebuilds_vlan_endpoints(tmp_path, monkeypatch):
    db_file, range_dir = _seed_vlan_range(tmp_path, monkeypatch)
    rng = Range.connect("vlab", db_path=db_file, range_dir=range_dir)
    link = rng.link("web", "core")
    ep_web, ep_core = link._endpoints
    # The VM endpoint carries the port's VLAN config (its TAP is the port).
    assert ep_web.vlan == {"mode": "access", "vids": [10], "native": None}
    # The L2 endpoint marks the bridge as vlan-aware so up() re-filters.
    assert ep_core.bridge_vlan_aware is True
    assert ep_web.bridge_vlan_aware is False


# --- LibvirtBackend command generation ---------------------------------------------

def _ok(stdout: str = "", stderr: str = "", rc: int = 0):
    m = MagicMock()
    m.returncode = rc
    m.stdout = stdout
    m.stderr = stderr
    return m


def _cmds(run):
    return [c.args[0] for c in run.call_args_list]


NS = ["ip", "netns", "exec", "rangectl-lab1"]


def test_set_vlan_filtering_command():
    be = LibvirtBackend(netns_name="rangectl-lab1")
    with patch("rangectl.libvirt_backend._run") as run:
        run.return_value = _ok()
        be.set_vlan_filtering("sw-core")
    assert NS + ["ip", "link", "set", "sw-core", "type", "bridge",
                 "vlan_filtering", "1"] in _cmds(run)


def test_set_port_vlans_access_removes_default_vid1():
    be = LibvirtBackend(netns_name="rangectl-lab1")
    with patch("rangectl.libvirt_backend._run") as run:
        run.return_value = _ok()
        be.set_port_vlans("vnet3", mode="access", vids=[10])
    cmds = _cmds(run)
    assert NS + ["bridge", "vlan", "del", "dev", "vnet3", "vid", "1"] in cmds
    assert NS + ["bridge", "vlan", "add", "dev", "vnet3", "vid", "10",
                 "pvid", "untagged"] in cmds
    # del runs before add so the port never carries both VLANs.
    assert cmds.index(NS + ["bridge", "vlan", "del", "dev", "vnet3",
                            "vid", "1"]) < \
        cmds.index(NS + ["bridge", "vlan", "add", "dev", "vnet3", "vid", "10",
                         "pvid", "untagged"])


def test_set_port_vlans_access_vid1_keeps_default():
    be = LibvirtBackend(netns_name="rangectl-lab1")
    with patch("rangectl.libvirt_backend._run") as run:
        run.return_value = _ok()
        be.set_port_vlans("vnet3", mode="access", vids=[1])
    cmds = _cmds(run)
    assert not any("del" in c for c in cmds)
    assert NS + ["bridge", "vlan", "add", "dev", "vnet3", "vid", "1",
                 "pvid", "untagged"] in cmds


def test_set_port_vlans_trunk_with_native():
    be = LibvirtBackend(netns_name="rangectl-lab1")
    with patch("rangectl.libvirt_backend._run") as run:
        run.return_value = _ok()
        be.set_port_vlans("vnet3", mode="trunk", vids=[10, 20], native=99)
    cmds = _cmds(run)
    assert NS + ["bridge", "vlan", "del", "dev", "vnet3", "vid", "1"] in cmds
    assert NS + ["bridge", "vlan", "add", "dev", "vnet3", "vid", "10"] in cmds
    assert NS + ["bridge", "vlan", "add", "dev", "vnet3", "vid", "20"] in cmds
    assert NS + ["bridge", "vlan", "add", "dev", "vnet3", "vid", "99",
                 "pvid", "untagged"] in cmds
    # No 6.1-only subcommands — must work on iproute2 5.15 (EC2).
    assert not any("flush" in c for c in cmds)


def test_set_port_vlans_bad_mode_raises():
    be = LibvirtBackend(netns_name="rangectl-lab1")
    with pytest.raises(ValueError):
        be.set_port_vlans("vnet3", mode="hybrid", vids=[10])


# --- YAML round-trip -----------------------------------------------------------------

def test_yaml_roundtrip_preserves_vlan_config(tmp_path):
    t = _vlan_topo(native=99)
    path = str(tmp_path / "topo.yaml")
    t.export(path)
    restored = Topology.from_yaml(path)
    core = restored._nodes["core"]
    assert core.vlan_aware is True
    assert restored._nodes["core"]._interfaces["port0"].vlan == {
        "mode": "access", "vids": [10], "native": None}
    assert restored._nodes["core"]._interfaces["port2"].vlan == {
        "mode": "trunk", "vids": [10, 20], "native": 99}


def test_yaml_roundtrip_plain_switch_stays_plain(tmp_path):
    t = Topology("lab")
    a = t.node("a", image="ubuntu")
    sw = t.switch("core")
    t.link(a.eth1["10.0.1.1/24"], sw.port0)
    path = str(tmp_path / "topo.yaml")
    t.export(path)
    restored = Topology.from_yaml(path)
    assert restored._nodes["core"].vlan_aware is False


# --- CLI net rendering ------------------------------------------------------------------

def test_cli_net_renders_vlan_table(monkeypatch, capsys):
    import subprocess

    from rangectl import cli

    class _DB:
        def list_nodes(self, name):
            return [
                {"name": "web", "os_type": "linux", "mgmt_ip": "10.255.1.1"},
                {"name": "core", "os_type": "switch", "mgmt_ip": None},
            ]

        def list_bridges(self, name):
            return [
                {"name": "mgmt-br", "bridge_type": "mgmt", "subnet": None,
                 "vlan_aware": 0},
                {"name": "sw-core", "bridge_type": "switch", "subnet": None,
                 "vlan_aware": 1},
            ]

        def list_links(self, name):
            return [
                {"node_a": "web", "iface_a": "eth1", "ip_a": "10.0.10.2",
                 "node_b": "core", "iface_b": "port0", "ip_b": None,
                 "bridge_name": "sw-core", "vlan_a": None,
                 "vlan_b": json.dumps(
                     {"mode": "access", "vids": [10], "native": None})},
                {"node_a": "router", "iface_a": "eth1", "ip_a": None,
                 "node_b": "core", "iface_b": "port2", "ip_b": None,
                 "bridge_name": "sw-core", "vlan_a": None,
                 "vlan_b": json.dumps(
                     {"mode": "trunk", "vids": [10, 20], "native": 99})},
            ]

    class _Rng:
        name = "vlab"
        _db = _DB()

    monkeypatch.setattr(cli.Range, "connect", staticmethod(lambda n: _Rng()))
    monkeypatch.setattr(cli, "_require_range_info", lambda n: {
        "netns_name": "rangectl-vlab", "subnet": "10.255.1.0/24",
        "veth_host": "mgh1", "veth_ns": "mgp1"})

    def fake_run(cmd, **kw):
        class R:
            returncode = 0
            stdout = ""
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert cli.main(["net", "vlab"]) == 0
    out = capsys.readouterr().out
    assert "vlan-aware" in out
    assert "port0" in out and "access 10" in out
    assert "port2" in out and "trunk 10,20" in out and "native 99" in out
    assert "pvid 1" in out.lower() or "PVID 1" in out  # default-port note


def test_cli_net_plain_switch_no_vlan_table(monkeypatch, capsys):
    import subprocess

    from rangectl import cli

    class _DB:
        def list_nodes(self, name):
            return [{"name": "core", "os_type": "switch", "mgmt_ip": None}]

        def list_bridges(self, name):
            return [{"name": "sw-core", "bridge_type": "switch",
                     "subnet": None, "vlan_aware": 0}]

        def list_links(self, name):
            return []

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
            stdout = ""
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert cli.main(["net", "lab"]) == 0
    out = capsys.readouterr().out
    assert "vlan-aware" not in out


# --- StateDB migration -------------------------------------------------------------------

def test_existing_db_gains_vlan_columns(tmp_path):
    """A DB created before Phase 25 (no vlan columns) is migrated on open."""
    import sqlite3

    from rangectl.state import StateDB
    db_file = str(tmp_path / "old.db")
    conn = sqlite3.connect(db_file)
    conn.executescript("""
        CREATE TABLE bridges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topology_name TEXT NOT NULL, name TEXT NOT NULL,
            subnet TEXT, bridge_type TEXT NOT NULL,
            UNIQUE(topology_name, name));
        CREATE TABLE links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topology_name TEXT NOT NULL,
            node_a TEXT NOT NULL, iface_a TEXT NOT NULL, ip_a TEXT,
            node_b TEXT NOT NULL, iface_b TEXT NOT NULL, ip_b TEXT,
            bridge_name TEXT, is_up BOOLEAN DEFAULT 1);
    """)
    conn.execute("INSERT INTO bridges (topology_name, name, bridge_type) "
                 "VALUES ('old', 'sw-x', 'switch')")
    conn.commit()
    conn.close()

    db = StateDB(db_file)
    try:
        rows = db.list_bridges("old")
        assert rows[0]["vlan_aware"] == 0
        db._conn.execute(
            "INSERT INTO links (topology_name, node_a, iface_a, node_b, "
            "iface_b, vlan_b) VALUES ('old','a','eth1','sw','port0','{}')")
        db._conn.commit()
        assert db.list_links("old")[0]["vlan_b"] == "{}"
    finally:
        db.close()
