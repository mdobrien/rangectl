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

### Phase 0: EC2 Environment Setup
Create `scratch/scripts/ec2-bootstrap.sh` that does everything below. Agent runs it on the EC2 box and walks away.

**System dependencies** (apt):
- `qemu-kvm`, `libvirt-daemon-system`, `libvirt-clients`, `virtinst`
- `bridge-utils`, `net-tools`, `cloud-image-utils` (for cloud-init seed ISOs)
- `python3-pip`, `python3-venv`

**User/group setup**:
- Add ubuntu user to `libvirt` and `kvm` groups

**Python dependencies** (pip):
- `libvirt-python`, `paramiko`, `pytest`, `pyyaml`, `jinja2`

**Download pre-built cloud images** (no building — these are published qcow2s):
- Ubuntu 22.04: `https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img`
- Ubuntu 24.04: `https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img`
- VyOS: rolling release qcow2 from `https://github.com/vyos/vyos-rolling-nightly-builds/releases`
- Store in `~/.rangectl/images/`

**Smoke test**:
- `kvm-ok` returns success
- `virsh list --all` works
- Boot Ubuntu 22.04 cloud image via `virt-install` with a cloud-init seed ISO (inject a test SSH key), confirm it starts, SSH in, destroy it

**Validation** (script exits non-zero if any fail):
- KVM enabled
- libvirt daemon running
- All three base images exist in `~/.rangectl/images/`
- Smoke test VM booted, SSH'd, and destroyed cleanly

### Phase 1: Backend Interface + Libvirt (VM lifecycle)
- `Backend` protocol definition
- `LibvirtBackend`: create, start, stop, destroy VMs
- XML generation for VM definitions
- COW overlay disk management (qcow2 overlays backed by read-only base)
- SQLite DB setup — schema for topologies, nodes, bridges, IP allocations, state, images
- Resource validation (check host capacity before deploy)
- **Unit tests**: MockBackend, StateDB schema ops, resource validation math
- **Integration tests**: VM create/start/stop/destroy via libvirt

### Phase 2: Networking
- Linux bridge create/destroy
- Tap interfaces, wire to bridges
- Management bridge per topology (`rangectl-mgmt-{name}`)
- Host interface on mgmt bridge at .254
- Sequential /24 mgmt subnet allocation from pool
- Static IP assignment via cloud-init/netplan
- Topology-name-prefixed resource naming
- **Unit tests**: IP allocation, subnet math, bridge naming
- **Integration tests**: bridge create, tap wiring, ping connectivity

### Phase 3: State Machine + Dependency Resolver
- Node state machine with defined transitions (defined → provisioning → ready → linked → running → destroying)
- DAG construction from `depends_on` declarations
- Wave-based parallel deploy (topo-sort → waves, threads within wave, block between waves)
- Readiness probes (L1: running, L2: ping, L3: custom)
- Deferred link wiring (both endpoints ready before bridge/tap creation)
- Structured logging — state transitions, probes, commands — stored in SQLite
- Deploy progress streaming (real-time)
- **Unit tests**: state transitions, topo-sort, wave computation
- **Integration tests**: full deploy with readiness probes

### Phase 4: Image Registry + Builder
- Local image store (directory + metadata in SQLite)
- `image add` / `image list` / `image remove` with inject method declaration
- `ImageBuilder`: boot, apply, snapshot, store (pre-bakes SSH key + guest agent)
- COW base images are read-only, overlays per node
- **Unit tests**: image metadata CRUD in SQLite
- **Integration tests**: boot-and-snapshot image build

### Phase 5: Dependency & Config Injection
- `packages()`, `powershell()` on Node and DependencySet
- `install()` for custom software
- `@configure` decorator for custom Python
- `service()` declarations with readiness
- `DependencySet` with `apply()`
- Injection strategy per image: cloud-init, cloudbase-init, guest-agent, pre-baked
- SSH keypair generation per topology, key injection via config method
- Paramiko for exec/upload over mgmt network
- Fail-fast with `cleanup_on_fail=True` default, debug mode to leave nodes up
- **Unit tests**: ordering, apply(), configure registration
- **Integration tests**: SSH exec, package install, file upload

### Phase 6: SDK API Surface
- `Topology`, `Node`, `Link` classes
- `topo.link(node.ethN["ip"], ...)` syntax
- `topo.deploy()` context manager
- `topo.export()` / `Topology.from_yaml()`
- `list_topologies()`
- Imperative interaction: `exec`, `upload`, `snapshot`, `restore`
- `rng.logs()`, `rng["node"].logs()` for structured log access
- Cross-node references via node objects (`target.eth0.ip`)
- **Unit tests**: Topology/Node/Link API, export/import
- **Integration tests**: end-to-end topology lifecycle

