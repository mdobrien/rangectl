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

---

**Build order for Phases 13-16:** Phase 13 → Phase 15 → Phase 14 → Phase 16

Phase 15 (SDK polish) redesigns the public API — Range lifecycle class, OS drivers, clean interface. Phase 14 (CLI) wraps the SDK. Building CLI before the SDK redesign means rewriting it, so Phase 15 must come first. Phase 13 (persistent ranges) is a prerequisite for both.

---

### Phase 13: Persistent Ranges
`Range.connect(name)` and `Range.list()` so ranges survive process exit. Full design: `20260529-10-phase13-persistent-ranges.md`.

- Persist engine bookkeeping (VM IDs, mgmt IPs, backend params) to StateDB
- `Range.connect()` reads StateDB + range.json, rebuilds LiveNode handles with OS drivers
- `Range.list()` returns status: RUNNING, FROZEN, ORPHANED
- Stale state detection and cleanup
- **Unit tests**: connect with mocked state, list, stale detection
- **Integration tests**: deploy → exit → reconnect → run → destroy

### Phase 15: SDK Polish (run BEFORE Phase 14)
Redesign the user-facing API. Full design: `20260529-12-phase15-sdk-polish.md`.

**Range lifecycle class** — structured definition with explicit methods:
- `define_nodes()` → `define_network()` → boot → `install_software()` → dep check → `configure_os()` → `verify()` → READY
- `verify()` is required — deploy fails if not overridden

**OS driver abstraction** — per-OS behavior behind a clean interface:
- `OSDriver` base class — `put()` and `exec()` required, everything else optional
- Shipped: LinuxDriver (Paramiko), VyOSDriver (Paramiko), ContainerDriver (docker), WindowsDriver (skeleton)
- Extensible via `OSType.register("junos", JunosDriver)`
- Auth handled by engine, drivers receive authenticated transports

**API cleanup:**
- Users never import Engine, LibvirtBackend, or StateDB
- `.run()` returns stdout, `.exec()` returns ExecResult, `.put()` for files, `.put_dir()` for dirs
- Node power ops: stop/start/restart/status
- ImageBuilder.build() implemented
- Refactor all integration tests (Topo 1-7) to use Range subclass API

- **Unit tests**: lifecycle method ordering, OS driver dispatch, ImageBuilder
- **Integration tests**: full lifecycle via Range subclass, refactored Topo 1-7

### Phase 14: CLI (run AFTER Phase 15)
Command-line tool for operating on deployed ranges. Full design: `20260529-11-phase14-cli.md`.

**Depends on:** Phase 13 (Range.connect) + Phase 15 (stable SDK with OS drivers)

**Commands:** list, status, exec, upload, ssh-config, virsh, netns, logs, qemu-log, ps, net, node stop/start/restart, freeze, thaw, snapshot, restore, internet, destroy, images, deploy (YAML secondary path)

- `argparse`, no external deps
- `--yaml` for machine-readable output
- SSH keys transparent (per-range ed25519, managed by engine)
- Node power ops also in SDK

- **Unit tests**: arg parsing, output formatting, error handling
- **Integration tests**: list, status, exec, node stop/start, destroy

### Phase 16: Management Namespace (Host Protection)
The architecture doc (v3) describes a three-tier namespace model: host → management namespace → range namespaces. Currently, veth pairs go directly from range namespaces to the host. All per-range iptables rules, routes, and veth pairs are created on the host. An orchestrator bug can corrupt host networking.

This phase adds the persistent management namespace as an isolation layer. After initial setup (one veth pair + one route + one MASQUERADE on the host), the host network config is locked. All per-range operations happen inside the management namespace.

**What changes:**
- New: `rangectl/mgmt_namespace.py` — create/destroy persistent management namespace, host↔mgmt veth pair
- `rangectl/netns.py` — per-range veth pairs connect to mgmt namespace (not host)
- `rangectl/internet.py` — iptables chains move into mgmt namespace
- `rangectl/supervisor.py` — routes added in mgmt namespace, not host
- Host setup: one-time provisioning script (4 operations: veth, route, FORWARD, MASQUERADE)

**Host network operations after initial setup: zero.** Recovery from orchestrator bug: kill mgmt namespace, recreate, reconnect ranges. Seconds, not a reboot.

