"""Unit tests for Phase 11 — namespace-aware Engine.deploy()/destroy().

When ``use_namespaces=True`` the engine stands up a per-range netns + libvirtd
via ``supervisor.create_range`` before deploying nodes, drives VM/bridge ops
through a per-range LibvirtBackend bound to the range's socket + netns, uses
clean bridge names (mgmt-br, data-0, …), and tears everything down with one
``supervisor.destroy_range`` call.

All external infrastructure (supervisor, LibvirtBackend, ContainerBackend,
cgroup) is mocked; only the engine's orchestration logic is exercised.
"""
from __future__ import annotations

import pytest

from rangectl import Topology
from rangectl import engine as engine_mod
from rangectl.cgroup import Resources
from rangectl.engine import Engine
from rangectl.supervisor import RangeInfo
from tests.unit.conftest import MockBackend


@pytest.fixture
def ns(monkeypatch):
    """Patch supervisor + per-range backends so ns-mode deploy runs in-memory.

    Returns an object exposing the mock call recorders and the per-range
    MockBackend the engine will drive for VM/bridge ops.
    """
    info = RangeInfo(
        name="r",
        pid=4242,
        netns_name="rangectl-r",
        libvirt_socket="/ranges/r/run-libvirt/libvirt-sock",
        mgmt_subnet="192.168.100.0/24",
        veth_host="mgh1234",
        veth_ns="mgp1234",
    )

    created: list[tuple] = []

    def fake_create_range(name, mgmt_subnet, range_dir="/ranges", cgroup_path=None):
        created.append((name, mgmt_subnet, cgroup_path))
        return info

    destroyed: list[str] = []

    def fake_destroy_range(name, range_dir="/ranges"):
        destroyed.append(name)

    range_backend = MockBackend()

    monkeypatch.setattr(engine_mod.supervisor, "create_range", fake_create_range)
    monkeypatch.setattr(engine_mod.supervisor, "destroy_range", fake_destroy_range)
    monkeypatch.setattr(Engine, "_make_range_backend",
                        lambda self, info: range_backend)

    cgroup_calls: list[tuple] = []
    monkeypatch.setattr(engine_mod.cgroup, "create_cgroup",
                        lambda name, res: cgroup_calls.append(("create", name, res))
                        or f"/sys/fs/cgroup/rangectl-{name}")
    monkeypatch.setattr(engine_mod.cgroup, "write_pid",
                        lambda path, pid: cgroup_calls.append(("write_pid", path, pid)))
    monkeypatch.setattr(engine_mod.cgroup, "destroy_cgroup",
                        lambda name: cgroup_calls.append(("destroy", name)))

    internet_calls: list[tuple] = []
    monkeypatch.setattr(engine_mod.internet, "enable_internet",
                        lambda name, subnet, veth: internet_calls.append(
                            ("enable", name, subnet, veth)))
    monkeypatch.setattr(engine_mod.internet, "disable_internet",
                        lambda name, subnet, veth: internet_calls.append(
                            ("disable", name, subnet, veth)))

    return type("NS", (), {
        "info": info,
        "created": created,
        "destroyed": destroyed,
        "range_backend": range_backend,
        "cgroup_calls": cgroup_calls,
        "internet_calls": internet_calls,
    })


def _two_node_link_topo(name="nsr") -> Topology:
    t = Topology(name)
    a = t.node("a", image="ubuntu", vcpu=1, memory=1024)
    b = t.node("b", image="ubuntu", vcpu=1, memory=1024)
    t.link(a.eth1["10.0.0.1/24"], b.eth1["10.0.0.2/24"])
    return t


# --- deploy ----------------------------------------------------------------

def test_deploy_calls_create_range(backend, db, ns):
    engine = Engine(backend, db, use_namespaces=True)
    topo = _two_node_link_topo()
    engine.deploy(topo)
    # No resources → no cgroup path passed to create_range.
    assert ns.created == [("nsr", "192.168.100.0/24", None)]


def test_deploy_uses_per_range_backend_for_vms(backend, db, ns):
    """VM create/start go to the per-range backend, not the template."""
    engine = Engine(backend, db, use_namespaces=True)
    engine.deploy(_two_node_link_topo())
    # Template backend only used for resource validation, never for VM ops.
    assert backend.calls_of("create_vm") == []
    assert len(ns.range_backend.calls_of("create_vm")) == 2
    assert len(ns.range_backend.calls_of("start")) == 2


