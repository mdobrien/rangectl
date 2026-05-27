from __future__ import annotations
import hashlib
import logging
from pathlib import Path
from threading import Lock, Thread

from rangectl.backend import Backend
from rangectl.networking import allocate_mgmt_ip, bridge_name, mgmt_bridge_name
from rangectl.state import StateDB
from rangectl.topology import LiveNode, Node, Range, Topology
from rangectl.types import (
    CycleError,
    InterfaceSpec,
    NodeState,
    OSType,
    ResourceError,
    VMSpec,
    transition_node_state,
)

log = logging.getLogger(__name__)

OVERLAY_ROOT = Path("~/.rangectl/overlays").expanduser()


def _mac_for(topo_name: str, node_name: str, suffix: str) -> str:
    """Deterministic locally-administered MAC derived from names."""
    h = hashlib.sha1(f"{topo_name}/{node_name}/{suffix}".encode()).hexdigest()
    return "52:54:00:" + ":".join(h[i:i + 2] for i in (0, 2, 4))


class Engine:
    """Orchestrates topology deployment using the dependency DAG."""

    def __init__(self, backend: Backend, db: StateDB) -> None:
        self._backend = backend
        self._db = db
        # Per-deploy tracking — populated during deploy(), consumed in destroy().
        self._vm_ids: dict[tuple[str, str], str] = {}
        self._mgmt_ips: dict[tuple[str, str], str] = {}
        self._link_bridges: dict[str, list[str]] = {}
        self._lock = Lock()

    def validate_resources(self, topology: Topology) -> None:
        log.info("Validating host resources for topology '%s'", topology.name)
        total_vcpu = sum(n.vcpu for n in topology._nodes.values())
        total_memory = sum(n.memory for n in topology._nodes.values())
        log.info("Required: %d vCPU, %d MB memory", total_vcpu, total_memory)
        resources = self._backend.host_resources()
        log.info("Available: %d vCPU, %d MB memory",
                 resources.available_vcpu, resources.available_memory_mb)
        if total_vcpu > resources.available_vcpu:
            raise ResourceError(
                f"insufficient vcpu: need {total_vcpu}, available {resources.available_vcpu}"
            )
        if total_memory > resources.available_memory_mb:
            raise ResourceError(
                f"insufficient memory: need {total_memory} MB, "
                f"available {resources.available_memory_mb} MB"
            )

    def compute_waves(self, topology: Topology) -> list[list[Node]]:
        log.info("Computing deploy waves for topology '%s'", topology.name)
        nodes = list(topology._nodes.values())
        node_names = {n.name for n in nodes}
        remaining_deps: dict[str, set[str]] = {
            n.name: {d.name for d in n.depends_on if d.name in node_names}
            for n in nodes
        }
        waves: list[list[Node]] = []
        placed: set[str] = set()
        while len(placed) < len(nodes):
            wave = [n for n in nodes
                    if n.name not in placed and remaining_deps[n.name] <= placed]
            if not wave:
                unresolved = [n.name for n in nodes if n.name not in placed]
                raise CycleError(f"dependency cycle among nodes: {unresolved}")
            waves.append(wave)
            placed.update(n.name for n in wave)
        return waves

    def deploy(self, topology: Topology, cleanup_on_fail: bool = True) -> Range:
        log.info("Engine deploying topology '%s'", topology.name)

        # Reset per-deploy bookkeeping for this topology.
        self._link_bridges[topology.name] = []
        for node in topology._nodes.values():
            self._vm_ids.pop((topology.name, node.name), None)
            self._mgmt_ips.pop((topology.name, node.name), None)

        log.info("Step 1: Validate resources")
        self.validate_resources(topology)

        log.info("Step 2: Allocate mgmt subnet")
        mgmt_subnet = self._db.allocate_mgmt_subnet(topology.name)

        log.info("Step 3: Create mgmt bridge")
        mgmt_bridge = mgmt_bridge_name(topology.name)
        self._backend.create_bridge(mgmt_bridge)

        # Save topology row now that we have subnet + bridge.
        self._db.save_topology(
            name=topology.name,
            status="deploying",
            mgmt_subnet=mgmt_subnet,
            mgmt_bridge=mgmt_bridge,
        )
        self._db.log_event(topology.name, None, "info",
                           f"mgmt subnet {mgmt_subnet} bridge {mgmt_bridge}")

        # Stable index for mgmt IP assignment based on declaration order.
        node_index: dict[str, int] = {
            name: i for i, name in enumerate(topology._nodes.keys())
        }

        log.info("Step 4: Compute waves")
        waves = self.compute_waves(topology)

        log.info("Step 5: Deploy waves")
        for i, wave in enumerate(waves):
            log.info("Wave %d: %s", i + 1, [n.name for n in wave])
            self._deploy_wave(topology, wave, mgmt_subnet, mgmt_bridge, node_index)

        log.info("Step 6: Wire topology links")
        for link_idx, link in enumerate(topology._links):
            self._wire_link(topology, link, link_idx)

        log.info("Step 7: Run dependency injection")
        for node in topology._nodes.values():
            self._inject_dependencies(topology, node)

        self._db.save_topology(
            name=topology.name, status="running",
            mgmt_subnet=mgmt_subnet, mgmt_bridge=mgmt_bridge,
        )
        self._db.log_event(topology.name, None, "info", "deployment complete")

        rng = Range(topology)
        for node in topology._nodes.values():
            mgmt_ip = self._mgmt_ips[(topology.name, node.name)]
            rng._nodes[node.name] = LiveNode(
                name=node.name,
                mgmt_ip=mgmt_ip,
                topology_name=topology.name,
                backend=self._backend,
                vm_id=self._vm_ids[(topology.name, node.name)],
                db=self._db,
            )
        rng._engine = self
        rng._db = self._db
        rng._backend = self._backend
        log.info("Deployment complete")
        return rng

    def _deploy_wave(self, topology: Topology, wave: list[Node],
                     mgmt_subnet: str, mgmt_bridge: str,
                     node_index: dict[str, int]) -> None:
        threads: list[Thread] = []
        errors: list[BaseException] = []

        def _runner(n: Node) -> None:
            try:
                self._deploy_node(topology, n, mgmt_subnet, mgmt_bridge, node_index[n.name])
            except BaseException as exc:  # capture for join-time re-raise
                with self._lock:
                    errors.append(exc)

        for node in wave:
            t = Thread(target=_runner, args=(node,),
                       name=f"deploy-{topology.name}-{node.name}")
            threads.append(t)
            t.start()

        for t in threads:
            t.join()
            log.info("Thread %s completed", t.name)

        if errors:
            raise errors[0]

    def _deploy_node(self, topology: Topology, node: Node,
                     mgmt_subnet: str, mgmt_bridge: str,
                     index: int) -> None:
        log.info("[%s/%s] Provisioning", topology.name, node.name)
        node.state = transition_node_state(node.state, NodeState.PROVISIONING)
        self._db.save_node(
            topology_name=topology.name,
            name=node.name,
            image=node.image,
            vcpu=node.vcpu,
            memory_mb=node.memory,
            os_type=node.os_type.value,
            state=node.state.value,
        )
        self._db.log_event(topology.name, node.name, "info", "provisioning")

        # Resolve image — in unit tests the image name is opaque; just pass through.
        image_record = self._db.get_image(node.image)
        image_path = image_record["path"] if image_record else node.image

        overlay_path = str(OVERLAY_ROOT / topology.name / f"{node.name}.qcow2")
        self._backend.create_overlay(image_path, overlay_path)

        mgmt_iface = InterfaceSpec(node_name=node.name, interface_name="eth0")
        topo_ifaces = [
            ifs for name, ifs in node._interfaces.items() if name != "eth0"
        ]
        spec = VMSpec(
            name=f"{topology.name}-{node.name}",
            image=node.image,
            vcpu=node.vcpu,
            memory=node.memory,
            os_type=node.os_type,
            interfaces=[mgmt_iface, *topo_ifaces],
            overlay_path=overlay_path,
        )
        vm_id = self._backend.create_vm(spec)
        with self._lock:
            self._vm_ids[(topology.name, node.name)] = vm_id

        self._backend.start(vm_id)

        mgmt_ip = allocate_mgmt_ip(mgmt_subnet, index)
        with self._lock:
            self._mgmt_ips[(topology.name, node.name)] = mgmt_ip

        self._backend.attach_interface(vm_id, mgmt_bridge,
                                       _mac_for(topology.name, node.name, "mgmt"))

        # Update DB row with mgmt_ip now that it's assigned.
        self._db.save_node(
            topology_name=topology.name,
            name=node.name,
            image=node.image,
            vcpu=node.vcpu,
            memory_mb=node.memory,
            os_type=node.os_type.value,
            state=node.state.value,
            mgmt_ip=mgmt_ip,
        )

        node.state = transition_node_state(node.state, NodeState.READY)
        self._db.update_node_state(topology.name, node.name, node.state.value)
        self._db.log_event(topology.name, node.name, "info",
                           f"ready, mgmt_ip={mgmt_ip}")

    def _wire_link(self, topology: Topology, link, link_index: int) -> None:
        br = bridge_name(topology.name, link_index)
        log.info("[%s] Wiring link %d: %s/%s <-> %s/%s via %s",
                 topology.name, link_index,
                 link.if_a.node_name, link.if_a.interface_name,
                 link.if_b.node_name, link.if_b.interface_name, br)

        self._backend.create_bridge(br)
        self._db._conn.execute(
            "INSERT INTO bridges (topology_name, name, bridge_type) VALUES (?, ?, ?)",
            (topology.name, br, "topology"),
        )
        self._db._conn.execute(
            "INSERT INTO links (topology_name, node_a, iface_a, ip_a, "
            "node_b, iface_b, ip_b, bridge_name) VALUES (?,?,?,?,?,?,?,?)",
            (topology.name, link.if_a.node_name, link.if_a.interface_name, link.if_a.ip,
             link.if_b.node_name, link.if_b.interface_name, link.if_b.ip, br),
        )
        self._db._conn.commit()
        self._link_bridges[topology.name].append(br)

        # Wire Link with backend/bridge refs so Link.down()/up() can work later.
        link._backend = self._backend
        link._bridge_name = br
        link._db = self._db
        link._topology_name = topology.name

        for side in (link.if_a, link.if_b):
            vm_id = self._vm_ids[(topology.name, side.node_name)]
            mac = _mac_for(topology.name, side.node_name, side.interface_name)
            self._backend.attach_interface(vm_id, br, mac)

        for side in (link.if_a, link.if_b):
            node = topology._nodes[side.node_name]
            if node.state == NodeState.READY:
                node.state = transition_node_state(node.state, NodeState.LINKED)
                self._db.update_node_state(topology.name, node.name, node.state.value)

    def _inject_dependencies(self, topology: Topology, node: Node) -> None:
        log.info("[%s/%s] Injecting dependencies", topology.name, node.name)
        # Nodes without links never reach LINKED via _wire_link — bridge that gap.
        if node.state == NodeState.READY:
            node.state = transition_node_state(node.state, NodeState.LINKED)
            self._db.update_node_state(topology.name, node.name, node.state.value)

        vm_id = self._vm_ids[(topology.name, node.name)]
        mgmt_ip = self._mgmt_ips[(topology.name, node.name)]
        live = LiveNode(
            name=node.name,
            mgmt_ip=mgmt_ip,
            topology_name=topology.name,
            backend=self._backend,
            vm_id=vm_id,
        )

        # 1. packages — Linux uses apt-get; Windows uses powershell commands.
        if node.os_type == OSType.LINUX and node._packages:
            pkg_list = " ".join(node._packages)
            self._backend.exec(vm_id, f"apt-get install -y {pkg_list}")
        for ps_cmd in node._powershell_commands:
            self._backend.exec(vm_id, f"powershell -Command {ps_cmd}")

        # 2. files — upload each registered file.
        for dst, src in node._files:
            self._backend.upload(vm_id, src, dst)

        # 3. installs — upload source, run install, optionally verify.
        for inst in node._installs:
            remote_src = f"/tmp/{Path(inst.src).name}"
            self._backend.upload(vm_id, inst.src, remote_src)
            self._backend.exec(vm_id, inst.install_cmd)
            if inst.verify_cmd:
                self._backend.exec(vm_id, inst.verify_cmd)

        # 4. configure functions — pass a LiveNode bound to the backend.
        for fn in node._configure_fns:
            fn(live)

        # 5. services — enable then start.
        for svc in node._services:
            if svc.enabled:
                self._backend.exec(vm_id, f"systemctl enable {svc.name}")
            start_cmd = svc.start_cmd or f"systemctl start {svc.name}"
            self._backend.exec(vm_id, start_cmd)

        if node.state == NodeState.LINKED:
            node.state = transition_node_state(node.state, NodeState.RUNNING)
            self._db.update_node_state(topology.name, node.name, node.state.value)
        self._db.log_event(topology.name, node.name, "info", "running")

    def destroy(self, topology: Topology) -> None:
        log.info("Destroying topology '%s'", topology.name)
        self._db.log_event(topology.name, None, "info", "destroying")

        for node in topology._nodes.values():
            vm_id = self._vm_ids.get((topology.name, node.name))
            if vm_id is None:
                continue
            try:
                node.state = transition_node_state(node.state, NodeState.DESTROYING)
            except Exception:
                node.state = NodeState.DESTROYING
            self._db.update_node_state(topology.name, node.name, node.state.value)
            self._backend.stop(vm_id)
            self._backend.destroy(vm_id)
            node.state = transition_node_state(node.state, NodeState.DESTROYED)
            self._db.update_node_state(topology.name, node.name, node.state.value)
            self._vm_ids.pop((topology.name, node.name), None)

        for br in self._link_bridges.get(topology.name, []):
            self._backend.delete_bridge(br)
        self._link_bridges.pop(topology.name, None)

        self._backend.delete_bridge(mgmt_bridge_name(topology.name))

        self._db.free_mgmt_subnet(topology.name)
        self._db.delete_topology(topology.name)
        log.info("Destroy complete for '%s'", topology.name)