- **Unit tests**: mgmt namespace create/destroy, per-range veth routing through mgmt ns
- **Integration tests**: deploy range, verify host iptables untouched, kill mgmt ns + recover

### Phase 17: Performance Benchmarking & Optimization
Establish baselines and identify bottlenecks. No premature optimization — measure first, then fix what matters.

**Benchmarks to establish:**
- Range deploy time: time from `deploy()` to READY (breakdown: namespace setup, libvirtd start, VM boot, cloud-init, SSH ready, dependency install, verify)
- Range destroy time: time from `destroy()` to clean
- Multi-range: deploy N ranges simultaneously, measure scaling behavior
- Snapshot/restore cycle time
- Freeze/thaw cycle time (freeze → verify frozen → thaw → verify resumed)
- Node boot time by OS type (Ubuntu, VyOS, container)
- SSH exec latency (host → mgmt ns → range ns → VM)
- File transfer throughput (put/put_dir via SFTP)
- Memory overhead per range (libvirtd RSS + QEMU RSS)
- Disk overhead per range (overlays, seed ISOs, per-range state dirs)
- Maximum concurrent ranges on c5.metal (96 vCPU, 192GB RAM)

**Optimization candidates (measure before touching):**
- Parallel VM boot within a wave (already threaded — verify it's actually parallel)
- Cloud-init vs pre-baked image boot time comparison
- Overlay creation time at scale (100+ overlays)
- libvirtd startup time in namespace
- SSH connection pooling (reuse Paramiko connections across exec calls)
- StateDB write batching during deploy

**Deliverables:**
- `scratch/scripts/benchmark.py` — repeatable benchmark suite
- Baseline numbers documented in a benchmark results file
- Identified bottlenecks with evidence (not guesses)
- Targeted fixes for top 2-3 bottlenecks

- **No unit tests** — this is measurement, not new functionality
- **Integration tests**: benchmark suite runs on EC2, produces results

### Phase 18: Security Hardening — QEMU Unprivileged
QEMU currently runs as root inside the PID namespace. With AppArmor disabled, a guest escape gives root inside the namespace. This phase changes QEMU to run as `libvirt-qemu` — the stock unprivileged user.

**Changes:**
- `rangectl/supervisor.py` — qemu.conf: `user = "libvirt-qemu"`, `group = "libvirt-qemu"`
- `rangectl/libvirt_backend.py` — `chown libvirt-qemu` on overlay files and seed ISOs
- `rangectl/supervisor.py` — per-range dirs need `libvirt-qemu` write access (logs, cache, runtime state)
- Verify: VyOS serial console PTYs still work (libvirtd creates PTY as root, QEMU accesses as libvirt-qemu)
- Verify: snapshot/restore still works with unprivileged QEMU

**Risk:** Low. File permission changes only. No architectural changes. Rollback: change user back to root.

- **Unit tests**: verify qemu.conf content, file permission helpers
- **Integration tests**: full Topo 1-7 regression with QEMU as libvirt-qemu

### Phase 19: Link Properties (WAN Simulation)
Runtime link impairment via `tc netem` inside the range netns. Every link can be degraded on the fly — latency, bandwidth, loss, jitter, reordering, corruption, duplication. All native Linux, no external deps.

**SDK:**
```python
# At definition time
lab.link(router.eth1["10.0.1.1/24"], target.eth1["10.0.1.2/24"],
         latency="50ms", bandwidth="10mbit", loss="2%")

# At runtime — modify live links
link = lab.link("router", "target")
link.impair(latency="100ms", bandwidth="1mbit", loss="5%", jitter="20ms")
link.impair(reorder="25%", corrupt="1%")
link.clear()  # remove all impairments, restore clean link
```

**Implementation:**
- `tc qdisc add dev <iface> root netem delay 50ms` (and variants) inside the range netns
- Applied per-interface — can be asymmetric (degraded in one direction only)
- `link.impair()` replaces current qdisc; `link.clear()` removes it
- Integrates with link.down()/up() — impairments are re-applied after link.up()

**Parameters:**
| Param | tc netem | Example |
|-------|---------|---------|
| `latency` | `delay` | `"50ms"`, `"100ms"` |
| `jitter` | `delay X Yms` | `"10ms"` (variation around latency) |
| `bandwidth` | `rate` (tbf parent) | `"10mbit"`, `"1gbit"` |
| `loss` | `loss` | `"5%"`, `"0.1%"` |
| `reorder` | `reorder` | `"25%"` |
| `corrupt` | `corrupt` | `"1%"` |
| `duplicate` | `duplicate` | `"1%"` |

- **Unit tests**: impair/clear generate correct tc commands (mocked)
- **Integration tests**: impair a link, measure latency increase via ping, clear and verify restored

### Phase 20: Hub & Switch Node Types
L2 network devices as first-class node types. No VM, no container — just a bridge with specific forwarding behavior inside the range netns.

**SDK:**
```python
# Switch — default Linux bridge behavior (MAC learning, per-port forwarding)
sw = lab.switch("core-switch", ports=8)

# Hub — all traffic flooded to all ports (disable MAC learning)
hub = lab.hub("monitor-hub", ports=4)

# Wire them like any other node
lab.link(router.eth1["10.0.1.1/24"], sw.port0)
lab.link(target.eth1["10.0.1.2/24"], sw.port1)
lab.link(sw.port2, hub.port0)  # switch uplink to hub
lab.link(ids_sensor.eth1, hub.port1)  # IDS sees all traffic via hub
```

**Implementation:**
- Switch: standard Linux bridge (`ip link add <name> type bridge`) — already what data bridges are
- Hub: Linux bridge with MAC learning disabled (`bridge link set dev <port> learning off` + `flood on`) — all frames flooded to all ports
- No boot time, no health check, no state machine — they're instant infrastructure
- Ports are just bridge interfaces, not TAPs — VMs attach TAPs to them as usual

- **Unit tests**: switch/hub creation, port assignment, MAC learning flag
- **Integration tests**: hub floods traffic to IDS sensor port, switch does not

### Phase 21: Port Mirroring, SPAN & Packet Capture
Observe traffic on any interface or bridge inside a range. Two capabilities: mirroring (copy traffic to another port) and capture (write pcap files).

**SDK — Packet capture:**
```python
# Capture on a specific node interface
cap = lab.capture("router", "eth1")         # returns a Capture handle
cap = lab.capture("router", "eth1",
                  filter="tcp port 80",      # BPF filter
                  output="/tmp/http.pcap")   # write to file

# Stop and get the pcap
cap.stop()
pcap_path = cap.file                        # path to pcap file

# Capture on a bridge (sees all traffic on the segment)
cap = lab.capture_bridge("data-0")
```

**SDK — Port mirroring:**
```python
# Mirror all traffic on a link to a sensor node
lab.mirror("router", "eth1", to="ids-sensor", port="eth0")

# Mirror with direction filter
lab.mirror("router", "eth1", to="ids-sensor", port="eth0",
           direction="ingress")  # or "egress" or "both"

# Remove mirror
lab.unmirror("router", "eth1")
```

**Implementation:**
- Capture: `ip netns exec <ns> tcpdump -i <iface> -w <file> <filter>` as a background process. Capture handle tracks the PID and stops it.
- Mirror (Linux bridge): `tc qdisc add dev <src> ingress && tc filter add dev <src> parent ffff: matchall action mirred egress mirror dev <dst>` inside the netns
- Mirror (OVS, if bridge driver abstraction exists): native `ovs-vsctl -- set Bridge <br> mirrors=@m ...`
- Capture files stored in `/ranges/<name>/captures/`
- Range destroy cleans up running captures

- **Unit tests**: capture start/stop commands (mocked), mirror tc rules
- **Integration tests**: capture pcap on link, verify packets present; mirror traffic to sensor, verify sensor sees it

### Phase 22: Per-Range Services (Extensible, DNS First)
Lightweight services that run inside the range's netns on the mgmt bridge. Extensible framework — DNS is the first implementation, others (DHCP, NTP, syslog) follow the same pattern.

**SDK:**
```python
class MyLab(Range):
    name = "my-lab"
    
    # Built-in services — declared on the range
    dns = True                    # per-range dnsmasq on mgmt bridge
    # dhcp = True                 # future
    # ntp = True                  # future — solves freeze/thaw clock skew
    # syslog = True               # future — centralized logging

    def define_nodes(self):
        self.target = self.node("target", image="ubuntu-22.04")
        self.web = self.node("web", image="ubuntu-22.04")

    def verify(self):
        # Nodes can resolve each other by name
        self["target"].run("ping -c 1 web.my-lab")
        self["web"].run("ping -c 1 target.my-lab")
```

**DNS implementation:**
- dnsmasq runs inside the range netns, listening on the mgmt bridge gateway IP (.254)
- Auto-registers all nodes: `<node-name>.<range-name>` → mgmt IP
- Cloud-init configures VMs to use .254 as nameserver (already sets gateway, just add DNS)
- VyOS: `set system name-server <.254>` via serial console
- Containers: `--dns <.254>` or resolv.conf injection
- dnsmasq upstream: uses host DNS (via mgmt ns → host veth) when `internet="full"`, no upstream when `internet="none"`

**Extensible framework:**
```python
class RangeService:
    """Base class for per-range services."""
    def start(self, netns_name, mgmt_bridge, mgmt_subnet): raise NotImplementedError
    def stop(self): raise NotImplementedError
    def health_check(self) -> bool: raise NotImplementedError

class DNSService(RangeService):
    """dnsmasq — auto-registers node hostnames."""
    def start(self, netns_name, mgmt_bridge, mgmt_subnet):
        # ip netns exec <ns> dnsmasq --interface=mgmt-br --bind-interfaces ...
    def register_node(self, hostname, ip):
        # write to /ranges/<name>/dnsmasq.hosts, SIGHUP dnsmasq
    def stop(self):
        # kill dnsmasq PID

# User-defined services follow the same pattern
class SyslogService(RangeService):
    def start(self, netns_name, mgmt_bridge, mgmt_subnet):
        # rsyslogd inside the netns
```

**Service lifecycle:** Services start after namespace setup but before VM boot (so DNS is available during cloud-init). Services stop during range destroy (after VMs are killed).

- **Unit tests**: service start/stop commands (mocked), DNS hostname registration, cloud-init DNS config
- **Integration tests**: deploy range with DNS, verify nodes resolve each other by name, verify DNS works with internet="none" and internet="full"

### Phase 23: Windows Support
- UEFI boot, virtio drivers
- cloudbase-init / unattend.xml generation
- `powershell()` on Node/DependencySet
- WinRM for post-boot config
- WindowsDriver implementation (currently skeleton)
- **Unit tests**: Windows-specific dep resolution
- **Integration tests**: Windows VM boot, cloudbase-init, WinRM

### Phase 24: Parallel Test Isolation
**Issue**: `20260601-5-parallel-test-isolation.md` (full root-cause analysis)

Production multi-range on one host is already non-overlapping (single shared
StateDB allocates unique mgmt subnets; `topologies.name` PK enforces unique
names). But the *test suite* can't run in parallel: per-test temp DBs reset the
subnet allocator to `192.168.100.0/24` every time, and fixed range names
(`persist`/`sdkrange`/`topo*`/`nsr`) collide on netns/veth/seed/overlay paths
(all keyed on range name). Surfaced running the suite parallel on the 96-core
EC2 box (29/60 concurrent copies failed on `FileExistsError .../seeds/<range>`).

**Changes:**
- Unique per-run range names — prefix with a run/worker id (e.g. xdist
  `worker_id` or a uuid) so netns (`rangectl-<name>`), veth (`mgh<hash>`), and
  disk paths never collide across concurrent runs.
- Host-wide subnet source of truth — either one shared StateDB for all
  concurrent ranges, or a file-locked host registry, so independent test DBs
  don't both grab `.100.0`.
- Per-run seed/overlay roots for **integration** (the unit slice — SEED_ROOT/
  OVERLAY_ROOT — is already handled by the `_isolate_state_roots` autouse
  conftest fixture).
- Working `pytest-xdist` (EC2's pip index maxes at xdist 1.24.1, broken under
  pytest 9 — needs a venv/working index).

**Depends on**: `20260601-3` (StateDB read-lock) should land first — parallel
deploys hammer the shared sqlite connection harder.

- **Unit tests**: already isolated; verify N concurrent suite copies stay green.
- **Integration tests**: run Topo 1-7 + persistent + cli concurrently via xdist
  on EC2 with no subnet/netns/disk collisions.

**Priority**: low — pure test-infra speed/parallelism. Production correctness is
unaffected. Revisit when suite wall-clock becomes a bottleneck.

## Open Questions
- YAML export schema — define later
