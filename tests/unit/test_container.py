"""Unit tests for Phase 7 — Docker container nodes.

Covers:
- Node.container kwarg + validation
- ContainerBackend shell delegation (create/start/stop/destroy/exec/upload)
- Container interface (veth) wiring
- Mixed VM + container topology DAG resolution
- Engine dispatch between libvirt backend and container backend
"""
from __future__ import annotations
from unittest.mock import patch, MagicMock

import pytest

from rangectl import Topology
from rangectl.container_backend import ContainerBackend
from rangectl.engine import Engine
from rangectl.types import ExecResult, InterfaceSpec, OSType, VMSpec


# --- Node & Topology API ---------------------------------------------------

def test_container_node_creation():
    t = Topology("mixed")
    n = t.node("svc", container="nginx:latest", vcpu=1, memory=512)
    assert n.container == "nginx:latest"
    assert n.is_container is True
    # Falling back to image=None is fine because container is set.
    assert n.image is None


def test_vm_node_is_not_container():
    t = Topology("mixed")
    n = t.node("vm", image="ubuntu-22.04")
    assert n.is_container is False
    assert n.container is None


def test_node_validation_neither_image_nor_container():
    t = Topology("bad")
    with pytest.raises(ValueError, match="image.*container"):
        t.node("nope")


def test_node_validation_both_image_and_container():
    t = Topology("bad")
    with pytest.raises(ValueError, match="image.*container"):
        t.node("nope", image="ubuntu-22.04", container="nginx:latest")


def test_mixed_topology_dag(backend, db):
    """VM + container nodes resolve into the same waves as VM-only."""
    t = Topology("mixed")
    c = t.node("c-svc", container="nginx:latest", vcpu=1, memory=128)
    t.node("vm-host", image="ubuntu-22.04", vcpu=1, memory=1024,
           depends_on=[c])
    engine = Engine(backend, db)
    waves = engine.compute_waves(t)
    assert [n.name for n in waves[0]] == ["c-svc"]
    assert [n.name for n in waves[1]] == ["vm-host"]


# --- ContainerBackend lifecycle --------------------------------------------

def _run_ok(stdout: str = "", stderr: str = "", rc: int = 0):
    """Return a CompletedProcess-like stub."""
    m = MagicMock()
    m.returncode = rc
    m.stdout = stdout
    m.stderr = stderr
    return m


@pytest.fixture
def cb():
    return ContainerBackend()


def test_container_backend_create_runs_docker_create(cb):
    spec = VMSpec(
        name="topo-svc",
        image="nginx:latest",
        vcpu=1,
        memory=128,
        os_type=OSType.LINUX,
        interfaces=[],
        topology_name="topo",
    )
    with patch("rangectl.container_backend._run") as run:
        run.return_value = _run_ok(stdout="topo-svc\n")
        vm_id = cb.create_vm(spec)
    assert vm_id == "topo-svc"
    cmd = run.call_args_list[0].args[0]
    assert cmd[:2] == ["docker", "create"]
    assert "--network=none" in cmd
    assert "--cap-add=NET_ADMIN" in cmd
    assert "--cap-add=NET_RAW" in cmd
    assert "--name" in cmd
    assert "topo-svc" in cmd
    assert "nginx:latest" in cmd


def test_container_backend_start_stop_destroy(cb):
    with patch("rangectl.container_backend._run") as run:
        run.return_value = _run_ok()
        cb.start("svc")
        cb.stop("svc")
        cb.destroy("svc")
    cmds = [c.args[0] for c in run.call_args_list]
    assert cmds[0] == ["docker", "start", "svc"]
    assert cmds[1] == ["docker", "stop", "svc"]
    assert cmds[2] == ["docker", "rm", "-f", "svc"]


def test_container_backend_exec_returns_exec_result(cb):
    with patch("rangectl.container_backend._run") as run:
        run.return_value = _run_ok(stdout="hello\n", stderr="", rc=0)
        result = cb.exec("svc", "echo hello")
    assert isinstance(result, ExecResult)
    assert result.exit_code == 0
    assert "hello" in result.stdout
    cmd = run.call_args_list[0].args[0]
    assert cmd[:3] == ["docker", "exec", "svc"]


