from __future__ import annotations
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from rangectl import cgroup, supervisor
from rangectl.dependencies import DependencyMixin
from rangectl.drivers import make_driver
from rangectl.types import (
    ExecResult,
    InterfaceSpec,
    NodeState,
    OSType,
    RangeNotRunning,
    ReadinessProbe,
)

log = logging.getLogger(__name__)


def _pid_alive(pid: int) -> bool:
    """True if a process with this PID exists (signal 0 = existence check)."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another user — still alive.
        return True


def _netns_exists(netns_name: str) -> bool:
    return Path(f"/run/netns/{netns_name}").exists()


def _read_range_json(name: str, range_dir: str) -> dict | None:
    p = Path(range_dir) / name / "range.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())


def _range_status(name: str, range_dir: str) -> str:
    """Liveness classification for a persisted range: 'running', 'frozen', or
    'orphaned' (state exists but the libvirtd/netns are gone)."""
    info = _read_range_json(name, range_dir)
    if (info is None or not _pid_alive(info["pid"])
            or not _netns_exists(info["netns_name"])):
        return "orphaned"
    if cgroup.is_frozen(name):
        return "frozen"
    return "running"


class Topology:

    def __init__(
        self,
        name: str,
        backend: Any = None,
        db: Any = None,
        container_backend: Any = None,
    ) -> None:
        # Optional host-unique prefix (e.g. an xdist worker id or uuid) so two
        # concurrent runs of the SAME range name never collide on netns / veth
        # hash / seed+overlay paths, which all derive from the range name. Empty
        # by default — no behavior change outside a parallel test runner.
        self.name = f'{os.environ.get("RANGECTL_RANGE_PREFIX", "")}{name}'
        self._nodes: dict[str, Node] = {}
        self._links: list[Link] = []
        self._backend = backend
        self._container_backend = container_backend
        self._db = db
        self._engine = None
        log.info("Topology '%s' created", self.name)

    def node(
        self,
        name: str,
        image: str | None = None,
        vcpu: int = 1,
        memory: int = 1024,
        os: OSType | str = OSType.LINUX,
        depends_on: list[Node] | None = None,
        ready_when: ReadinessProbe | None = None,
        container: str | None = None,
    ) -> Node:
        if (image is None) == (container is None):
            raise ValueError(
                f"node '{name}': exactly one of image= or container= must be set"
            )
        log.info("Declaring node '%s' (%s, vcpu=%d, memory=%dMB)",
                 name,
                 f"image={image}" if image else f"container={container}",
                 vcpu, memory)
        node = Node(
            name=name,
            topology=self,
            image=image,
            vcpu=vcpu,
            memory=memory,
            os_type=OSType(os) if isinstance(os, str) else os,
            depends_on=depends_on or [],
            ready_when=ready_when,
            container=container,
        )
        self._nodes[name] = node
        return node

    def link(self, if_a: InterfaceSpec, if_b: InterfaceSpec,
             **impairments: Any) -> Link:
        log.info("Declaring link: %s/%s [%s] <-> %s/%s [%s] impair=%s",
                 if_a.node_name, if_a.interface_name, if_a.ip,
                 if_b.node_name, if_b.interface_name, if_b.ip, impairments)
        # Record IP on the node's interface so deploy() can configure it.
        node_a = self._nodes[if_a.node_name]
        node_a._interfaces[if_a.interface_name] = if_a
        node_b = self._nodes[if_b.node_name]
        node_b._interfaces[if_b.interface_name] = if_b
        lnk = Link(if_a, if_b, topology=self)
        # Definition-time impairment defaults (latency=, bandwidth=, loss=, ...)
        # are applied by the engine once the link's TAPs exist during deploy.
        lnk._default_impairments = impairments
        self._links.append(lnk)
        return lnk

    def deploy(self, cleanup_on_fail: bool = True,
               use_namespaces: bool = True,
               resources: Any = None,
               internet: str = "none") -> Range:
        # Namespace mode is the default: each range gets its own netns, so data
        # subnets AND mgmt are isolated and multiple ranges run concurrently with
        # no cross-talk. Legacy host-level mode (use_namespaces=False) shares the
        # host network stack — ranges with overlapping data subnets collide — and
        # is kept only as an explicit opt-in.
        from rangectl.engine import Engine
        log.info("Deploying topology '%s' (%d nodes, %d links, cleanup_on_fail=%s, "
                 "use_namespaces=%s, internet=%s)",
                 self.name, len(self._nodes), len(self._links), cleanup_on_fail,
                 use_namespaces, internet)
        if self._backend is None or self._db is None:
            raise RuntimeError(
                "Topology.deploy() requires backend and db. "
                "Pass them to Topology(name, backend=..., db=...) or use Engine directly."
            )
        self._engine = Engine(self._backend, self._db,
                              container_backend=self._container_backend,
                              use_namespaces=use_namespaces,
                              resources=resources,
                              internet=internet)
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
                "container": node.container,
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
                image=nd.get("image"),
                container=nd.get("container"),
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
        image: str | None,
        vcpu: int,
        memory: int,
        os_type: OSType,
        depends_on: list[Node],
        ready_when: ReadinessProbe | None,
        container: str | None = None,
    ) -> None:
        super().__init__()
        self.name = name
        self.topology = topology
        self.image = image
        self.container = container
        self.vcpu = vcpu
        self.memory = memory
        self.os_type = os_type
        self.depends_on = depends_on
        self.ready_when = ready_when
        self.state = NodeState.DEFINED
        self._interfaces: dict[str, InterfaceSpec] = {}

    @property
    def is_container(self) -> bool:
        return self.container is not None

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
        # Current tc netem impairments, keyed by endpoint node name. Empty dict
        # per side = clean link. Re-applied after Link.up() recreates the TAPs.
        self._impairments: dict[str, dict] = {}
        # Impairments declared at definition time (Topology.link kwargs),
        # applied once by the engine after the link is wired during deploy.
        self._default_impairments: dict = {}

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
        # Recreating the bridge reset the TAPs to clean qdiscs — restore any
        # impairments that were in effect before the link went down.
        self._reapply_impairments()
        if self._db is not None and self._topology_name is not None:
            self._db.log_event(self._topology_name, None, "info",
                               f"link up: {self._bridge_name}")

    # --- impairment (Phase 19 — WAN simulation via tc netem) --------------

    def _sides(self) -> list[tuple[str, tuple[str, str]]]:
        """[(node_name, (vm_id, mac)), ...] — one per endpoint, a then b."""
        return [
            (self.if_a.node_name, self._endpoints[0]),
            (self.if_b.node_name, self._endpoints[1]),
        ]

    def _targets(self, outbound: str | None) -> list[tuple[str, tuple[str, str]]]:
        sides = self._sides()
        if outbound is None:
            return sides
        matched = [s for s in sides if s[0] == outbound]
        if not matched:
            raise ValueError(
                f"outbound={outbound!r} is not an endpoint of this link "
                f"({self.if_a.node_name}, {self.if_b.node_name})"
            )
        return matched

    def impair(self, *, latency=None, jitter=None, bandwidth=None, loss=None,
               reorder=None, corrupt=None, duplicate=None,
               outbound: str | None = None) -> None:
        """Apply tc netem impairments on the link's TAP devices.

        Symmetric by default (both directions). Pass ``outbound=<node>`` to
        degrade only that node's egress. Replaces any current impairment on
        the targeted side(s); other params are not merged.
        """
        from rangectl.link_properties import build_netem_cmds
        if self._backend is None:
            raise RuntimeError("Link not wired to backend; deploy the topology first")
        params = {k: v for k, v in dict(
            latency=latency, jitter=jitter, bandwidth=bandwidth, loss=loss,
            reorder=reorder, corrupt=corrupt, duplicate=duplicate,
        ).items() if v is not None}
        log.info("Link impair %s/%s <-> %s/%s: %s (outbound=%s)",
                 self.if_a.node_name, self.if_a.interface_name,
                 self.if_b.node_name, self.if_b.interface_name, params, outbound)
        netns = getattr(self._backend, "_netns_name", None)
        cmds: list[list[str]] = []
        for node_name, (vm_id, mac) in self._targets(outbound):
            tap = self._backend._find_tap_for_mac(vm_id, mac)
            if not tap:
                raise RuntimeError(
                    f"no TAP found for {node_name} (vm={vm_id} mac={mac})")
            cmds += build_netem_cmds(tap, netns, **params)
            self._impairments[node_name] = dict(params)
        self._backend.run_tc(cmds)

    def clear(self, *, outbound: str | None = None) -> None:
        """Remove impairments. ``outbound=<node>`` scopes to one direction."""
        from rangectl.link_properties import build_clear_cmds
        if self._backend is None:
            raise RuntimeError("Link not wired to backend; deploy the topology first")
        log.info("Link clear %s/%s <-> %s/%s (outbound=%s)",
                 self.if_a.node_name, self.if_a.interface_name,
                 self.if_b.node_name, self.if_b.interface_name, outbound)
        netns = getattr(self._backend, "_netns_name", None)
        cmds: list[list[str]] = []
        for node_name, (vm_id, mac) in self._targets(outbound):
            tap = self._backend._find_tap_for_mac(vm_id, mac)
            if tap:
                cmds += build_clear_cmds(tap, netns)
            self._impairments[node_name] = {}
        self._backend.run_tc(cmds)

    def _reapply_impairments(self) -> None:
        from rangectl.link_properties import build_netem_cmds
        netns = getattr(self._backend, "_netns_name", None)
        cmds: list[list[str]] = []
        for node_name, (vm_id, mac) in self._sides():
            params = self._impairments.get(node_name)
            if not params:
                continue
            tap = self._backend._find_tap_for_mac(vm_id, mac)
            if tap:
                cmds += build_netem_cmds(tap, netns, **params)
        if cmds:
            self._backend.run_tc(cmds)

    @property
    def impairments(self) -> dict:
        """Current impairment state, keyed by endpoint node name. A side with
        no impairment maps to an empty dict."""
        return {
            self.if_a.node_name: dict(self._impairments.get(self.if_a.node_name, {})),
            self.if_b.node_name: dict(self._impairments.get(self.if_b.node_name, {})),
        }


class Range:
    """Two faces of the same object:

    1. **Live handle** to a deployed topology, returned by ``Topology.deploy()``
       and ``Engine.deploy()`` (constructed with an explicit ``topology``).
    2. **Lifecycle base class** users subclass to declare infrastructure. The
       subclass sets a ``name`` class attribute and overrides ``define_nodes``,
       ``define_network``, ``install_software``, ``configure_os`` and (required)
       ``verify``. Calling ``deploy()`` runs them in order, wiring the Engine /
       backend / StateDB internally so users never touch them.

    ``internet`` is the outbound-internet policy ("none" or "full") and
    ``resources`` carries the cgroup limits the range was deployed with. Both
    only have an effect in namespace mode; in legacy mode they're recorded but
    the freeze/thaw/internet controls raise if invoked.
    """

    # Overridable by subclasses (lifecycle API).
    internet: str = "none"
    resources: Any = None

    def __init__(self, topology: Topology | None = None, internet: str | None = None,
                 resources: Any = None, persistent: bool = False) -> None:
        if topology is None:
            # Lifecycle (subclass) mode — build the internal Topology from the
            # subclass's `name` class attribute. The engine/backend are created
            # lazily in deploy().
            if type(self) is Range:
                raise TypeError(
                    "Range() requires a topology; subclass Range to use the "
                    "lifecycle API (define_nodes/define_network/verify)"
                )
            cls_name = getattr(type(self), "name", None)
            if not cls_name or not isinstance(cls_name, str):
                raise RuntimeError(
                    f"{type(self).__name__} must set a class attribute "
                    "name = '<range-name>'"
                )
            topology = Topology(cls_name)
            self._lifecycle = True
        else:
            self._lifecycle = False
        self.topology = topology
        self.internet = internet if internet is not None else type(self).internet
        self.resources = resources if resources is not None else type(self).resources
        # Persistent ranges (reconnected via Range.connect()) survive __exit__ —
        # leaving the `with` block disconnects rather than tearing the range
        # down. Ephemeral ranges (the default, returned by deploy()) auto-destroy
        # on exit, preserving the existing context-manager contract.
        self._persistent = persistent
        self._nodes: dict[str, LiveNode] = {}
        self._engine: Any = None
        self._db: Any = None
        self._backend: Any = None
        # Wired by Engine.deploy() in namespace mode so the runtime freeze/thaw
        # and internet controls know which cgroup / veth / subnet to act on.
        self._mgmt_subnet: str | None = None
        self._veth_host: str | None = None

    # --- lifecycle API (override in subclasses) ---------------------------

    @property
    def name(self) -> str:
        """Range name. A subclass overrides this with a string class attribute,
        which shadows this property; the live-handle form falls back to the
        topology name."""
        return self.topology.name

    def define_nodes(self) -> None:
        """Declare nodes via ``self.node(...)``. Override in subclass."""

    def define_network(self) -> None:
        """Declare links via ``self.link(...)``. Override in subclass."""

    def install_software(self) -> None:
        """Install packages / services on live nodes. Override in subclass."""

    def configure_os(self) -> None:
        """Apply routes/sysctls/files on live nodes. Override in subclass."""

    def verify(self) -> None:
        """Assert the range is working. **Required** — deploy raises if not
        overridden, forcing the author to define what 'working' means."""
        raise NotImplementedError("verify() must be overridden")

    def node(self, name: str, image: str | None = None,
             os_type: OSType | str | None = None, **kwargs: Any) -> Node:
        """Declare a node on the internal topology. Accepts ``os_type=`` (alias
        for the topology's ``os=``) for readability."""
        if os_type is not None:
            kwargs["os"] = os_type
        return self.topology.node(name, image=image, **kwargs)

    def deploy(self, *, backend: Any = None, db: Any = None,
               container_backend: Any = None, use_namespaces: bool | None = None,
               cleanup_on_fail: bool = True) -> "Range":
        """Stand up the lab. Runs the lifecycle in order:

            define_nodes -> define_network -> boot (engine, DAG waves) ->
            install_software -> configure_os -> verify -> READY

        With no ``backend`` a real LibvirtBackend + StateDB are created and the
        range runs in namespace mode. Tests inject a MockBackend + in-memory DB
        (which disables namespace mode by default)."""
        if not self._lifecycle:
            raise RuntimeError(
                "deploy() is only for Range subclasses; this is a live handle "
                "(use Topology.deploy()/Engine.deploy() instead)"
            )
        if type(self).verify is Range.verify:
            raise RuntimeError(
                f"{type(self).__name__}.verify() must be overridden before deploy()"
            )
        from rangectl.engine import Engine

        # 1. Declarative phase — populate the internal topology.
        self.define_nodes()
        self.define_network()

        # 2. Resolve backends. Injected backend == test mode == no namespaces.
        testing = backend is not None
        if use_namespaces is None:
            use_namespaces = not testing
        if backend is None:
            from rangectl.libvirt_backend import LibvirtBackend
            backend = LibvirtBackend()
        if db is None:
            from rangectl.state import StateDB
            db = StateDB()
        has_containers = any(n.is_container for n in self.topology._nodes.values())
        if has_containers and container_backend is None and not testing:
            from rangectl.container_backend import ContainerBackend
            container_backend = ContainerBackend()
        self.topology._backend = backend
        self.topology._db = db
        self.topology._container_backend = container_backend

        engine = Engine(backend, db, container_backend=container_backend,
                        use_namespaces=use_namespaces, resources=self.resources,
                        internet=self.internet)

        # 3. Boot — engine handles cloud-init, SSH wait, DAG wave ordering.
        rng = engine.deploy(self.topology, cleanup_on_fail=cleanup_on_fail)

        # Absorb the live state into self — the lab IS the topology object.
        self._nodes = rng._nodes
        self._engine = engine
        self._db = db
        self._backend = rng._backend
        self._mgmt_subnet = rng._mgmt_subnet
        self._veth_host = rng._veth_host
        self.topology._engine = engine
        # Rebind user-held Node attributes (self.router, ...) to their LiveNodes
        # so post-boot lifecycle hooks operate on live nodes.
        self._rebind_live()

        # 4. Post-boot lifecycle.
        self.install_software()
        self.configure_os()
        self.verify()
        log.info("Range '%s' READY", self.name)
        return self

    def _rebind_live(self) -> None:
        for attr, val in list(self.__dict__.items()):
            if isinstance(val, Node) and val.name in self._nodes:
                setattr(self, attr, self._nodes[val.name])

    # --- verify helpers (simple stubs that run real commands) -------------

    def expect_reach(self, node: Any, dest: str, via: Any = None) -> None:
        """Assert ``node`` can ping ``dest``. ``via`` is accepted for readability
        but not used (the route must already exist)."""
        live = node if isinstance(node, LiveNode) else self[node]
        result = live.exec(f"ping -c 1 -W 2 {dest}")
        if result.exit_code != 0:
            raise AssertionError(
                f"{live.name} cannot reach {dest}: {result.stderr.strip()}"
            )

    def __repr__(self) -> str:
        cls = type(self).__name__
        if self._nodes:
            status, count = "RUNNING", len(self._nodes)
        else:
            status, count = "DEFINED", len(self.topology._nodes)
        return (f'{cls}("{self.name}", status={status}, nodes={count}, '
                f'internet={self.internet})')

    def __enter__(self) -> Range:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._persistent:
            log.info("Range context exiting, leaving persistent range '%s' running",
                     self.topology.name)
            return
        log.info("Range context exiting, destroying topology '%s'", self.topology.name)
        self.destroy()

    def destroy(self) -> None:
        """Tear down the range — stop/undefine VMs, remove namespace/bridges,
        free state. Works on both deployed and reconnected ranges."""
        if self._engine is not None:
            self._engine.destroy(self.topology)
        else:
            self.topology.destroy()

    # --- persistent-range reconnect (Phase 13) ----------------------------

    @classmethod
    def connect(cls, name: str, db_path: str | None = None,
                range_dir: str | None = None) -> "Range":
        """Reconnect to a range deployed by an earlier process.

        Rebuilds the per-range backends, LiveNode handles, and an ns-mode
        Engine from persisted state (StateDB + range.json), so exec/upload/
        snapshot/restore/destroy all work cross-process. Raises
        ``RangeNotRunning`` if the topology is unknown/destroyed or its
        libvirtd/netns/socket are gone.
        """
        from rangectl.container_backend import ContainerBackend
        from rangectl.engine import Engine
        from rangectl.libvirt_backend import LibvirtBackend
        from rangectl.state import StateDB

        rdir = range_dir or supervisor.DEFAULT_RANGE_DIR
        db = StateDB(db_path)
        try:
            topo_row = db.get_topology(name)
            if topo_row is None or topo_row["status"] == "destroyed":
                raise RangeNotRunning(name, "no such running range in state DB")

            info = _read_range_json(name, rdir)
            if info is None:
                raise RangeNotRunning(name, f"range.json missing under {rdir}/{name}")
            if not _pid_alive(info["pid"]):
                raise RangeNotRunning(name, f"libvirtd pid {info['pid']} is not alive")
            if not _netns_exists(info["netns_name"]):
                raise RangeNotRunning(name, f"netns {info['netns_name']} is gone")
            if not Path(info["libvirt_socket"]).exists():
                raise RangeNotRunning(
                    name, f"libvirt socket {info['libvirt_socket']} missing")
        except BaseException:
            db.close()
            raise

        node_rows = db.list_nodes(name)
        has_containers = any(r["os_type"] == "container" for r in node_rows)

        # Per-range backends bound to the range's libvirt socket + netns.
        lvb = LibvirtBackend(libvirt_socket=info["libvirt_socket"],
                             netns_name=info["netns_name"])
        cb = ContainerBackend(netns_name=info["netns_name"]) if has_containers else None

        # Rebuild a Topology with Node objects so engine.destroy() can iterate
        # nodes and route each to the right backend (VM vs container).
        topology = Topology(name, backend=lvb, db=db, container_backend=cb)
        for r in node_rows:
            if r["os_type"] == "container":
                topology.node(r["name"], container=r["image"],
                              vcpu=r["vcpu"], memory=r["memory_mb"])
            else:
                topology.node(r["name"], image=r["image"], vcpu=r["vcpu"],
                              memory=r["memory_mb"], os=r["os_type"])

        # Rebuild an ns-mode Engine with the bookkeeping destroy() relies on.
        engine = Engine(lvb, db, container_backend=cb, use_namespaces=True)
        engine._range_info[name] = supervisor.RangeInfo(
            name=name, pid=info["pid"], netns_name=info["netns_name"],
            libvirt_socket=info["libvirt_socket"], mgmt_subnet=info["subnet"],
            veth_host=info["veth_host"], veth_ns=info["veth_ns"],
        )
        engine._range_backends[name] = lvb
        if cb is not None:
            engine._range_container_backends[name] = cb
        engine._link_bridges[name] = []

        rng = cls(topology, persistent=True)
        rng._engine = engine
        rng._db = db
        rng._backend = lvb
        rng._mgmt_subnet = info["subnet"]
        rng._veth_host = info["veth_host"]
        topology._engine = engine

        for r in node_rows:
            vm_id = r["vm_id"] or f"{name}-{r['name']}"
            mgmt_ip = r["mgmt_ip"]
            engine._vm_ids[(name, r["name"])] = vm_id
            engine._mgmt_ips[(name, r["name"])] = mgmt_ip
            if r["os_type"] == "container":
                backend: Any = cb
            else:
                backend = lvb
                is_vyos = r["os_type"] == "vyos"
                lvb.reconnect_vm(
                    vm_id, name, mgmt_ip,
                    ssh_user="vyos" if is_vyos else "ubuntu",
                    ssh_password="vyos" if is_vyos else None,
                )
            rng._nodes[r["name"]] = LiveNode(
                name=r["name"], mgmt_ip=mgmt_ip, topology_name=name,
                backend=backend, vm_id=vm_id, db=db,
                os_type=r["os_type"],
                ssh_user="vyos" if r["os_type"] == "vyos" else "ubuntu",
            )

        # Rebuild links from the DB so impair/clear/down/up work cross-process.
        # Each Link needs its bridge name, per-range backend, and (vm_id, mac)
        # endpoints — the same wiring Engine._wire_link does at deploy.
        from rangectl.engine import _mac_for
        for lk in db.list_links(name):
            if_a = InterfaceSpec(node_name=lk["node_a"],
                                 interface_name=lk["iface_a"], ip=lk["ip_a"])
            if_b = InterfaceSpec(node_name=lk["node_b"],
                                 interface_name=lk["iface_b"], ip=lk["ip_b"])
            link = Link(if_a, if_b, topology=topology)
            link._backend = lvb
            link._bridge_name = lk["bridge_name"]
            link._db = db
            link._topology_name = name
            link._is_up = bool(lk.get("is_up", 1))
            for side in (if_a, if_b):
                vm_id = engine._vm_ids[(name, side.node_name)]
                link._endpoints.append(
                    (vm_id, _mac_for(name, side.node_name, side.interface_name)))
            topology._links.append(link)

        log.info("Reconnected to range '%s' (%d nodes, %d links)",
                 name, len(node_rows), len(topology._links))
        return rng

    @classmethod
    def list(cls, db_path: str | None = None,
             range_dir: str | None = None) -> list[dict]:
        """All non-destroyed ranges with a liveness-checked status
        ('running', 'frozen', or 'orphaned')."""
        from rangectl.state import StateDB
        rdir = range_dir or supervisor.DEFAULT_RANGE_DIR
        db = StateDB(db_path)
        try:
            result = []
            for topo in db.list_topologies():
                if topo["status"] == "destroyed":
                    continue
                name = topo["name"]
                result.append({
                    "name": name,
                    "status": _range_status(name, rdir),
                    "node_count": len(db.list_nodes(name)),
                    "mgmt_subnet": topo["mgmt_subnet"],
                    "created_at": topo.get("created_at"),
                })
            return result
        finally:
            db.close()

    @classmethod
    def cleanup(cls, name: str, db_path: str | None = None,
                range_dir: str | None = None) -> None:
        """Force-remove an orphaned range's state when no live range remains:
        kill any surviving libvirtd, tear down the netns + range dir + cgroup,
        reclaim the VM overlay/seed disk, and clear the DB rows + mgmt subnet
        allocation."""
        from rangectl.engine import cleanup_vm_storage
        from rangectl.state import StateDB
        rdir = range_dir or supervisor.DEFAULT_RANGE_DIR
        log.info("Cleaning up range '%s'", name)
        try:
            supervisor.destroy_range(name, range_dir=rdir)
        except Exception as exc:
            log.warning("cleanup: destroy_range failed: %s", exc)
        try:
            cgroup.destroy_cgroup(name)
        except Exception as exc:
            log.warning("cleanup: destroy_cgroup failed: %s", exc)
        # Reclaim overlay/seed disk (outside the range dir, so destroy_range
        # doesn't touch it) — otherwise cleaning an orphan leaks exactly that
        # disk. Best-effort; no-op if already gone.
        try:
            cleanup_vm_storage(name)
        except Exception as exc:
            log.warning("cleanup: vm storage failed: %s", exc)
        db = StateDB(db_path)
        try:
            db.free_mgmt_subnet(name)
            db.delete_topology(name)
        finally:
            db.close()

    def __getitem__(self, node_name: str) -> LiveNode:
        return self._nodes[node_name]

    def link(self, a: Any, b: Any, **kwargs: Any) -> Link:
        """Two modes, picked by argument type:

        - **define** (``InterfaceSpec`` args): create a link on the internal
          topology — ``self.link(router.eth1["10.0.1.1/24"], t.eth1["10.0.1.2/24"])``.
          Extra kwargs (``latency=``, ``bandwidth=``, ``loss=``, ...) set
          definition-time impairment defaults.
        - **lookup** (node-name strings): return the existing link between two
          nodes for fault injection — ``lab.link("router", "target").down()``.
        """
        if isinstance(a, InterfaceSpec):
            return self.topology.link(a, b, **kwargs)
        log.info("Looking up link between %s and %s", a, b)
        for lnk in self.topology._links:
            if {lnk.if_a.node_name, lnk.if_b.node_name} == {a, b}:
                return lnk
        raise KeyError(f"No link between {a} and {b}")

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

    # --- runtime resource + internet controls (namespace mode) ------------

    def freeze(self) -> None:
        """Atomically pause every process in the range's cgroup (VMs included).

        The freezer suspends all tasks in ``rangectl-<name>`` at once, so VMs
        stop executing entirely — they go unresponsive until ``thaw()``.
        """
        from rangectl import cgroup
        log.info("Freezing range '%s'", self.topology.name)
        cgroup.freeze(self.topology.name)
        if self._db is not None:
            self._db.log_event(self.topology.name, None, "info", "range frozen")

    def thaw(self) -> None:
        """Resume a frozen range — all processes continue where they left off."""
        from rangectl import cgroup
        log.info("Thawing range '%s'", self.topology.name)
        cgroup.thaw(self.topology.name)
        if self._db is not None:
            self._db.log_event(self.topology.name, None, "info", "range thawed")

    def enable_internet(self) -> None:
        """Grant the range outbound internet (MASQUERADE via the host uplink)."""
        from rangectl import internet as internet_mod
        if self._veth_host is None or self._mgmt_subnet is None:
            raise RuntimeError(
                "enable_internet() requires a namespace-mode range "
                "(deploy with use_namespaces=True)"
            )
        from rangectl.networking import MGMT_NS
        log.info("Enabling internet for range '%s'", self.topology.name)
        internet_mod.enable_internet(self.topology.name, self._mgmt_subnet,
                                     self._veth_host, netns=MGMT_NS)
        self.internet = "full"
        if self._db is not None:
            self._db.log_event(self.topology.name, None, "info", "internet enabled")

    def disable_internet(self) -> None:
        """Revoke the range's outbound internet, removing only its own rules."""
        from rangectl import internet as internet_mod
        if self._veth_host is None or self._mgmt_subnet is None:
            raise RuntimeError(
                "disable_internet() requires a namespace-mode range "
                "(deploy with use_namespaces=True)"
            )
        from rangectl.networking import MGMT_NS
        log.info("Disabling internet for range '%s'", self.topology.name)
        internet_mod.disable_internet(self.topology.name, self._mgmt_subnet,
                                      self._veth_host, netns=MGMT_NS)
        self.internet = "none"
        if self._db is not None:
            self._db.log_event(self.topology.name, None, "info", "internet disabled")


class LiveNode:
    """Handle to a running node within a deployed topology."""

    def __init__(self, name: str, mgmt_ip: str, topology_name: str,
                 backend: Any = None, vm_id: str | None = None,
                 db: Any = None, os_type: OSType | str = OSType.LINUX,
                 ssh_user: str = "ubuntu") -> None:
        self.name = name
        self.mgmt_ip = mgmt_ip
        self.topology_name = topology_name
        self.ssh_user = ssh_user
        self._backend = backend
        self._vm_id = vm_id
        self._db = db
        # OS-specific operations (route, sysctl, packages, ...) route through
        # this driver, which translates them to the right commands for the OS.
        self._driver = make_driver(os_type, backend, vm_id)

    def __repr__(self) -> str:
        return (f'LiveNode("{self.name}", ip={self.mgmt_ip}, '
                f'vm_id={self._vm_id})')

    # --- command + file convenience (Phase 15) ----------------------------

    def run(self, cmd: str, check: bool = True) -> str:
        """Run ``cmd`` and return stdout. Raises on non-zero exit unless
        ``check=False``. (``exec()`` returns the full ExecResult instead.)"""
        result = self.exec(cmd)
        if check and result.exit_code != 0:
            raise RuntimeError(
                f"command failed (exit {result.exit_code}) on {self.name}: "
                f"{result.stderr.strip()}"
            )
        return result.stdout

    def put(self, src: str, dst: str) -> None:
        """Alias for ``upload`` — copy a single file to the node."""
        self.upload(src, dst)

    def put_dir(self, src: str, dst: str) -> None:
        """Copy a directory tree to the node (via the OS driver)."""
        self._driver.put_dir(src, dst)

    # --- OS-abstracted operations (routed through the driver) -------------

    def route(self, dest: str, via: str) -> None:
        self._driver.add_route(dest, via)

    def sysctl(self, key: str, value: Any) -> None:
        self._driver.set_sysctl(key, value)

    def packages(self, pkgs: list[str]) -> None:
        self._driver.install_packages(pkgs)

    def service(self, name: str, enabled: bool = True) -> None:
        if enabled:
            self._driver.enable_service(name)

    def firewall_allow(self, port: int, proto: str = "tcp") -> None:
        self._driver.firewall_allow(port, proto)

    def check_port(self, port: int, host: str = "127.0.0.1") -> bool:
        """Verify a TCP port is open on the node (stub used by verify())."""
        result = self.exec(
            f"bash -c 'exec 3<>/dev/tcp/{host}/{port}' 2>/dev/null")
        if result.exit_code != 0:
            raise AssertionError(
                f"port {port} not open on {self.name} ({host})")
        return True

    # --- power operations -------------------------------------------------

    def start(self) -> None:
        if self._backend is None or self._vm_id is None:
            raise RuntimeError(f"LiveNode {self.name!r} not bound to a backend")
        self._backend.start(self._vm_id)

    def stop(self) -> None:
        if self._backend is None or self._vm_id is None:
            raise RuntimeError(f"LiveNode {self.name!r} not bound to a backend")
        self._backend.stop(self._vm_id)

    def restart(self) -> None:
        self.stop()
        self.start()

    @property
    def status(self) -> str:
        """Power state of the node ('running', 'shut off', 'paused', ...)."""
        if self._backend is None or self._vm_id is None:
            raise RuntimeError(f"LiveNode {self.name!r} not bound to a backend")
        return self._backend.status(self._vm_id)

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
