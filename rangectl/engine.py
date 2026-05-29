from __future__ import annotations
import hashlib
import logging
import os
from pathlib import Path
from threading import Lock, Thread

from rangectl import cgroup, supervisor
from rangectl.backend import Backend
from rangectl.cgroup import Resources
from rangectl.cloudinit import create_seed_iso
from rangectl.libvirt_backend import LibvirtBackend
from rangectl.networking import (
    allocate_mgmt_ip,
    bridge_name,
    mgmt_bridge_name,
    mgmt_host_ip,
    ns_bridge_name,
    ns_mgmt_bridge_name,
)
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

# Overlays and seed ISOs live under libvirt's default image dir so the stock
# AppArmor profile (which whitelists /var/lib/libvirt/images/**) lets qemu open
# them without a custom rule. Fall back to ~/.rangectl when running unit tests
# that won't actually launch qemu.
def _state_root() -> Path:
    libvirt = Path("/var/lib/libvirt/images")
    if libvirt.exists() and os.access(libvirt, os.W_OK):
        return libvirt / "rangectl"
    return Path("~/.rangectl").expanduser()


OVERLAY_ROOT = _state_root() / "overlays"
SEED_ROOT = _state_root() / "seeds"
MGMT_CIDR = "24"


def _mac_for(topo_name: str, node_name: str, suffix: str) -> str:
    """Deterministic locally-administered MAC derived from names."""
    h = hashlib.sha1(f"{topo_name}/{node_name}/{suffix}".encode()).hexdigest()
    return "52:54:00:" + ":".join(h[i:i + 2] for i in (0, 2, 4))


def _guest_iface_name(image_os_type: str, slot_index: int) -> str:
    """Target interface name the engine/bootstrap will use for the i-th NIC.

    This is the post-bootstrap, configure-time name. Backends are responsible
    for any OS-specific renaming needed to make the kernel device match
    (e.g. VyOS's initramfs udev renames eth*→e<IFINDEX> on first boot; the
    LibvirtBackend bootstrap renames them back to eth<N> before running the
    VyOS configure CLI, and pins the mapping with `hw-id` so it persists).
    """
    if image_os_type == "vyos":
        return f"eth{slot_index}"
    return f"if{slot_index}"


