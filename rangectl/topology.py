from __future__ import annotations
import logging
import os
import tempfile
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

    def __init__(
        self,
        name: str,
        backend: Any = None,
        db: Any = None,
    ) -> None:
        self.name = name
        self._nodes: dict[str, Node] = {}
        self._links: list[Link] = []
        self._backend = backend
        self._db = db
        self._engine = None
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
        # Record IP on the node's interface so deploy() can configure it.
        node_a = self._nodes[if_a.node_name]
        node_a._interfaces[if_a.interface_name] = if_a
        node_b = self._nodes[if_b.node_name]
        node_b._interfaces[if_b.interface_name] = if_b
        lnk = Link(if_a, if_b, topology=self)
        self._links.append(lnk)
        return lnk

    def deploy(self, cleanup_on_fail: bool = True) -> Range:
        from rangectl.engine import Engine
        log.info("Deploying topology '%s' (%d nodes, %d links, cleanup_on_fail=%s)",
                 self.name, len(self._nodes), len(self._links), cleanup_on_fail)
        if self._backend is None or self._db is None:
            raise RuntimeError(
                "Topology.deploy() requires backend and db. "
                "Pass them to Topology(name, backend=..., db=...) or use Engine directly."
            )
        self._engine = Engine(self._backend, self._db)
        rng = self._engine.deploy(self, cleanup_on_fail=cleanup_on_fail)
        rng._engine = self._engine
        rng._db = self._db
        rng._backend = self._backend
        for live in rng._nodes.values():
            live._db = self._db
        return rng

    def export(self, path: str) -> None:
        import yaml
        log.info("Exporting topology '%s' to %s", self.name, path)
        data: dict[str, Any] = {"name": self.name, "nodes": [], "links": []}
        for node in self._nodes.values():
            node_data = {
                "name": node.name,
                "image": node.image,
                "vcpu": node.vcpu,
                "memory": node.memory,
                "os": node.os_type.value,
                "depends_on": [d.name for d in node.depends_on],
                "interfaces": [
                    {"name": k, "ip": v.ip, "cidr": v.cidr}
                    for k, v in node._interfaces.items()
                ],
            }
            data["nodes"].append(node_data)
        for lnk in self._links:
            data["links"].append({
                "node_a": lnk.if_a.node_name,
                "iface_a": lnk.if_a.interface_name,
                "ip_a": f"{lnk.if_a.ip}/{lnk.if_a.cidr}" if lnk.if_a.ip else None,
                "node_b": lnk.if_b.node_name,
                "iface_b": lnk.if_b.interface_name,
                "ip_b": f"{lnk.if_b.ip}/{lnk.if_b.cidr}" if lnk.if_b.ip else None,
            })
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    @classmethod
    def from_yaml(cls, path: str) -> Topology:
        import yaml
        log.info("Loading topology from %s", path)
        with open(path) as f:
            data = yaml.safe_load(f)
        topo = cls(data["name"])
        # First pass: create all nodes (depends_on resolved in second pass).
        for nd in data.get("nodes", []):
            node = topo.node(
                nd["name"],
                nd["image"],
                vcpu=nd.get("vcpu", 1),
                memory=nd.get("memory", 1024),
                os=nd.get("os", "linux"),
            )
            # Restore declared interfaces (with IPs if present).
            for iface in nd.get("interfaces", []) or []:
                node._interfaces[iface["name"]] = InterfaceSpec(
                    node_name=node.name,
                    interface_name=iface["name"],
                    ip=iface.get("ip"),
                    cidr=iface.get("cidr"),
                )
        # Second pass: wire depends_on now that all nodes exist.
        for nd in data.get("nodes", []):
            node = topo._nodes[nd["name"]]
            node.depends_on = [topo._nodes[d] for d in nd.get("depends_on", [])]
        # Recreate links.
        for lnk in data.get("links", []) or []:
            node_a = topo._nodes[lnk["node_a"]]
            if_a = getattr(node_a, lnk["iface_a"])
            if lnk.get("ip_a"):
                if_a = if_a[lnk["ip_a"]]
            node_b = topo._nodes[lnk["node_b"]]
            if_b = getattr(node_b, lnk["iface_b"])
            if lnk.get("ip_b"):
                if_b = if_b[lnk["ip_b"]]
            topo.link(if_a, if_b)
        return topo

    def destroy(self) -> None:
        log.info("Destroying topology '%s'", self.name)
        if self._engine is None:
            raise RuntimeError(
                f"Topology '{self.name}' was not deployed via Topology.deploy(); nothing to destroy"
            )
        self._engine.destroy(self)


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
            # __getattr__ is only called when normal lookup fails, so _interfaces
            # may not exist yet during __init__ — guard against that.
            ifaces = self.__dict__.get("_interfaces")
            if ifaces is None:
                raise AttributeError(name)
            if name not in ifaces:
                ifaces[name] = InterfaceSpec(
                    node_name=self.name,
                    interface_name=name,
                )
            return ifaces[name]
        raise AttributeError(f"'{type(self).__name__}' has no attribute '{name}'")


