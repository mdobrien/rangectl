"""Unit tests for Phase 13 — persistent ranges.

Covers Range.connect()/list()/cleanup(), LibvirtBackend.reconnect_vm(), and the
persistent (non-destroying) context-manager behaviour. State is set up by hand
(StateDB rows + a range.json file) so these run with no infrastructure; liveness
probes are monkeypatched.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from rangectl import topology as topo_mod
from rangectl.container_backend import ContainerBackend
from rangectl.libvirt_backend import LibvirtBackend
from rangectl.state import StateDB
from rangectl.topology import Range, Topology
from rangectl.types import RangeNotRunning


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A file-backed StateDB + range dir, with liveness probes forced alive.

    Returns a small namespace with helpers to seed topologies/nodes/range.json.
    """
    db_file = str(tmp_path / "rangectl.db")
    range_dir = str(tmp_path / "ranges")

    # Default: everything alive, nothing frozen. Individual tests override.
    monkeypatch.setattr(topo_mod, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(topo_mod, "_netns_exists", lambda ns: True)
    monkeypatch.setattr(topo_mod.cgroup, "is_frozen", lambda name: False)

    def seed(name="lab", nodes=None, status="running",
             pid=4242, write_json=True, subnet="192.168.100.0/24"):
        nodes = nodes or [
            {"name": "a", "image": "ubuntu", "os_type": "linux"},
            {"name": "b", "image": "ubuntu", "os_type": "linux"},
        ]
        db = StateDB(db_file)
        db.save_topology(name, status, subnet, "mgmt-br")
        for i, n in enumerate(nodes):
            db.save_node(
                topology_name=name, name=n["name"], image=n["image"],
                vcpu=n.get("vcpu", 1), memory_mb=n.get("memory", 1024),
                os_type=n["os_type"], state="running",
                mgmt_ip=n.get("mgmt_ip", f"192.168.100.{i + 1}"),
                vm_id=f"{name}-{n['name']}",
            )
        db.close()
        rp = Path(range_dir) / name
        rp.mkdir(parents=True, exist_ok=True)
        sock = rp / "libvirt-sock"
        sock.write_text("")
        if write_json:
            (rp / "range.json").write_text(json.dumps({
                "pid": pid,
                "netns_name": f"rangectl-{name}",
                "veth_host": "mgh0001",
                "veth_ns": "mgp0001",
                "host_ip": "192.168.100.254",
                "subnet": subnet,
                "libvirt_socket": str(sock),
            }))
        return name

    return type("S", (), {
        "db_file": db_file,
        "range_dir": range_dir,
        "seed": staticmethod(seed),
    })


# ---------- connect ----------

def test_connect_returns_range(state):
    state.seed("lab")
    rng = Range.connect("lab", db_path=state.db_file, range_dir=state.range_dir)
    assert isinstance(rng, Range)
    assert rng.topology.name == "lab"
    assert set(rng._nodes) == {"a", "b"}


def test_connect_not_found(state):
    with pytest.raises(RangeNotRunning):
        Range.connect("ghost", db_path=state.db_file, range_dir=state.range_dir)


def test_connect_destroyed_status(state):
    state.seed("dead", status="destroyed")
    with pytest.raises(RangeNotRunning):
        Range.connect("dead", db_path=state.db_file, range_dir=state.range_dir)


def test_connect_missing_range_json(state):
    state.seed("nojson", write_json=False)
    with pytest.raises(RangeNotRunning):
        Range.connect("nojson", db_path=state.db_file, range_dir=state.range_dir)


def test_connect_stale_pid(state, monkeypatch):
    state.seed("stale")
    monkeypatch.setattr(topo_mod, "_pid_alive", lambda pid: False)
    with pytest.raises(RangeNotRunning):
        Range.connect("stale", db_path=state.db_file, range_dir=state.range_dir)


def test_connect_missing_netns(state, monkeypatch):
    state.seed("nons")
    monkeypatch.setattr(topo_mod, "_netns_exists", lambda ns: False)
    with pytest.raises(RangeNotRunning):
        Range.connect("nons", db_path=state.db_file, range_dir=state.range_dir)


def test_connect_rebuilds_livenodes(state):
    state.seed("lab", nodes=[
        {"name": "router", "image": "vyos-1.4", "os_type": "vyos",
         "mgmt_ip": "192.168.100.1"},
        {"name": "target", "image": "ubuntu-22.04", "os_type": "linux",
         "mgmt_ip": "192.168.100.2"},
    ])
    rng = Range.connect("lab", db_path=state.db_file, range_dir=state.range_dir)

    router = rng["router"]
    assert router._vm_id == "lab-router"
    assert router.mgmt_ip == "192.168.100.1"
    assert isinstance(router._backend, LibvirtBackend)
    # SSH state must be populated for exec() to work cross-process.
    assert router._backend._vm_mgmt_ip["lab-router"] == "192.168.100.1"
    assert router._backend._vm_ssh_user["lab-router"] == "vyos"
    assert rng["target"]._backend._vm_ssh_user["lab-target"] == "ubuntu"


def test_connect_container_uses_container_backend(state):
    state.seed("mix", nodes=[
        {"name": "vm", "image": "ubuntu", "os_type": "linux"},
        {"name": "ctr", "image": "nginx:latest", "os_type": "container"},
    ])
    rng = Range.connect("mix", db_path=state.db_file, range_dir=state.range_dir)
    assert isinstance(rng["vm"]._backend, LibvirtBackend)
    assert isinstance(rng["ctr"]._backend, ContainerBackend)


def test_connect_is_persistent(state):
    state.seed("lab")
    rng = Range.connect("lab", db_path=state.db_file, range_dir=state.range_dir)
    assert rng._persistent is True


def test_connect_does_not_destroy_on_exit(state):
    state.seed("lab")
    rng = Range.connect("lab", db_path=state.db_file, range_dir=state.range_dir)
    called = []
    rng._engine.destroy = lambda topo: called.append(topo)
    with rng:
        pass
    assert called == []


# ---------- ephemeral context manager (regression) ----------

def test_ephemeral_context_destroys():
    rng = Range(Topology("eph"))
    called = []
    rng._engine = type("E", (), {"destroy": lambda self, t: called.append(t)})()
    with rng:
        pass
    assert len(called) == 1


def test_range_destroy_method_calls_engine():
    rng = Range(Topology("d"))
    called = []
    rng._engine = type("E", (), {"destroy": lambda self, t: called.append(t)})()
    rng.destroy()
    assert len(called) == 1


# ---------- list ----------

def test_list_running(state):
    state.seed("one", subnet="192.168.100.0/24")
    state.seed("two", subnet="192.168.101.0/24")
    rows = Range.list(db_path=state.db_file, range_dir=state.range_dir)
    by_name = {r["name"]: r for r in rows}
    assert set(by_name) == {"one", "two"}
    assert by_name["one"]["status"] == "running"
    assert by_name["one"]["node_count"] == 2
    assert by_name["one"]["mgmt_subnet"] == "192.168.100.0/24"


def test_list_excludes_destroyed(state):
    state.seed("live")
    state.seed("gone", status="destroyed")
    names = {r["name"] for r in Range.list(db_path=state.db_file,
                                           range_dir=state.range_dir)}
    assert names == {"live"}


def test_list_with_orphaned(state, monkeypatch):
    state.seed("alive", pid=100)
    state.seed("orphan", pid=200)
    # Only pid 100 is alive; pid 200's process is gone.
    monkeypatch.setattr(topo_mod, "_pid_alive", lambda pid: pid == 100)
    rows = {r["name"]: r["status"] for r in
            Range.list(db_path=state.db_file, range_dir=state.range_dir)}
    assert rows["alive"] == "running"
    assert rows["orphan"] == "orphaned"


def test_list_frozen(state, monkeypatch):
    state.seed("frz")
    monkeypatch.setattr(topo_mod.cgroup, "is_frozen", lambda name: True)
    rows = {r["name"]: r["status"] for r in
            Range.list(db_path=state.db_file, range_dir=state.range_dir)}
    assert rows["frz"] == "frozen"


def test_list_orphan_when_range_json_missing(state):
    state.seed("nojson", write_json=False)
    rows = {r["name"]: r["status"] for r in
            Range.list(db_path=state.db_file, range_dir=state.range_dir)}
    assert rows["nojson"] == "orphaned"


# ---------- cleanup ----------

def test_cleanup_removes_state(state, monkeypatch):
    state.seed("orphan")
    destroyed = []
    monkeypatch.setattr(topo_mod.supervisor, "destroy_range",
                        lambda name, range_dir=None: destroyed.append(name))
    monkeypatch.setattr(topo_mod.cgroup, "destroy_cgroup",
                        lambda name: destroyed.append(("cg", name)))

    Range.cleanup("orphan", db_path=state.db_file, range_dir=state.range_dir)

    assert "orphan" in destroyed
    db = StateDB(state.db_file)
    try:
        assert db.get_topology("orphan") is None
        cur = db._conn.execute("SELECT subnet FROM mgmt_subnets WHERE topology_name=?",
                               ("orphan",))
        assert cur.fetchone() is None
    finally:
        db.close()


# ---------- LibvirtBackend.reconnect_vm ----------

def test_backend_reconnect_populates_ssh_state():
    be = LibvirtBackend()
    be.reconnect_vm("topo-node", "topo", "10.0.0.5",
                    ssh_user="vyos", ssh_password="vyos")
    assert be._vm_mgmt_ip["topo-node"] == "10.0.0.5"
    assert be._vm_ssh_user["topo-node"] == "vyos"
    assert be._vm_topo["topo-node"] == "topo"
    assert be._vm_ssh_password["topo-node"] == "vyos"


def test_backend_reconnect_defaults():
    be = LibvirtBackend()
    be.reconnect_vm("t-n", "t", "10.0.0.6")
    assert be._vm_ssh_user["t-n"] == "ubuntu"
    assert be._vm_ssh_password["t-n"] is None


# ---------- StateDB.list_nodes ----------

def test_list_nodes(state):
    state.seed("lab")
    db = StateDB(state.db_file)
    try:
        rows = db.list_nodes("lab")
    finally:
        db.close()
    assert {r["name"] for r in rows} == {"a", "b"}
    assert all(r["vm_id"] == f"lab-{r['name']}" for r in rows)