def test_make_range_backend_gets_socket_and_netns(backend, db, monkeypatch):
    """The real factory builds a LibvirtBackend bound to socket + netns."""
    captured = {}

    class FakeLVB(MockBackend):
        def __init__(self, **kwargs):
            super().__init__()
            captured.update(kwargs)

    monkeypatch.setattr(engine_mod, "LibvirtBackend", FakeLVB)
    info = RangeInfo(
        name="r", pid=1, netns_name="rangectl-r",
        libvirt_socket="/ranges/r/run-libvirt/libvirt-sock",
        mgmt_subnet="192.168.100.0/24", veth_host="h", veth_ns="p",
    )
    engine = Engine(backend, db, use_namespaces=True)
    engine._make_range_backend(info)
    assert captured["libvirt_socket"] == info.libvirt_socket
    assert captured["netns_name"] == info.netns_name


def test_deploy_uses_clean_bridge_names(backend, db, ns):
    engine = Engine(backend, db, use_namespaces=True)
    engine.deploy(_two_node_link_topo())
    bridges = [c[0][0] for c in ns.range_backend.calls_of("create_bridge")]
    # Single topology link -> data-0. mgmt-br is created by the supervisor,
    # not the engine, so it must NOT appear here.
    assert bridges == ["data-0"]
    # Interface specs reference clean names.
    specs = [c[0][0] for c in ns.range_backend.calls_of("create_vm")]
    mgmt_bridges = {ifs.bridge for s in specs for ifs in s.interfaces
                    if ifs.interface_name == "mgmt"}
    assert mgmt_bridges == {"mgmt-br"}
    data_bridges = {ifs.bridge for s in specs for ifs in s.interfaces
                    if ifs.interface_name == "eth1"}
    assert data_bridges == {"data-0"}


def test_deploy_skips_host_mgmt_bridge_creation(backend, db, ns):
    """In ns mode the engine must not create/assign the mgmt bridge itself."""
    engine = Engine(backend, db, use_namespaces=True)
    engine.deploy(_two_node_link_topo())
    assert ns.range_backend.calls_of("assign_host_ip") == []
    bridges = [c[0][0] for c in ns.range_backend.calls_of("create_bridge")]
    assert "mgmt-br" not in bridges


# --- destroy ---------------------------------------------------------------

def test_destroy_calls_destroy_range(backend, db, ns):
    engine = Engine(backend, db, use_namespaces=True)
    topo = _two_node_link_topo()
    engine.deploy(topo)
    engine.destroy(topo)
    assert ns.destroyed == ["nsr"]


def test_destroy_reaps_vms_via_pidns_not_per_vm(backend, db, ns):
    """In ns mode, VM nodes are reaped by killing the range's libvirtd
    (destroy_range), NOT by a slow per-VM `virsh destroy`/`stop`. Asserting no
    per-VM destroy/stop locks in the perf fix (see deploy-performance analysis)."""
    engine = Engine(backend, db, use_namespaces=True)
    topo = _two_node_link_topo()
    engine.deploy(topo)
    ns.range_backend.calls.clear()
    engine.destroy(topo)
    assert ns.destroyed == ["nsr"]
    assert ns.range_backend.calls_of("destroy") == []
    assert ns.range_backend.calls_of("stop") == []


def test_destroy_does_not_delete_bridges_directly(backend, db, ns):
    """Bridges live inside the netns; destroy_range cleans them, so the engine
    must not issue per-bridge delete calls in ns mode."""
    engine = Engine(backend, db, use_namespaces=True)
    topo = _two_node_link_topo()
    engine.deploy(topo)
    ns.range_backend.calls.clear()
    engine.destroy(topo)
    assert ns.range_backend.calls_of("delete_bridge") == []


# --- cleanup_on_fail -------------------------------------------------------