class Link:

    def __init__(self, if_a: InterfaceSpec, if_b: InterfaceSpec, topology: Topology) -> None:
        self.if_a = if_a
        self.if_b = if_b
        self.topology = topology
        self._is_up = True
        # Wired by Engine._wire_link after the bridge is created.
        self._backend: Any = None
        self._bridge_name: str | None = None
        self._db: Any = None
        self._topology_name: str | None = None
        # Endpoints needed for Link.up() to re-enslave VM TAPs to the
        # recreated bridge. Populated by Engine._wire_link as
        # [(vm_id, mac), (vm_id, mac)].
        self._endpoints: list[tuple[str, str]] = []

    def down(self) -> None:
        log.info("Link down: %s/%s <-> %s/%s",
                 self.if_a.node_name, self.if_a.interface_name,
                 self.if_b.node_name, self.if_b.interface_name)
        if self._backend is None or self._bridge_name is None:
            raise RuntimeError("Link not wired to backend; deploy the topology first")
        self._backend.delete_bridge(self._bridge_name)
        self._is_up = False
        if self._db is not None and self._topology_name is not None:
            self._db.log_event(self._topology_name, None, "info",
                               f"link down: {self._bridge_name}")

    def up(self) -> None:
        log.info("Link up: %s/%s <-> %s/%s",
                 self.if_a.node_name, self.if_a.interface_name,
                 self.if_b.node_name, self.if_b.interface_name)
        if self._backend is None or self._bridge_name is None:
            raise RuntimeError("Link not wired to backend; deploy the topology first")
        self._backend.create_bridge(self._bridge_name)
        # Re-enslave each VM's TAP to the newly recreated bridge. Deleting
        # the bridge orphaned its slave TAPs; creating a fresh bridge with
        # the same name does NOT auto-reattach them, so we must do it
        # explicitly to restore connectivity.
        for vm_id, mac in self._endpoints:
            self._backend.attach_interface(vm_id, self._bridge_name, mac)
        self._is_up = True
        if self._db is not None and self._topology_name is not None:
            self._db.log_event(self._topology_name, None, "info",
                               f"link up: {self._bridge_name}")


class Range:
    """Live handle to a deployed topology. Returned by Topology.deploy()."""

    def __init__(self, topology: Topology) -> None:
        self.topology = topology
        self._nodes: dict[str, LiveNode] = {}
        self._engine: Any = None
        self._db: Any = None
        self._backend: Any = None

    def __enter__(self) -> Range:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        log.info("Range context exiting, destroying topology '%s'", self.topology.name)
        if self._engine is not None:
            self._engine.destroy(self.topology)
        else:
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
        for live in self._nodes.values():
            live.snapshot(name)
        if self._db is not None:
            self._db.log_event(self.topology.name, None, "info",
                               f"range snapshot: {name}")

    def restore(self, name: str) -> None:
        log.info("Restore all nodes in '%s' to snapshot: %s", self.topology.name, name)
        for live in self._nodes.values():
            live.restore(name)
        if self._db is not None:
            self._db.log_event(self.topology.name, None, "info",
                               f"range restore: {name}")

    def logs(self, level: str | None = None) -> list[dict]:
        log.info("Fetching logs for topology '%s' (level=%s)", self.topology.name, level)
        if self._db is None:
            raise RuntimeError("Range has no DB reference; cannot fetch logs")
        return self._db.get_logs(self.topology.name, level=level)


class LiveNode:
    """Handle to a running node within a deployed topology."""

    def __init__(self, name: str, mgmt_ip: str, topology_name: str,
                 backend: Any = None, vm_id: str | None = None,
                 db: Any = None) -> None:
        self.name = name
        self.mgmt_ip = mgmt_ip
        self.topology_name = topology_name
        self._backend = backend
        self._vm_id = vm_id
        self._db = db

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
        from jinja2 import Template
        log.info("[%s/%s] template: %s -> %s (vars=%s)", self.topology_name, self.name, src, dst, vars)
        if self._backend is None or self._vm_id is None:
            raise RuntimeError(
                f"LiveNode {self.name!r} not bound to a backend; cannot template"
            )
        with open(src) as f:
            rendered = Template(f.read()).render(vars or {})
        # Write rendered output to a temp file, upload, then clean up.
        fd, tmp_path = tempfile.mkstemp(suffix=".rendered")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(rendered)
            self._backend.upload(self._vm_id, tmp_path, dst)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def logs(self, level: str | None = None) -> list[dict]:
        log.info("Fetching logs for node '%s/%s' (level=%s)", self.topology_name, self.name, level)
        if self._db is None:
            raise RuntimeError(f"LiveNode {self.name!r} has no DB reference; cannot fetch logs")
        return self._db.get_logs(self.topology_name, node_name=self.name, level=level)

    def snapshot(self, name: str) -> str:
        log.info("[%s/%s] snapshot: %s", self.topology_name, self.name, name)
        if self._backend is None or self._vm_id is None:
            raise RuntimeError(
                f"LiveNode {self.name!r} not bound to a backend; cannot snapshot"
            )
        snap_id = self._backend.snapshot(self._vm_id, name)
        if self._db is not None:
            with self._db._lock:
                self._db._conn.execute(
                    "INSERT INTO snapshots (topology_name, node_name, snapshot_name, snapshot_id) "
                    "VALUES (?, ?, ?, ?)",
                    (self.topology_name, self.name, name, snap_id),
                )
                self._db._conn.commit()
        return snap_id

    def restore(self, name: str) -> None:
        log.info("[%s/%s] restore: %s", self.topology_name, self.name, name)
        if self._backend is None or self._vm_id is None:
            raise RuntimeError(
                f"LiveNode {self.name!r} not bound to a backend; cannot restore"
            )
        snap_id: str | None = None
        if self._db is not None:
            cur = self._db._conn.execute(
                "SELECT snapshot_id FROM snapshots "
                "WHERE topology_name=? AND node_name=? AND snapshot_name=? "
                "ORDER BY id DESC LIMIT 1",
                (self.topology_name, self.name, name),
            )
            row = cur.fetchone()
            if row:
                snap_id = row[0]
        # Fall back to the given name if no DB record (test-friendly behavior).
        self._backend.restore(self._vm_id, snap_id or name)
