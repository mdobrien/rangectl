# rangectl — Rapid Automated Network Generation and Environment Control
**Created**: 2026-05-27
**Status**: In Progress

## Goal
Design and build **rangectl** — a VM testbed orchestration platform with a clean Python SDK. Libvirt/QEMU backend, single-host, deployable on any Ubuntu box.

Core problem: GNS3 has the right mental model but terrible execution (race conditions, unreliable lifecycle, painful API). We want the same intuitive topology-as-graph model with rock-solid orchestration and a SDK that feels like writing normal Python.

## Design Decisions (Locked)

### Architecture
- **Declarative topology deployment**, imperative interaction post-deploy
- **Libvirt/QEMU backend** — no Proxmox dependency, runs on any Linux box with KVM
- **Single-host** — multi-host is future scope
- **Backend interface** (`Backend` protocol) so Proxmox or other backends can be added later

### State & Lifecycle
- **Proper state machine** for node lifecycle (defined → provisioning → ready → linked → running → destroying)
- **Fail-fast** with rollback on deployment failure
- **Dependency DAG** with explicit `depends_on`, resolved via topo-sort
- **Readiness model** (3 levels):
  - L1: VM running (hypervisor reports running) — default minimum
  - L2: OS booted (ping passes on mgmt interface) — default
  - L3: Service ready (user-defined: port open, process running, etc.)
- GNS3 only does L1. We default to L2, support L3 via `ready_when`

### Networking
- **Linux bridges** (not OVS, for now)
- **Static IPs** declared in topology: `router.eth0["10.0.1.1/24"]`
- **Management network**: separate bridge per topology (`testbed-mgmt-{name}`), isolated subnet, host at .254
- **Named topologies**: name prefixes all resources (VMs, bridges), multiple topologies coexist
- **No jump box for now** — host is on every mgmt bridge. Architecture supports adding jump box later (`jump_box=True`)
- Links are deferred — actual bridge/tap wiring happens only when both endpoints report ready

### Images
- **Local registry** with `image add` CLI
- **ImageBuilder**: boot base image, apply changes, snapshot, store
- Build-time (baked image) and deploy-time (config injection) both supported
- User prototypes with deploy-time deps, promotes to baked image when stable

### Dependencies & Config
- **Layered dependency model**: system packages → language packages → custom installs → configure functions → services
- **`DependencySet`**: composable, reusable dependency groups that can be `apply()`'d to nodes
- **Platform-specific**: `apt()`, `pip()`, `choco()`, `powershell()`
- **Custom software**: `install(name, src, install_cmd, verify_cmd)`
- **Custom Python**: `@node.configure` / `@depset.configure` decorators
- **Services**: `node.service(name, start_cmd, ready_when)`
- **Config injection**: abstracted — engine picks cloud-init, guest agent, or unattend.xml based on OS
- **Windows support**: via choco, powershell, cloudbase-init/unattend.xml

### SDK API Surface
- `Topology(name)` — named, isolated topology
- `topo.node(name, image, depends_on, ready_when)` — declare a node
- `topo.link(node_a.ethN["ip"], node_b.ethN["ip"])` — declare a link with static IPs
- `topo.deploy()` — context manager, returns live lab
- `topo.export("file.yaml")` — export topology without deploying
- `Topology.from_yaml("file.yaml")` — import topology from YAML
- `DependencySet(name)` — reusable dep group
- `ImageBuilder(base).packages().build(name)` — bake custom images
- `list_topologies()` — list deployed topologies
- `lab["node"].exec()`, `.upload()`, `.mgmt_ip` — imperative interaction
- `lab.snapshot()`, `lab.restore()` — topology-wide snapshots

### Topology Export
- YAML format, works without deploying
- Contains: nodes, interfaces, IPs, links, dependencies, images
- Importable: `Topology.from_yaml()` reconstructs the full topology

## Implementation Phases

### Phase 1: Backend Interface + Libvirt (VM lifecycle)
- `Backend` protocol definition
- `LibvirtBackend`: create, start, stop, destroy VMs
- XML generation for VM definitions
- COW overlay disk management (qcow2 overlays backed by read-only base)
- SQLite DB setup — schema for topologies, nodes, bridges, IP allocations, state
- Resource validation (check host capacity before deploy)

### Phase 2: Networking
- Linux bridge create/destroy
- Tap interfaces, wire to bridges
- Management bridge per topology (`testbed-mgmt-{name}`)
- Host interface on mgmt bridge at .254
- Sequential /24 mgmt subnet allocation from pool
- Static IP assignment via cloud-init/netplan
- Topology-name-prefixed resource naming

### Phase 3: State Machine + Dependency Resolver
- Node state machine with defined transitions (defined → provisioning → ready → linked → running → destroying)
- DAG construction from `depends_on` declarations
- Wave-based parallel deploy (topo-sort → waves, threads within wave, block between waves)
- Readiness probes (L1: running, L2: ping, L3: custom)
- Deferred link wiring (both endpoints ready before bridge/tap creation)
- Structured logging — state transitions, probes, commands — stored in SQLite
- Deploy progress streaming (real-time)

### Phase 4: Image Registry + Builder
- Local image store (directory + metadata)
- `image add` / `image list` / `image remove` with inject method declaration
- `ImageBuilder`: boot, apply, snapshot, store (pre-bakes SSH key + guest agent)
- COW base images are read-only, overlays per node

### Phase 5: Dependency & Config Injection
- `apt()`, `pip()`, `choco()`, `powershell()` on Node and DependencySet
- `install()` for custom software
- `@configure` decorator for custom Python
- `service()` declarations with readiness
- `DependencySet` with `apply()`
- Injection strategy per image: cloud-init, cloudbase-init, guest-agent, pre-baked
- SSH keypair generation per topology, key injection via config method
- Paramiko for exec/upload over mgmt network
- Fail-fast with `cleanup_on_fail=True` default, debug mode to leave nodes up

### Phase 6: SDK API Surface
- `Topology`, `Node`, `Link` classes
- `topo.link(node.ethN["ip"], ...)` syntax
- `topo.deploy()` context manager
- `topo.export()` / `Topology.from_yaml()`
- `list_topologies()`
- Imperative interaction: `exec`, `upload`, `snapshot`, `restore`
- `lab.logs()`, `lab["node"].logs()` for structured log access
- Cross-node references via node objects (`target.eth0.ip`)

### Phase 7: Windows Support
- UEFI boot, virtio drivers
- cloudbase-init / unattend.xml generation
- `choco()`, `powershell()` on Node/DependencySet
- WinRM for post-boot config

## Open Questions
- YAML export schema — define later