def test_container_backend_exec_propagates_nonzero(cb):
    with patch("rangectl.container_backend._run") as run:
        run.return_value = _run_ok(stdout="", stderr="nope", rc=2)
        result = cb.exec("svc", "false")
    assert result.exit_code == 2
    assert result.stderr == "nope"


def test_container_backend_upload_uses_docker_cp(cb):
    with patch("rangectl.container_backend._run") as run:
        run.return_value = _run_ok()
        cb.upload("svc", "/tmp/a", "/etc/a")
    cmd = run.call_args_list[0].args[0]
    assert cmd == ["docker", "cp", "/tmp/a", "svc:/etc/a"]


def test_container_backend_create_overlay_noop(cb):
    # Containers don't have qcow2 overlays — return path unchanged.
    assert cb.create_overlay("nginx:latest", "/ignored") == "/ignored"


def test_container_backend_snapshot_raises(cb):
    with pytest.raises(NotImplementedError):
        cb.snapshot("svc", "snap1")


def test_container_backend_restore_raises(cb):
    with pytest.raises(NotImplementedError):
        cb.restore("svc", "snap1")


def test_container_backend_create_bridge_idempotent(cb):
    with patch("rangectl.container_backend._run") as run:
        # First add: success. Then set up.
        run.return_value = _run_ok()
        cb.create_bridge("br0")
    cmds = [c.args[0] for c in run.call_args_list]
    # ip link add ... + ip link set ... up
    assert cmds[0][:4] == ["ip", "link", "add", "name"]
    assert cmds[-1][:5] == ["ip", "link", "set", "br0", "up"]


# --- attach_interface (veth wiring) ----------------------------------------

def test_container_backend_attach_interface_does_veth_wiring(cb):
    spec = VMSpec(
        name="topo-svc",
        image="nginx:latest",
        vcpu=1, memory=128, os_type=OSType.LINUX,
        interfaces=[
            InterfaceSpec(
                node_name="svc", interface_name="mgmt",
                ip="10.255.1.2", cidr="24",
                bridge="rlmgt-topo", mac="52:54:00:aa:bb:01",
            ),
            InterfaceSpec(
                node_name="svc", interface_name="eth1",
                ip="10.0.1.1", cidr="24",
                bridge="topo-br0", mac="52:54:00:aa:bb:02",
            ),
        ],
        topology_name="topo",
    )
    # Seed the create call so cb stores the spec.
    with patch("rangectl.container_backend._run") as run:
        run.return_value = _run_ok(stdout="topo-svc\n")
        cb.create_vm(spec)

    with patch("rangectl.container_backend._run") as run:
        # inspect returns the pid; everything else returns ok.
        def side_effect(cmd, **kw):
            if cmd[:2] == ["docker", "inspect"]:
                return _run_ok(stdout="12345\n")
            if cmd[:3] == ["ip", "link", "show"]:
                return _run_ok(rc=1, stderr="does not exist")
            return _run_ok()
        run.side_effect = side_effect

        # mgmt iface → guest device eth0
        cb.attach_interface("topo-svc", "rlmgt-topo", "52:54:00:aa:bb:01")

    cmds = [c.args[0] for c in run.call_args_list]
    flat = [" ".join(c) for c in cmds]
    # veth pair created
    assert any("ip link add" in s and "type veth" in s for s in flat), flat
    # bridge enslavement of the host side
    assert any("master rlmgt-topo" in s for s in flat), flat
    # peer moved into netns by container pid
    assert any("netns 12345" in s for s in flat), flat
    # renamed to eth0 inside the netns
    assert any("nsenter" in s and "name eth0" in s for s in flat), flat
    # IP assignment inside the netns
    assert any(
        "nsenter" in s and "ip addr add 10.255.1.2/24" in s for s in flat
    ), flat