def test_deploy_cleanup_on_fail_tears_down_range(backend, db, ns, monkeypatch):
    """A mid-wave deploy failure (e.g. node SSH never comes up) must tear the
    range down — destroy the started VMs and the per-range libvirtd/netns — so
    nothing leaks. Regression for the cleanup_on_fail gap."""
    engine = Engine(backend, db, use_namespaces=True)
    topo = _two_node_link_topo()

    def boom(vm_id):
        ns.range_backend._record("start", vm_id)
        raise RuntimeError("SSH not reachable on 192.168.100.2 after 240s: timed out")
    monkeypatch.setattr(ns.range_backend, "start", boom)

    with pytest.raises(RuntimeError, match="timed out"):
        engine.deploy(topo)

    # Per-range libvirtd/netns torn down (destroy_range called for this topo) —
    # this is what reaps the started VMs (PID-ns kill), so the engine does NOT
    # issue a per-VM `virsh destroy` in ns mode.
    assert ns.destroyed == ["nsr"]
    assert ns.range_backend.calls_of("destroy") == []
    # mgmt subnet freed + topology row removed.
    assert db.get_topology("nsr") is None


def test_deploy_cleanup_on_fail_false_leaves_state(backend, db, ns, monkeypatch):
    """With cleanup_on_fail=False the engine must NOT tear down on failure."""
    engine = Engine(backend, db, use_namespaces=True)
    topo = _two_node_link_topo()

    def boom(vm_id):
        raise RuntimeError("boom")
    monkeypatch.setattr(ns.range_backend, "start", boom)

    with pytest.raises(RuntimeError, match="boom"):
        engine.deploy(topo, cleanup_on_fail=False)

    assert ns.destroyed == []


# --- cgroup integration ----------------------------------------------------

def test_deploy_with_resources_creates_cgroup_and_writes_pid(backend, db, ns):
    res = Resources(memory="4G", cpus=2)
    engine = Engine(backend, db, use_namespaces=True, resources=res)
    topo = _two_node_link_topo()
    engine.deploy(topo)
    kinds = [c[0] for c in ns.cgroup_calls]
    assert kinds[0] == "create"
    assert ("write_pid", f"/sys/fs/cgroup/rangectl-{topo.name}", 4242) in ns.cgroup_calls
    # The cgroup path must reach create_range so libvirtd self-places into it —
    # this is what actually puts QEMU under the freezer/limits.
    assert ns.created == [("nsr", "192.168.100.0/24",
                          f"/sys/fs/cgroup/rangectl-{topo.name}")]
    engine.destroy(topo)
    assert ("destroy", topo.name) in ns.cgroup_calls


def test_deploy_without_resources_skips_cgroup(backend, db, ns):
    engine = Engine(backend, db, use_namespaces=True)
    engine.deploy(_two_node_link_topo())
    assert ns.cgroup_calls == []


# --- internet policy -------------------------------------------------------

def test_deploy_full_internet_enables_during_setup(backend, db, ns):
    engine = Engine(backend, db, use_namespaces=True, internet="full")
    topo = _two_node_link_topo()
    engine.deploy(topo)
    assert ("enable", "nsr", "192.168.100.0/24", "mgh1234") in ns.internet_calls


def test_deploy_none_internet_does_not_enable(backend, db, ns):
    engine = Engine(backend, db, use_namespaces=True)  # internet defaults none
    engine.deploy(_two_node_link_topo())
    assert ns.internet_calls == []


def test_destroy_full_internet_disables(backend, db, ns):
    engine = Engine(backend, db, use_namespaces=True, internet="full")
    topo = _two_node_link_topo()
    engine.deploy(topo)
    engine.destroy(topo)
    assert ("disable", "nsr", "192.168.100.0/24", "mgh1234") in ns.internet_calls


def test_deploy_full_internet_wires_range_controls(backend, db, ns):
    """The returned Range carries veth + subnet so runtime toggle works."""
    engine = Engine(backend, db, use_namespaces=True, internet="full")
    rng = engine.deploy(_two_node_link_topo())
    assert rng.internet == "full"
    assert rng._veth_host == "mgh1234"
    assert rng._mgmt_subnet == "192.168.100.0/24"


# --- backward compat -------------------------------------------------------

def test_legacy_mode_never_touches_supervisor(backend, db, ns):
    engine = Engine(backend, db)  # use_namespaces defaults False
    topo = _two_node_link_topo("legacy")
    engine.deploy(topo)
    engine.destroy(topo)
    assert ns.created == []
    assert ns.destroyed == []
    # VM ops went to the template backend as before.
    assert len(backend.calls_of("create_vm")) == 2
