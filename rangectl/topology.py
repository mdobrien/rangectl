from __future__ import annotations
import logging
from typing import Any

from rangectl.dependencies import DependencyMixin
from rangectl.types import (
    ExecResult,
    InterfaceSpec,
    NodeState,
    OSType,
    ReadinessProbe,
)

log = logging.getLogger(__name__)


class Topology:

    def __init__(self, name: str) -> None:
        self.name = name
        self._nodes: dict[str, Node] = {}
        self._links: list[Link] = []
        log.info("Topology '%s' created", name)

    def node(
        self,
        name: str,
        image: str,
        vcpu: int = 1,
        memory: int = 1024,
        os: OSType | str = OSType.LINUX,
        depends_on: list[Node] | None = None,
        ready_when: ReadinessProbe | None = None,
    ) -> Node:
        log.info("Declaring node '%s' (image=%s, vcpu=%d, memory=%dMB)", name, image, vcpu, memory)
        node = Node(
            name=name,
            topology=self,
            image=image,
            vcpu=vcpu,
            memory=memory,
            os_type=OSType(os) if isinstance(os, str) else os,
            depends_on=depends_on or [],
            ready_when=ready_when,
        )
        self._nodes[name] = node
        return node

    def link(self, if_a: InterfaceSpec, if_b: InterfaceSpec) -> Link:
        log.info("Declaring link: %s/%s [%s] <-> %s/%s [%s]",
                 if_a.node_name, if_a.interface_name, if_a.ip,
                 if_b.node_name, if_b.interface_name, if_b.ip)
        lnk = Link(if_a, if_b, topology=self)
        self._links.append(lnk)
        return lnk

    def deploy(self, cleanup_on_fail: bool = True) -> Range:
        log.info("Deploying topology '%s' (%d nodes, %d links, cleanup_on_fail=%s)",
                 self.name, len(self._nodes), len(self._links), cleanup_on_fail)
        log.info("Step 1: Validate resources")
        log.info("Step 2: Create mgmt bridge (rangectl-mgmt-%s)", self.name)
        log.info("Step 3: Build dependency DAG, compute waves")
        log.info("Step 4: Deploy waves (create VMs, wait for readiness, wire links)")
        log.info("Step 5: Run dependency injection (apt, pip, install, configure, services)")
        log.info("Step 6: Return Range handle")
        raise NotImplementedError

    def export(self, path: str) -> None:
        log.info("Exporting topology '%s' to %s", self.name, path)
        log.info("Nodes: %s", list(self._nodes.keys()))
        for lnk in self._links:
            log.info("Link: %s/%s [%s/%s] <-> %s/%s [%s/%s]",
                     lnk.if_a.node_name, lnk.if_a.interface_name, lnk.if_a.ip, lnk.if_a.cidr,
                     lnk.if_b.node_name, lnk.if_b.interface_name, lnk.if_b.ip, lnk.if_b.cidr)
        raise NotImplementedError

    @classmethod
    def from_yaml(cls, path: str) -> Topology:
        log.info("Loading topology from %s", path)
        raise NotImplementedError

    def destroy(self) -> None:
        log.info("Destroying topology '%s'", self.name)
        raise NotImplementedError


class Node(DependencyMixin):

    def __init__(
        self,
        name: str,
        topology: Topology,
        image: str,
        vcpu: int,
        memory: int,
        os_type: OSType,
        depends_on: list[Node],
        ready_when: ReadinessProbe | None,
    ) -> None:
        super().__init__()
        self.name = name
        self.topology = topology
        self.image = image
        self.vcpu = vcpu
        self.memory = memory
        self.os_type = os_type
        self.depends_on = depends_on
        self.ready_when = ready_when
        self.state = NodeState.DEFINED
        self._interfaces: dict[str, InterfaceSpec] = {}

    def __getattr__(self, name: str) -> InterfaceSpec:
        if name.startswith("eth"):
            if name not in self._interfaces:
                self._interfaces[name] = InterfaceSpec(
                    node_name=self.name,
                    interface_name=name,
                )
            return self._interfaces[name]
        raise AttributeError(f"'{type(self).__name__}' has no attribute '{name}'")