def test_container_backend_attach_interface_topology_link_uses_iface_name(cb):
    spec = VMSpec(
        name="topo-svc",
        image="nginx:latest",
        vcpu=1, memory=128, os_type=OSType.LINUX,
        interfaces=[
            InterfaceSpec(
                node_name="svc", interface_name="mgmt",
                ip="10.255.1.2", cidr="24",
                bridge="rlmgt-topo", mac="52:54:00:aa:bb:01",
            ),
            InterfaceSpec(
                node_name="svc", interface_name="eth1",
                ip="10.0.1.1", cidr="24",
                bridge="topo-br0", mac="52:54:00:aa:bb:02",
            ),
        ],
        topology_name="topo",
    )
    with patch("rangectl.container_backend._run") as run:
        run.return_value = _run_ok(stdout="topo-svc\n")
        cb.create_vm(spec)

    with patch("rangectl.container_backend._run") as run:
        def side_effect(cmd, **kw):
            if cmd[:2] == ["docker", "inspect"]:
                return _run_ok(stdout="12345\n")
            if cmd[:3] == ["ip", "link", "show"]:
                return _run_ok(rc=1, stderr="does not exist")
            return _run_ok()
        run.side_effect = side_effect
        cb.attach_interface("topo-svc", "topo-br0", "52:54:00:aa:bb:02")

    flat = [" ".join(c.args[0]) for c in run.call_args_list]
    assert any("name eth1" in s for s in flat), flat
    assert any("ip addr add 10.0.1.1/24" in s for s in flat), flat


def test_container_backend_attach_interface_netns_mode():
    """With a range netns, the host-side veth is moved into the netns and the
    bridge enslavement happens inside it (Phase 11)."""
    cb = ContainerBackend(netns_name="rangectl-r")
    spec = VMSpec(
        name="r-svc",
        image="nginx:latest",
        vcpu=1, memory=128, os_type=OSType.LINUX,
        interfaces=[
            InterfaceSpec(
                node_name="svc", interface_name="mgmt",
                ip="10.255.1.2", cidr="24",
                bridge="mgmt-br", mac="52:54:00:aa:bb:01",
            ),
        ],
        topology_name="r",
    )
    with patch("rangectl.container_backend._run") as run:
        run.return_value = _run_ok(stdout="r-svc\n")
        cb.create_vm(spec)

    with patch("rangectl.container_backend._run") as run:
        def side_effect(cmd, **kw):
            if cmd[:2] == ["docker", "inspect"]:
                return _run_ok(stdout="12345\n")
            # existence check lives inside the netns now; report missing.
            if cmd[:3] == ["ip", "netns", "exec"] and "show" in cmd:
                return _run_ok(rc=1, stderr="does not exist")
            return _run_ok()
        run.side_effect = side_effect
        cb.attach_interface("r-svc", "mgmt-br", "52:54:00:aa:bb:01")

    flat = [" ".join(c.args[0]) for c in run.call_args_list]
    # Host-side veth moved into the range netns.
    assert any("link set" in s and "netns rangectl-r" in s for s in flat), flat
    # Bridge enslavement runs inside the netns.
    assert any(
        s.startswith("ip netns exec rangectl-r ") and "master mgmt-br" in s
        for s in flat
    ), flat


# --- Engine dispatch -------------------------------------------------------