### Phase 7: Docker Container Nodes
Mixed ranges — VMs and containers in the same topology, on the same bridges. A container is just a netns with a filesystem and a PID namespace. Wire it to topology bridges the same way as a VM — except veth pair instead of TAP.

**Core mechanic** (no QEMU, no TAP, no domain XML — ~1s start vs ~30s VM boot):
```bash
docker run --network=none -d --name <n> <image>    # isolated container, no Docker networking
ip link add veth_c0 type veth peer name veth_c0p
ip link set veth_c0 master <bridge>                 # bridge end
ip link set veth_c0p netns <container-pid>           # container end
nsenter -t <pid> -n ip link set veth_c0p name eth1
nsenter -t <pid> -n ip addr add 10.0.1.5/24 dev eth1
nsenter -t <pid> -n ip link set eth1 up
```

**What changes in rangectl** (mostly additive):
- SDK syntax: `topo.node("web", container="nginx:latest")` vs `topo.node("db", image="ubuntu-22.04")`
- Node type detection: `container=` kwarg → ContainerBackend, `image=` kwarg → LibvirtBackend
- `link()` model unchanged — a link between ContainerNode and VMNode is a bridge with one TAP member (VM) and one veth member (container). Kernel doesn't care.
- Command execution: VMs use SSH, containers use `docker exec`. Both behind unified `LiveNode.exec(cmd)`.
- Management network: container gets a veth on the mgmt bridge, same addressing as VMs
- Health checks: same readiness model (L1/L2/L3) — containers support exec, SSH optional
- Lifecycle: same state machine as VMs

**Hard parts (real but bounded):**
- Privileged networking (FRR, nftables, IP forwarding) needs `--cap-add=NET_ADMIN --cap-add=NET_RAW` or `--privileged`. Explicit policy in node spec.
- Multi-interface containers: all NICs wired with `--network=none` before signaling init. ContainerLab uses an entrypoint shim for this.
- Image distribution: Docker's own registry/pull, separate from qcow2 image workflow.
- Snapshot semantics differ: `docker commit` or just relaunch from image. v1: containers don't participate in `snapshot()`/`restore()` — defer to v2.

- **Unit tests**: ContainerBackend with MockBackend pattern, mixed topology DAG resolution
- **Integration tests**: Topo 7 — mixed VM + container topology (e.g. FRR container router + Ubuntu VM hosts), cross-type connectivity, container exec

### Phase 8: Namespace Isolation — Supervisor + Network Namespace
Create the namespace plumbing: supervisor launches libvirtd inside PID+net+mount namespaces, veth pair connects range to host for management access. Validated in feasibility spike (`scratch/scripts/libvirtd-ns-experiment.sh`). Full design: `agents/network-architecture.md`.

**What this solves (across Phases 8-12):**
- Bridge name collisions disappear — names scoped per netns, no more `rlmgt-{hash}` hashing
- iptables blast radius bounded — errant rules can't affect other ranges or host
- L2 isolation is structural (netns), not policy-based (iptables FORWARD DROP)
- Clean teardown guaranteed — kill libvirtd PID 1 → kernel reaps all QEMU in the ns
- Resource limits per range (memory, CPU, PIDs) via cgroups v2
- Freeze/thaw via cgroup freezer (atomic pause, zero CPU)