class Link:

    def __init__(self, if_a: InterfaceSpec, if_b: InterfaceSpec, topology: Topology) -> None:
        self.if_a = if_a
        self.if_b = if_b
        self.topology = topology
        self._is_up = True

    def down(self) -> None:
        log.info("Link down: %s/%s <-> %s/%s",
                 self.if_a.node_name, self.if_a.interface_name,
                 self.if_b.node_name, self.if_b.interface_name)
        self._is_up = False
        raise NotImplementedError

    def up(self) -> None:
        log.info("Link up: %s/%s <-> %s/%s",
                 self.if_a.node_name, self.if_a.interface_name,
                 self.if_b.node_name, self.if_b.interface_name)
        self._is_up = True
        raise NotImplementedError


class Range:
    """Live handle to a deployed topology. Returned by Topology.deploy()."""

    def __init__(self, topology: Topology) -> None:
        self.topology = topology
        self._nodes: dict[str, LiveNode] = {}

    def __enter__(self) -> Range:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        log.info("Range context exiting, destroying topology '%s'", self.topology.name)
        self.topology.destroy()

    def __getitem__(self, node_name: str) -> LiveNode:
        return self._nodes[node_name]

    def link(self, node_a: str, node_b: str) -> Link:
        log.info("Looking up link between %s and %s", node_a, node_b)
        for lnk in self.topology._links:
            if {lnk.if_a.node_name, lnk.if_b.node_name} == {node_a, node_b}:
                return lnk
        raise KeyError(f"No link between {node_a} and {node_b}")

    def snapshot(self, name: str) -> None:
        log.info("Snapshot all nodes in '%s': %s", self.topology.name, name)
        raise NotImplementedError

    def restore(self, name: str) -> None:
        log.info("Restore all nodes in '%s' to snapshot: %s", self.topology.name, name)
        raise NotImplementedError

    def logs(self, level: str | None = None) -> list[dict]:
        log.info("Fetching logs for topology '%s' (level=%s)", self.topology.name, level)
        raise NotImplementedError


class LiveNode:
    """Handle to a running node within a deployed topology."""

    def __init__(self, name: str, mgmt_ip: str, topology_name: str,
                 backend: Any = None, vm_id: str | None = None) -> None:
        self.name = name
        self.mgmt_ip = mgmt_ip
        self.topology_name = topology_name
        self._backend = backend
        self._vm_id = vm_id

    def exec(self, command: str) -> ExecResult:
        log.info("[%s/%s] exec: %s", self.topology_name, self.name, command)
        if self._backend is None or self._vm_id is None:
            raise RuntimeError(
                f"LiveNode {self.name!r} not bound to a backend; cannot exec"
            )
        return self._backend.exec(self._vm_id, command)

    def upload(self, src: str, dst: str) -> None:
        log.info("[%s/%s] upload: %s -> %s", self.topology_name, self.name, src, dst)
        if self._backend is None or self._vm_id is None:
            raise RuntimeError(
                f"LiveNode {self.name!r} not bound to a backend; cannot upload"
            )
        self._backend.upload(self._vm_id, src, dst)

    def template(self, src: str, dst: str, vars: dict[str, Any] | None = None) -> None:
        log.info("[%s/%s] template: %s -> %s (vars=%s)", self.topology_name, self.name, src, dst, vars)
        raise NotImplementedError

    def logs(self, level: str | None = None) -> list[dict]:
        log.info("Fetching logs for node '%s/%s' (level=%s)", self.topology_name, self.name, level)
        raise NotImplementedError

    def snapshot(self, name: str) -> None:
        log.info("[%s/%s] snapshot: %s", self.topology_name, self.name, name)
        raise NotImplementedError

    def restore(self, name: str) -> None:
        log.info("[%s/%s] restore: %s", self.topology_name, self.name, name)
        raise NotImplementedError