class _MockContainerBackend:
    """Minimal container backend recording calls; mirrors MockBackend shape."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.exec_default = ExecResult(0, "", "")
        self._next = 0

    def _rec(self, n, *a, **k):
        self.calls.append((n, a, k))

    def create_vm(self, spec):
        self._rec("create_vm", spec)
        self._next += 1
        return f"ctr-{self._next}"

    def start(self, vm_id): self._rec("start", vm_id)
    def stop(self, vm_id): self._rec("stop", vm_id)
    def destroy(self, vm_id): self._rec("destroy", vm_id)
    def exec(self, vm_id, cmd):
        self._rec("exec", vm_id, cmd)
        return self.exec_default
    def upload(self, vm_id, src, dst): self._rec("upload", vm_id, src, dst)
    def create_overlay(self, base, path):
        self._rec("create_overlay", base, path)
        return path
    def attach_interface(self, vm_id, bridge, mac):
        self._rec("attach_interface", vm_id, bridge, mac)
    def snapshot(self, vm_id, name):
        raise NotImplementedError
    def restore(self, vm_id, snap_id):
        raise NotImplementedError

    def calls_of(self, name):
        return [(a, k) for (n, a, k) in self.calls if n == name]


def test_engine_dispatches_container_backend_for_container_node(backend, db):
    """Engine routes create_vm of a container node to the container backend."""
    cb = _MockContainerBackend()
    t = Topology("mixed")
    t.node("svc", container="nginx:latest", vcpu=1, memory=128)
    t.node("vm", image="ubuntu", vcpu=1, memory=1024)
    engine = Engine(backend, db, container_backend=cb)
    engine.deploy(t)
    # container backend got the container node
    ctr_specs = [c[0] for (c, _) in cb.calls_of("create_vm")]
    assert len(ctr_specs) == 1
    assert ctr_specs[0].name == "mixed-svc"
    # libvirt mock got the VM node
    vm_specs = [c[1][0] for c in backend.calls if c[0] == "create_vm"]
    assert len(vm_specs) == 1
    assert vm_specs[0].name == "mixed-vm"


def test_engine_destroy_uses_per_node_backend(backend, db):
    cb = _MockContainerBackend()
    t = Topology("mixed")
    t.node("svc", container="nginx:latest", vcpu=1, memory=128)
    t.node("vm", image="ubuntu", vcpu=1, memory=1024)
    engine = Engine(backend, db, container_backend=cb)
    engine.deploy(t)
    engine.destroy(t)
    # Teardown force-destroys via each node's own backend; no graceful stop().
    assert len(cb.calls_of("stop")) == 0
    assert len(cb.calls_of("destroy")) == 1
    # The VM half goes to libvirt backend
    vm_stops = [c for c in backend.calls if c[0] == "stop"]
    vm_destroys = [c for c in backend.calls if c[0] == "destroy"]
    assert len(vm_stops) == 0
    assert len(vm_destroys) == 1


def test_engine_skips_overlay_and_seed_for_container_node(backend, db):
    cb = _MockContainerBackend()
    t = Topology("mixed")
    t.node("svc", container="nginx:latest", vcpu=1, memory=128)
    engine = Engine(backend, db, container_backend=cb)
    engine.deploy(t)
    # No libvirt overlay calls for container-only topology.
    assert backend.calls_of("create_overlay") == []
    # Container backend's create_overlay should also not be required (no-op acceptable).


def test_engine_live_node_bound_to_container_backend(backend, db):
    cb = _MockContainerBackend()
    t = Topology("mixed")
    t.node("svc", container="nginx:latest", vcpu=1, memory=128)
    t.node("vm", image="ubuntu", vcpu=1, memory=1024)
    engine = Engine(backend, db, container_backend=cb)
    rng = engine.deploy(t)
    # LiveNode for container should use the container backend
    assert rng["svc"]._backend is cb
    assert rng["vm"]._backend is backend


def test_mixed_topology_link_attaches_both_backends(backend, db):
    """A link between a VM and a container attaches on both backends."""
    cb = _MockContainerBackend()
    t = Topology("mixed", backend=backend, db=db)
    c = t.node("svc", container="nginx:latest", vcpu=1, memory=128)
    v = t.node("vm", image="ubuntu", vcpu=1, memory=1024)
    t.link(c.eth1["10.0.1.1/24"], v.eth1["10.0.1.2/24"])
    engine = Engine(backend, db, container_backend=cb)
    engine.deploy(t)
    # Container side: attach_interface called for mgmt + eth1
    ctr_attaches = cb.calls_of("attach_interface")
    assert len(ctr_attaches) >= 2
    # VM side: 1 mgmt + 1 link iface attach on libvirt mock
    vm_attaches = backend.calls_of("attach_interface")
    assert len(vm_attaches) >= 2