**What carries over unchanged (across all sub-phases):**
- Topology DAG, state machine, dependency injection
- Image registry, cloud-init builders, SSH plumbing
- ContainerBackend (containers already use netns — they move into the range's netns)
- SDK API surface (Topology, Node, Link, Range, LiveNode)
- Unit tests (MockBackend-based, no namespace dependency)

**New module: `rangectl/supervisor.py`**
- `unshare --pid --fork --net --mount --uts --propagation private --mount-proc`
- Bind-mount per-range directories over libvirt state paths:
  - `/run/libvirt` → `/ranges/<name>/run-libvirt` (sockets, PID files)
  - `/var/log/libvirt`, `/var/cache/libvirt`, `/etc/libvirt` → per-range dirs
  - `/var/lib/libvirt/qemu`, `dnsmasq`, `boot`, `swtpm` → per-range dirs
  - `/var/lib/libvirt/images` NOT mounted (shared image registry)
  - `/run/dbus` → empty dir (blocks systemd-machined cross-ns bug)
- Per-range `qemu.conf`: `security_driver = "none"`, `dynamic_ownership = 0`, `user/group = root`
- `exec /usr/sbin/libvirtd --config <per-range>/libvirtd.conf --pid-file /run/libvirt/libvirtd.pid`
- Libvirt socket exposed on host filesystem at `/ranges/<name>/run-libvirt/libvirt-sock`
- Teardown: kill libvirtd's host-PID → kernel SIGKILLs every process in the PID ns

**New module: `rangectl/netns.py`**
- Create mgmt bridge inside range netns: `mgmt-br` (clean name, no hashing needed)
- Create veth pair: one end in range netns (on mgmt-br), one end in host namespace
- Host route: `ip route add 10.255.X.0/24 via <veth-host-end>`
- iptables FORWARD ACCEPT for the mgmt CIDR on host
- Management IPs auto-assigned from range's /24

**Per-range process tree:**
```
Range "lab-1"
├── PID Namespace (libvirtd = PID 1)
│   ├── qemu: router
│   ├── qemu: server
│   └── qemu: client
├── Network Namespace
│   ├── mgmt-br (10.255.1.0/24)
│   ├── data bridges (user-defined)
│   └── veth pair → host namespace
├── Mount Namespace (bind-mounts for libvirt state isolation)
│   └── /run/dbus blocked
└── Libvirt socket: /ranges/lab-1/run-libvirt/libvirt-sock
```

**Gate 2 testing strategy for Phases 8-11:** Three representative topologies validate each phase incrementally. Full Topo 1-7 regression runs once in Phase 12.

| Test | What it covers | Backend paths exercised |
|------|---------------|----------------------|
| 2-node (Topo 1 pattern) | Basic VM lifecycle, cloud-init, mgmt SSH via veth | Ubuntu, LibvirtBackend |
| VyOS routed (Topo 2 pattern) | Multi-OS, serial console bootstrap, cross-subnet routing | VyOS, multi-bridge |
| Mixed VM+container (Topo 7 pattern) | Container veth wiring, docker exec, mixed backend dispatch | ContainerBackend + LibvirtBackend |
| Multi-range (2 ranges simultaneous) | Two ranges coexist, separate netns/libvirtd/mgmt, independent destroy | Namespace isolation, per-range sockets |

- **Unit tests**: supervisor launch/teardown (mocked unshare), netns veth creation, host route setup, bind-mount list generation
- **Integration tests**: 2-node + VyOS routed + mixed VM/container on ns-aware supervisor. Virsh from host via per-range socket, SSH reachable via veth mgmt path.

### Phase 9: Namespace Isolation — Cgroups
Resource control and freeze/thaw per range via cgroups v2.

**New module: `rangectl/cgroup.py`**
- Create cgroup at `/sys/fs/cgroup/<range-name>/`
- Controllers:
  - `memory.max` — hard memory ceiling for all VMs in range
  - `cpu.max` — CPU time quota (microseconds per period)
  - `pids.max` — fork bomb protection
  - `cpuset.cpus` — pin to specific cores (optional)
  - `cgroup.freeze` — pause/resume all processes atomically
- Supervisor writes its own PID into the cgroup before `unshare` so all descendants are born into it
- Freeze/thaw caveats: clock skew on thaw (guest kvm-clock adjusts, userspace may not). Short-duration freezes recommended. Post-thaw hook: `range.on_thaw(lambda vm: vm.exec("chronyc makestep"))`

**SDK surface:**
```python
range = Range("lab-1", resources=Resources(memory="32G", cpus=8, pids=500))
range.freeze()    # cgroup.freeze = 1
range.thaw()      # cgroup.freeze = 0
```

- **Unit tests**: cgroup creation, limit writing, freeze/thaw state, resource dataclass
- **Integration tests**: deploy range with cgroup limits, verify cgroup exists, freeze → verify CPU drops to 0, thaw → verify VMs resume. Run 2-node + VyOS + mixed tests under cgroup.

### Phase 10: Namespace Isolation — Backend Rewrite
Rewrite `libvirt_backend.py` to operate inside namespaces via per-range libvirt sockets.

**Changes to `libvirt_backend.py` (~50% rewrite):**
- `create_vm` → `virsh -c qemu+unix:///system?socket=/ranges/<name>/run-libvirt/libvirt-sock define <xml>`
- Bridge/TAP creation moves inside the netns (executed via `nsenter` or by libvirtd itself since it's in the ns)
- Bridge names are clean (`mgmt-br`, `data-0`, `data-1`) — no hashing, no IFNAMSIZ workaround
- Delete: `_create_bridge()`, `_delete_bridge()` host-level implementations → replaced by netns-aware versions in `netns.py`
- Delete: bridge name hashing, host-IP collision avoidance, `_ensure_mgmt_isolation()` iptables rule

**What survives:**
- Domain XML generation (`_xml_for()`) — mostly unchanged
- SSH plumbing (`_wait_for_ssh`, `_ssh_client`, `exec`, `upload`) — unchanged, SSH goes via veth
- VyOS serial console bootstrap — unchanged
- Snapshot/restore — unchanged (virsh commands just use the per-range socket)
- Cloud-init seed ISO generation — unchanged

- **Unit tests**: updated MockBackend to reflect new interface, verify socket-based virsh commands
- **Integration tests**: 2-node + VyOS routed + mixed VM/container on ns-aware backend. VM boots, SSH works, cross-subnet routing works, docker exec works, all via per-range libvirtd.

### Phase 11: Namespace Isolation — Engine Integration
Wire the engine to call supervisor for range provisioning, use the ns-aware backend for node deployment.

**Changes to `engine.py` (~20%):**
- `deploy()` calls `supervisor.create_range()` before deploying nodes (creates ns, starts libvirtd, wires veth)
- `destroy()` calls `supervisor.destroy_range()` after node cleanup (kills PID ns, removes cgroup, deletes veth)
- `_deploy_node()` uses ns-aware backend (virsh via per-range socket)
- Mgmt subnet allocation unchanged — just routed via veth instead of host bridge
- DAG resolution, wave deploy, dependency injection — all unchanged

**Provisioning order (from architecture doc):**
```
Step 1: Create network namespace
Step 2: Create mgmt bridge in namespace
Step 3: Create veth pair
Step 4: Add host route to mgmt subnet
Step 5: Add iptables FORWARD rule for CIDR
Step 6: Start libvirtd in namespace
Step 7: Start QEMU processes (VMs boot)
Step 8: Health checks confirm VMs reachable
Step 9: Post-boot configuration
```

- **Unit tests**: engine calls supervisor in correct order, deploy/destroy lifecycle with mocked supervisor
- **Integration tests**: 2-node + VyOS routed + mixed VM/container end-to-end through engine with namespace isolation

### Phase 12: Namespace Isolation — SDK Surface + Internet Policy + Full Regression
Expose namespace features through the SDK, add per-range internet access control, and validate everything works together.

**SDK additions:**
```python
range = Range("my-lab",
    topology=topo,
    mgmt_network="10.255.1.0/24",
    resources=Resources(memory="32G", cpus=8),
    internet="full")

range.freeze()
range.thaw()
range.enable_internet()
range.disable_internet()
```

**Internet access policy:**
- `internet="none"` (default): VMs can reach each other and host can reach VMs. No outbound.
- `internet="full"`: MASQUERADE all range traffic out through host's internet connection via the veth choke point.
- Per-range iptables chain (`RANGE-<name>`) — teardown flushes only its chain.
- Runtime toggle: `range.enable_internet()` / `range.disable_internet()`

**Management access from host:**
```bash
rangectl virsh lab-1 list                    # virsh scoped to range
rangectl virsh lab-1 console router          # break-glass serial console
rangectl exec lab-1 -- ip link show          # run command inside netns
```

**Full regression (Topo 1-7 on ns-aware backend):**
All existing test topologies must pass on the new namespace-isolated architecture:
- Topo 1: 2 Ubuntu VMs
- Topo 2: VyOS router + 2 Ubuntu
- Topo 3: Services + DependencySet (nginx install, requires internet=full)
- Topo 4: Diamond DAG + snapshot/restore
- Topo 5: Link toggle
- Topo 6: Multi-topology isolation (now structural via netns, not iptables FORWARD DROP)
- Topo 7: Mixed VM + container

Plus new tests:
- Two ranges with full namespace isolation — no cross-range L2 leakage, independent destroy
- Freeze/thaw — CPU drops to 0, VMs resume
- Internet policy — `internet="full"` allows apt-get, `internet="none"` blocks it
- Resource limits enforced

- **Unit tests**: Range constructor with resources/internet, freeze/thaw API, internet toggle
- **Integration tests**: full Topo 1-7 regression + namespace isolation + freeze/thaw + internet policy

### Phase 13: Windows Support
- UEFI boot, virtio drivers
- cloudbase-init / unattend.xml generation
- `powershell()` on Node/DependencySet
- WinRM for post-boot config
- **Unit tests**: Windows-specific dep resolution
- **Integration tests**: Windows VM boot, cloudbase-init, WinRM

## Open Questions
- YAML export schema — define later
