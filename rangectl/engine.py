from __future__ import annotations
import logging
from threading import Thread

from rangectl.backend import Backend
from rangectl.state import StateDB
from rangectl.topology import Range, LiveNode, Node, Topology
from rangectl.types import CycleError, NodeState, ResourceError

log = logging.getLogger(__name__)


class Engine:
    """Orchestrates topology deployment using the dependency DAG."""

    def __init__(self, backend: Backend, db: StateDB) -> None:
        self._backend = backend
        self._db = db

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

        log.info("Step 1: Validate resources")
        self.validate_resources(topology)

        log.info("Step 2: Allocate mgmt subnet")
        mgmt_subnet = self._db.allocate_mgmt_subnet(topology.name)

        log.info("Step 3: Create mgmt bridge")
        mgmt_bridge = f"rangectl-mgmt-{topology.name}"
        self._backend.create_bridge(mgmt_bridge)

        log.info("Step 4: Compute waves")
        waves = self.compute_waves(topology)

        log.info("Step 5: Deploy waves")
        for i, wave in enumerate(waves):
            log.info("Wave %d: %s", i + 1, [n.name for n in wave])
            self._deploy_wave(topology, wave, mgmt_subnet, mgmt_bridge)

        log.info("Step 6: Wire topology links")
        for link in topology._links:
            self._wire_link(topology, link)

        log.info("Step 7: Run dependency injection")
        for node in topology._nodes.values():
            self._inject_dependencies(topology, node)

        log.info("Deployment complete")
        raise NotImplementedError

    def _deploy_wave(self, topology: Topology, wave: list[Node],
                     mgmt_subnet: str, mgmt_bridge: str) -> None:
        threads: list[Thread] = []
        for node in wave:
            t = Thread(
                target=self._deploy_node,
                args=(topology, node, mgmt_subnet, mgmt_bridge),
                name=f"deploy-{topology.name}-{node.name}",
            )
            threads.append(t)
            t.start()

        for t in threads:
            t.join()
            log.info("Thread %s completed", t.name)

    def _deploy_node(self, topology: Topology, node: Node,
                     mgmt_subnet: str, mgmt_bridge: str) -> None:
        log.info("[%s/%s] Creating VM", topology.name, node.name)
        log.info("[%s/%s] Creating COW overlay from base image '%s'",
                 topology.name, node.name, node.image)
        log.info("[%s/%s] Attaching mgmt interface to %s", topology.name, node.name, mgmt_bridge)
        log.info("[%s/%s] Starting VM", topology.name, node.name)
        log.info("[%s/%s] Waiting for readiness (L2: ping)", topology.name, node.name)
        if node.ready_when:
            log.info("[%s/%s] Waiting for readiness (L3: %s)",
                     topology.name, node.name, node.ready_when.probe_type)
        log.info("[%s/%s] Node ready", topology.name, node.name)
        raise NotImplementedError

    def _wire_link(self, topology: Topology, link) -> None:
        log.info("[%s] Wiring link: %s/%s <-> %s/%s",
                 topology.name,
                 link.if_a.node_name, link.if_a.interface_name,
                 link.if_b.node_name, link.if_b.interface_name)
        # create bridge for this link, attach tap interfaces on both sides
        raise NotImplementedError

    def _inject_dependencies(self, topology: Topology, node: Node) -> None:
        log.info("[%s/%s] Injecting dependencies", topology.name, node.name)
        if node._packages:
            log.info("[%s/%s] packages: %s (engine resolves package manager from image/OS)",
                     topology.name, node.name, node._packages)
        for ps_cmd in node._powershell_commands:
            log.info("[%s/%s] powershell: %s", topology.name, node.name, ps_cmd)
        for inst in node._installs:
            log.info("[%s/%s] install %s from %s", topology.name, node.name, inst.name, inst.src)
        for fn in node._configure_fns:
            log.info("[%s/%s] configure: %s()", topology.name, node.name, fn.__name__)
        for svc in node._services:
            log.info("[%s/%s] service: %s (enabled=%s)", topology.name, node.name, svc.name, svc.enabled)
        raise NotImplementedError

    def destroy(self, topology: Topology) -> None:
        log.info("Destroying topology '%s'", topology.name)
        log.info("Step 1: Stop and destroy all VMs")
        log.info("Step 2: Delete topology bridges")
        log.info("Step 3: Delete mgmt bridge")
        log.info("Step 4: Delete COW overlays")
        log.info("Step 5: Free mgmt subnet")
        log.info("Step 6: Update DB")
        raise NotImplementedError