class Engine:
    """Orchestrates topology deployment using the dependency DAG."""

    def __init__(self, backend: Backend, db: StateDB,
                 container_backend: Backend | None = None,
                 use_namespaces: bool = False,
                 resources: Resources | None = None) -> None:
        self._backend = backend
        self._container_backend = container_backend
        self._db = db
        # When True, each range gets its own PID/net/mount namespaces and a
        # per-range libvirtd (see supervisor.py). VM and bridge ops route into
        # that namespace; bridge names become clean (mgmt-br, data-0, …). When
        # False the engine behaves exactly as the legacy host-level deploy.
        self._use_namespaces = use_namespaces
        self._resources = resources
        # Per-deploy tracking — populated during deploy(), consumed in destroy().
        self._vm_ids: dict[tuple[str, str], str] = {}
        self._mgmt_ips: dict[tuple[str, str], str] = {}
        self._link_bridges: dict[str, list[str]] = {}
        # Namespace-mode bookkeeping, keyed by topology name.
        self._range_info: dict[str, supervisor.RangeInfo] = {}
        self._range_backends: dict[str, Backend] = {}
        self._range_container_backends: dict[str, Backend] = {}
        self._cgroup_paths: dict[str, str] = {}
        self._lock = Lock()

    # --- backend / bridge-name resolution (namespace-aware) ---

    def _vm_backend(self, topology_name: str) -> Backend:
        """The backend that drives VM nodes for this topology.

        In namespace mode this is the per-range LibvirtBackend (bound to the
        range's libvirt socket + netns); otherwise the template backend.
        """
        if self._use_namespaces:
            return self._range_backends[topology_name]
        return self._backend

    def _backend_for(self, topology_name: str, node: Node) -> Backend:
        """Pick the backend for this node based on type (image vs container)."""
        if node.is_container:
            if self._container_backend is None:
                raise RuntimeError(
                    f"node {node.name!r} is a container but no container_backend "
                    "was passed to Engine()"
                )
            # In namespace mode containers wire into the range's netns, so use
            # the per-range container backend (created during _setup_namespace).
            if self._use_namespaces:
                return self._range_container_backends[topology_name]
            return self._container_backend
        return self._vm_backend(topology_name)

    def _mgmt_bridge_name(self, topology_name: str) -> str:
        if self._use_namespaces:
            return ns_mgmt_bridge_name()
        return mgmt_bridge_name(topology_name)

    def _link_bridge_name(self, topology_name: str, index: int) -> str:
        if self._use_namespaces:
            return ns_bridge_name(index)
        return bridge_name(topology_name, index)

    def _make_range_backend(self, info: supervisor.RangeInfo) -> Backend:
        """Create a LibvirtBackend bound to a range's socket + netns, reusing
        the template backend's ssh settings."""
        return LibvirtBackend(
            ssh_user=getattr(self._backend, "_ssh_user", "ubuntu"),
            ssh_ready_timeout=getattr(self._backend, "_ssh_ready_timeout", 180),
            libvirt_socket=info.libvirt_socket,
            netns_name=info.netns_name,
        )

    def _make_range_container_backend(self, info: supervisor.RangeInfo) -> Backend:
        """Create a ContainerBackend that wires container veths into the
        range's netns instead of the host namespace."""
        from rangectl.container_backend import ContainerBackend
        return ContainerBackend(netns_name=info.netns_name)

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

    def _link_index_for(self, topology: Topology, node_name: str,
                        iface_name: str) -> int | None:
        for i, link in enumerate(topology._links):
            for side in (link.if_a, link.if_b):
                if side.node_name == node_name and side.interface_name == iface_name:
                    return i
        return None

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
        host_ip = mgmt_host_ip(mgmt_subnet)

        # In namespace mode, stand up the range's PID/net/mount namespaces and
        # per-range libvirtd before any VM/bridge ops. The mgmt bridge + host
        # gateway IP are created inside the netns by the supervisor, so the
        # engine does NOT create them here.
        if self._use_namespaces:
            self._setup_namespace(topology.name, mgmt_subnet)

        vm_backend = self._vm_backend(topology.name)

        log.info("Step 3: Create mgmt bridge")
        mgmt_bridge = self._mgmt_bridge_name(topology.name)
        if not self._use_namespaces:
            vm_backend.create_bridge(mgmt_bridge)
            # Give the host an IP on the mgmt bridge so it can SSH into VMs.
            vm_backend.assign_host_ip(mgmt_bridge, host_ip, MGMT_CIDR)

        # Save topology row now that we have subnet + bridge.
        self._db.save_topology(
            name=topology.name,
            status="deploying",
            mgmt_subnet=mgmt_subnet,
            mgmt_bridge=mgmt_bridge,
        )
        self._db.log_event(topology.name, None, "info",
                           f"mgmt subnet {mgmt_subnet} bridge {mgmt_bridge}")

        log.info("Step 4: Create topology link bridges")
        for i, _ in enumerate(topology._links):
            br = self._link_bridge_name(topology.name, i)
            vm_backend.create_bridge(br)
            self._link_bridges[topology.name].append(br)
            self._db._conn.execute(
                "INSERT INTO bridges (topology_name, name, bridge_type) VALUES (?, ?, ?)",
                (topology.name, br, "topology"),
            )
        self._db._conn.commit()

        log.info("Step 5: Pre-allocate mgmt IPs")
        node_index: dict[str, int] = {
            name: i for i, name in enumerate(topology._nodes.keys())
        }
        for node in topology._nodes.values():
            ip = allocate_mgmt_ip(mgmt_subnet, node_index[node.name])
            self._mgmt_ips[(topology.name, node.name)] = ip

        # Eagerly request the topology ssh pubkey so it's available to all
        # parallel deploy threads (and to seed ISO generation). Must come from
        # the VM backend so its per-topology key store is populated for the
        # SSH/VyOS-bootstrap paths that run later on the same instance.
        ssh_pubkey = vm_backend.ssh_pubkey(topology.name)

        log.info("Step 6: Compute waves")
        waves = self.compute_waves(topology)

        log.info("Step 7: Deploy waves")
        for i, wave in enumerate(waves):
            log.info("Wave %d: %s", i + 1, [n.name for n in wave])
            self._deploy_wave(topology, wave, mgmt_subnet, mgmt_bridge,
                              ssh_pubkey, host_ip)

        log.info("Step 8: Wire topology links (DB + attach for hot-attach back-ends)")
        for link_idx, link in enumerate(topology._links):
            self._wire_link(topology, link, link_idx)

        log.info("Step 9: Run dependency injection")
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
                backend=self._backend_for(topology.name, node),
                vm_id=self._vm_ids[(topology.name, node.name)],
                db=self._db,
            )
        rng._engine = self
        rng._db = self._db
        rng._backend = vm_backend
        log.info("Deployment complete")
        return rng

    def _setup_namespace(self, topology_name: str, mgmt_subnet: str) -> None:
        """Create cgroup (if resources requested), the range namespaces +
        per-range libvirtd, and the per-range LibvirtBackend bound to them."""
        cgroup_path: str | None = None
        if self._resources is not None:
            cgroup_path = cgroup.create_cgroup(topology_name, self._resources)
            self._cgroup_paths[topology_name] = cgroup_path

        info = supervisor.create_range(topology_name, mgmt_subnet)
        self._range_info[topology_name] = info
        self._range_backends[topology_name] = self._make_range_backend(info)
        if self._container_backend is not None:
            self._range_container_backends[topology_name] = (
                self._make_range_container_backend(info)
            )

        # Place libvirtd (and thus every QEMU child it spawns) into the cgroup.
        if cgroup_path is not None:
            cgroup.write_pid(cgroup_path, info.pid)

    def _deploy_wave(self, topology: Topology, wave: list[Node],
                     mgmt_subnet: str, mgmt_bridge: str,
                     ssh_pubkey: str, host_ip: str) -> None:
        threads: list[Thread] = []
        errors: list[BaseException] = []

        def _runner(n: Node) -> None:
            try:
                self._deploy_node(topology, n, mgmt_subnet, mgmt_bridge,
                                  ssh_pubkey, host_ip)
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

    def _build_interface_specs(self, topology: Topology, node: Node,
                               mgmt_bridge: str) -> list[InterfaceSpec]:
        """Compute final InterfaceSpec list with bridge+mac populated."""
        ifaces: list[InterfaceSpec] = []
        # Management interface is always present and always called "mgmt"
        # internally (named differently from topology ifaces to avoid clashing
        # if a user puts a topology link on eth0).
        ifaces.append(InterfaceSpec(
            node_name=node.name,
            interface_name="mgmt",
            ip=self._mgmt_ips[(topology.name, node.name)],
            cidr="24",
            bridge=mgmt_bridge,
            mac=_mac_for(topology.name, node.name, "mgmt"),
        ))
        # Topology link interfaces — find matching link bridge by index.
        for iface_name, ifs in node._interfaces.items():
            link_idx = self._link_index_for(topology, node.name, iface_name)
            if link_idx is None:
                continue
            br = self._link_bridge_name(topology.name, link_idx)
            mac = _mac_for(topology.name, node.name, iface_name)
            ifaces.append(InterfaceSpec(
                node_name=node.name,
                interface_name=iface_name,
                ip=ifs.ip,
                cidr=ifs.cidr,
                bridge=br,
                mac=mac,
            ))
        return ifaces

    def _deploy_node(self, topology: Topology, node: Node,
                     mgmt_subnet: str, mgmt_bridge: str,
                     ssh_pubkey: str, host_ip: str) -> None:
        log.info("[%s/%s] Provisioning (%s)", topology.name, node.name,
                 "container" if node.is_container else "vm")
        node.state = transition_node_state(node.state, NodeState.PROVISIONING)
        backend = self._backend_for(topology.name, node)
        # `image` column persists either the VM image or the container image
        # — both are the user-facing source of the node's runtime.
        image_or_container = node.container if node.is_container else node.image
        self._db.save_node(
            topology_name=topology.name,
            name=node.name,
            image=image_or_container,
            vcpu=node.vcpu,
            memory_mb=node.memory,
            os_type=node.os_type.value,
            state=node.state.value,
        )
        self._db.log_event(topology.name, node.name, "info", "provisioning")

        if node.is_container:
            self._deploy_container_node(topology, node, mgmt_bridge, host_ip)
            return

        # Resolve image — in unit tests the image name is opaque; just pass through.
        image_record = self._db.get_image(node.image)
        image_path = image_record["path"] if image_record else node.image

        # The image record's os_type wins over node.os_type for backend-
        # specific behavior (cloud-init flavor, ssh user). VyOS needs vyos-
        # flavored cloud-init and the "vyos" SSH user.
        image_os_type = (image_record or {}).get("os_type", node.os_type.value)
        is_vyos = image_os_type == "vyos"

        overlay_path = str(OVERLAY_ROOT / topology.name / f"{node.name}.qcow2")
        backend.create_overlay(image_path, overlay_path)

        # Build the final interface list with bridges + MACs assigned.
        ifaces = self._build_interface_specs(topology, node, mgmt_bridge)
        mgmt_ip = self._mgmt_ips[(topology.name, node.name)]

        # Generate the cloud-init seed ISO before create_vm so it can be
        # attached as a CDROM. Tests use MockBackend which never reads it; we
        # still attempt creation but tolerate environments without cloud-localds
        # (the helper raises; engine should swallow only when backend is mock).
        seed_path = str(SEED_ROOT / topology.name / f"{node.name}.iso")
        Path(seed_path).parent.mkdir(parents=True, exist_ok=True)
        seed_ifaces = []
        for i, ifs in enumerate(ifaces):
            # Domain XML orders interfaces as: mgmt first, then topology links
            # in declaration order. The guest-visible name depends on the OS
            # (cloud-init netplan vs VyOS initramfs udev).
            seed_ifaces.append({
                "mac": ifs.mac,
                "ip": ifs.ip,
                "cidr": ifs.cidr,
                "gateway": host_ip if ifs.interface_name == "mgmt" else None,
                "eth_name": _guest_iface_name(image_os_type, i),
            })
        if is_vyos:
            # VyOS doesn't run cloud-init in our build, so skip the seed ISO
            # entirely and bootstrap via serial console after start().
            seed_path = None  # type: ignore[assignment]
        else:
            try:
                create_seed_iso(
                    output_path=seed_path,
                    hostname=f"{topology.name}-{node.name}",
                    ssh_pubkey=ssh_pubkey,
                    ifaces=seed_ifaces,
                )
            except RuntimeError as exc:
                log.warning("Skipping seed ISO build (%s); proceeding without it", exc)
                seed_path = None  # type: ignore[assignment]

        spec = VMSpec(
            name=f"{topology.name}-{node.name}",
            image=node.image,
            vcpu=node.vcpu,
            memory=node.memory,
            os_type=node.os_type,
            interfaces=ifaces,
            overlay_path=overlay_path,
            seed_iso_path=seed_path,
            mgmt_ip=mgmt_ip,
            topology_name=topology.name,
            ssh_user="vyos" if is_vyos else "ubuntu",
            ssh_password="vyos" if is_vyos else None,
        )
        vm_id = backend.create_vm(spec)
        with self._lock:
            self._vm_ids[(topology.name, node.name)] = vm_id

        if is_vyos and hasattr(backend, "prepare_vyos_bootstrap"):
            backend.prepare_vyos_bootstrap(
                vm_id=vm_id,
                ifaces=seed_ifaces,
                ssh_pubkey=ssh_pubkey,
                host_ip=host_ip,
            )

        backend.start(vm_id)

        # attach_interface call retained for back-ends that need hot-attach
        # (e.g. MockBackend tests count these). For LibvirtBackend it's a no-op
        # since interfaces are inlined into the domain XML.
        backend.attach_interface(vm_id, mgmt_bridge,
                                 _mac_for(topology.name, node.name, "mgmt"))

        # Update DB row with mgmt_ip.
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

    def _deploy_container_node(self, topology: Topology, node: Node,
                                mgmt_bridge: str, host_ip: str) -> None:
        """Provision a Docker container node and wire its mgmt interface."""
        cb = self._backend_for(topology.name, node)

        ifaces = self._build_interface_specs(topology, node, mgmt_bridge)
        mgmt_ip = self._mgmt_ips[(topology.name, node.name)]

        spec = VMSpec(
            name=f"{topology.name}-{node.name}",
            image=node.container,  # the docker image string
            vcpu=node.vcpu,
            memory=node.memory,
            os_type=node.os_type,
            interfaces=ifaces,
            overlay_path=None,
            seed_iso_path=None,
            mgmt_ip=mgmt_ip,
            topology_name=topology.name,
        )
        vm_id = cb.create_vm(spec)
        with self._lock:
            self._vm_ids[(topology.name, node.name)] = vm_id

        cb.start(vm_id)
        # Container is now running with --network=none; wire mgmt veth.
        cb.attach_interface(vm_id, mgmt_bridge,
                            _mac_for(topology.name, node.name, "mgmt"))

        self._db.save_node(
            topology_name=topology.name,
            name=node.name,
            image=node.container,
            vcpu=node.vcpu,
            memory_mb=node.memory,
            os_type=node.os_type.value,
            state=node.state.value,
            mgmt_ip=mgmt_ip,
        )
        node.state = transition_node_state(node.state, NodeState.READY)
        self._db.update_node_state(topology.name, node.name, node.state.value)
        self._db.log_event(topology.name, node.name, "info",
                           f"ready (container), mgmt_ip={mgmt_ip}")

    def _wire_link(self, topology: Topology, link, link_index: int) -> None:
        br = self._link_bridge_name(topology.name, link_index)
        log.info("[%s] Wiring link %d: %s/%s <-> %s/%s via %s",
                 topology.name, link_index,
                 link.if_a.node_name, link.if_a.interface_name,
                 link.if_b.node_name, link.if_b.interface_name, br)

        self._db._conn.execute(
            "INSERT INTO links (topology_name, node_a, iface_a, ip_a, "
            "node_b, iface_b, ip_b, bridge_name) VALUES (?,?,?,?,?,?,?,?)",
            (topology.name, link.if_a.node_name, link.if_a.interface_name, link.if_a.ip,
             link.if_b.node_name, link.if_b.interface_name, link.if_b.ip, br),
        )
        self._db._conn.commit()

        # Wire Link with backend/bridge refs so Link.down()/up() can work later.
        # In namespace mode this is the per-range backend so bridge recreate
        # happens inside the range's netns.
        link._backend = self._vm_backend(topology.name)
        link._bridge_name = br
        link._db = self._db
        link._topology_name = topology.name

        # Call attach_interface for each side. LibvirtBackend uses this to
        # ensure the VM's TAP is enslaved to the bridge (idempotent during
        # initial deploy; load-bearing when Link.up() recreates the bridge).
        # ContainerBackend uses this to create+wire the veth pair.
        # Mock back-ends record the call, which the unit tests rely on.
        for side in (link.if_a, link.if_b):
            vm_id = self._vm_ids[(topology.name, side.node_name)]
            mac = _mac_for(topology.name, side.node_name, side.interface_name)
            link._endpoints.append((vm_id, mac))
            side_node = topology._nodes[side.node_name]
            self._backend_for(topology.name, side_node).attach_interface(vm_id, br, mac)

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
        backend = self._backend_for(topology.name, node)
        live = LiveNode(
            name=node.name,
            mgmt_ip=mgmt_ip,
            topology_name=topology.name,
            backend=backend,
            vm_id=vm_id,
        )

        # 1. packages — Linux uses apt-get; Windows uses powershell commands.
        if node.os_type == OSType.LINUX and node._packages:
            pkg_list = " ".join(node._packages)
            # Cloud images run as the non-root `ubuntu` user; package and
            # service ops always need sudo. Wait for any cloud-init apt lock
            # to release first, otherwise install races with first-boot
            # `unattended-upgrades`.
            backend.exec(vm_id, "cloud-init status --wait || true")
            r = backend.exec(
                vm_id,
                f"sudo DEBIAN_FRONTEND=noninteractive apt-get install -y {pkg_list}",
            )
            if r.exit_code != 0:
                log.error("[%s/%s] apt-get install failed (rc=%d):\nstdout=%s\nstderr=%s",
                          topology.name, node.name, r.exit_code, r.stdout, r.stderr)
                raise RuntimeError(
                    f"apt-get install failed for {node.name}: {r.stderr.strip()[:300]}"
                )
        for ps_cmd in node._powershell_commands:
            backend.exec(vm_id, f"powershell -Command {ps_cmd}")

        # 2. files — upload each registered file.
        for dst, src in node._files:
            backend.upload(vm_id, src, dst)

        # 3. installs — upload source, run install, optionally verify.
        for inst in node._installs:
            remote_src = f"/tmp/{Path(inst.src).name}"
            backend.upload(vm_id, inst.src, remote_src)
            backend.exec(vm_id, inst.install_cmd)
            if inst.verify_cmd:
                backend.exec(vm_id, inst.verify_cmd)

        # 4. configure functions — pass a LiveNode bound to the backend.
        for fn in node._configure_fns:
            fn(live)

        # 5. services — enable then start. sudo for Linux (cloud images run
        # as the unprivileged `ubuntu` user). Windows nodes never set the
        # Linux services list, so no Windows-side branch needed here.
        for svc in node._services:
            if svc.enabled:
                backend.exec(vm_id, f"sudo systemctl enable {svc.name}")
            start_cmd = svc.start_cmd or f"sudo systemctl start {svc.name}"
            backend.exec(vm_id, start_cmd)

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
            node_backend = self._backend_for(topology.name, node)
            node_backend.stop(vm_id)
            node_backend.destroy(vm_id)
            node.state = transition_node_state(node.state, NodeState.DESTROYED)
            self._db.update_node_state(topology.name, node.name, node.state.value)
            self._vm_ids.pop((topology.name, node.name), None)

        if self._use_namespaces:
            # One supervisor teardown kills the range's libvirtd PID namespace
            # (reaping every QEMU child) and removes the netns, mgmt network,
            # data bridges (they live inside the netns), and range dir.
            self._teardown_namespace(topology.name)
        else:
            for br in self._link_bridges.get(topology.name, []):
                self._backend.delete_bridge(br)
            self._backend.delete_bridge(mgmt_bridge_name(topology.name))
        self._link_bridges.pop(topology.name, None)

        self._db.free_mgmt_subnet(topology.name)
        self._db.delete_topology(topology.name)
        log.info("Destroy complete for '%s'", topology.name)

    def _teardown_namespace(self, topology_name: str) -> None:
        """Tear down the range's namespaces + per-range libvirtd, then its
        cgroup (if one was created)."""
        supervisor.destroy_range(topology_name)
        self._range_info.pop(topology_name, None)
        self._range_backends.pop(topology_name, None)
        self._range_container_backends.pop(topology_name, None)
        cgroup_path = self._cgroup_paths.pop(topology_name, None)
        if cgroup_path is not None:
            cgroup.destroy_cgroup(topology_name)
