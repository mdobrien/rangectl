# rangectl — Architecture, Design, and SDK Overview

**Rapid Automated Network Generation and Environment Control**

rangectl is a Python SDK for declarative VM testbed orchestration built on libvirt/QEMU and Linux kernel primitives. You define network topologies in Python — VMs, Docker containers, links with static IPs, dependencies, and readiness probes — and the engine deploys them as isolated "ranges," each running inside its own PID, network, and mount namespaces with a dedicated libvirtd instance. The DAG-based dependency resolver deploys nodes in parallel waves, defers link wiring until both endpoints pass health checks, and guarantees clean teardown by leveraging PID namespace reaping — killing libvirtd takes every QEMU process with it. Ranges get per-range cgroup v2 resource limits (memory, CPU, PIDs) with a cgroup freezer for atomic pause/resume, per-range internet policy via dedicated iptables chains (none or full NAT, runtime-toggleable), and a veth-based management network that gives the host SSH access to every VM without a jump box. The image layer uses qcow2 copy-on-write overlays so 20 VMs share one base image with millisecond provisioning. The SDK supports mixed VM and container topologies on shared bridges, VyOS router bootstrap via serial console, topology-wide snapshot/restore, link fault injection, composable dependency sets, Jinja2 templating with cross-node variable references, and YAML topology export/import. The codebase is 3,700 lines of framework code backed by 242 tests (222 unit, 20 integration) validated on EC2 c5.metal across seven test topologies covering multi-OS routing, diamond DAG deployments, multi-range isolation, and mixed VM/container connectivity.

---

## What rangectl Does Today

rangectl lets you define a network topology in Python — VMs, containers, links, IP addresses, dependencies — and deploy it as an isolated, self-contained environment called a **range**. Each range runs inside its own Linux kernel namespaces (network, PID, mount) with a dedicated libvirtd instance, so multiple ranges coexist on one host with structural isolation — not naming conventions or iptables hacks.

### Implemented Features

**Topology & Deployment**
- Declarative topology definition: nodes, links, static IPs, dependencies, readiness probes
- DAG-based dependency resolution with wave-parallel deployment
- Node state machine with verified transitions (DEFINED → PROVISIONING → READY → LINKED → RUNNING)
- Health checks: SSH reachable (default), port open, process running, or arbitrary command
- Deferred link wiring — bridges connect only when both endpoints are READY
- YAML topology export/import without deploying
- Context manager support for automatic teardown on exit

**Node Types**
- VMs via libvirt/QEMU with qcow2 copy-on-write overlays (millisecond provisioning)
- Docker containers with `--network=none` + veth wiring into the range's namespace
- Mixed VM + container topologies on shared bridges
- VyOS router support via serial console bootstrap (pexpect)

**Namespace Isolation (per range)**
- Network namespace: structural L2 isolation, clean bridge names (`mgmt-br`, `data-0`), no IFNAMSIZ hashing
- PID namespace: libvirtd as PID 1, kernel reaps all QEMU on teardown
- Mount namespace: per-range libvirt state dirs, shared image registry, dbus blocked
- Cgroup v2: per-range memory/CPU/PID limits, cgroup freezer for atomic pause/resume
- Veth pair management network: host routes into each range, SSH from host to any VM

**Internet Policy**
- Per-range iptables chains (`RANGE-<name>`) with MASQUERADE
- `internet="none"` (default) or `internet="full"` at deploy time
- Runtime toggle: `range.enable_internet()` / `range.disable_internet()`

**Configuration & Dependencies**
- Layered dependency model: packages → custom installs → configure functions → services
- Reusable `DependencySet` — composable, shareable dependency groups
- `@configure` decorator for custom Python running post-install
- Cloud-init seed ISO generation for Ubuntu; serial console for VyOS
- SSH exec and SFTP upload via Paramiko over the management network
- Jinja2 template rendering with cross-node variable references

**Lifecycle Operations**
- Topology-wide snapshot/restore via per-range libvirtd
- Freeze/thaw via cgroup freezer (atomic pause, zero CPU while frozen)
- Link toggle: `link.down()` / `link.up()` for fault injection
- Per-range virsh access: `virsh -c qemu+unix:///system?socket=<per-range-socket>`

**Image Management**
- Local image registry (SQLite metadata + qcow2 files) with add/list/remove
- qcow2 CoW overlay chains: 20 VMs share one base image
- Golden images are immutable and versioned (0o444 permissions)

**Testing**
- 216 unit tests (MockBackend + in-memory SQLite, run anywhere)
- Integration tests on EC2 c5.metal: Topo 1–7 covering 2-node, VyOS routing, services, diamond DAG + snapshot, link toggle, multi-range isolation, mixed VM + container
- Namespace-specific tests: freeze/thaw, internet policy, resource limits

### Not Yet Implemented
- **Persistent ranges** — ranges stay running after process exit but there's no `Range.connect(name)` to reconnect. Ephemeral (context manager) mode only.
- **ImageBuilder.build()** — stubbed. Use pre-built cloud images via `ImageRegistry.add()`.
- **CLI** — SDK-only for now (design decision D9). No `rangectl` command-line tool.
- **Windows support** — UEFI boot, cloudbase-init, WinRM (Phase 13).

---

## Table of Contents

1. [Problem & Vision](#1-problem--vision)
2. [System Overview](#2-system-overview)
3. [Range Primitive](#3-range-primitive)
4. [Kernel Primitives](#4-kernel-primitives)
5. [Networking](#5-networking)
6. [VM Image Management](#6-vm-image-management)
7. [Node State Machine](#7-node-state-machine)
8. [Python SDK](#8-python-sdk)
9. [Requirements Catalogue](#9-requirements-catalogue)
10. [Design Decisions](#10-design-decisions)
11. [Infrastructure & Deployment](#11-infrastructure--deployment)
12. [Implementation Status](#12-implementation-status)
13. [Comparison with GNS3](#13-comparison-with-gns3)
14. [Glossary](#14-glossary)

---

## 1. Problem & Vision

### 1.1 The Problem

Tools like **GNS3** have the right mental model — nodes, links, topologies as graphs — but suffer from brittle implementations:

- Race conditions in node lifecycle management.
- Treating "API returned 200" as "operation complete."
- Optimistic link creation before endpoints exist.
- A REST + Web UI surface that is painful to automate.

Other tools (Terraform, Vagrant) were never designed for network/security lab topologies. They orchestrate cloud or single-VM resources, not VM graphs with arbitrary L2/L3 topologies.

### 1.2 The Vision

**rangectl** keeps GNS3's intuitive topology model but rebuilds the orchestration on three guarantees:

1. **Lifecycle reliability** — every transition is verified, never assumed.
2. **Structural isolation** — Linux kernel namespaces and cgroups enforce separation, not policy or naming conventions.
3. **A Python SDK that reads like normal code** — declarative topology definition with imperative escape hatches.

### 1.3 Core Design Principles

- **Ranges are the primary abstraction.** A range is a self-contained topology of VMs (and containers) with isolated networking, resource limits, and lifecycle guarantees.
- **No dependencies beyond libvirt/QEMU.** Linux kernel primitives provide isolation and resource control. Libvirt is retained only for VM lifecycle management (domain XML, QEMU spawning, `virsh console`, snapshots).
- **SDK-first.** The Python SDK is the only public interface. A new engineer should reach a working topology in 20 minutes.
- **Lifecycle reliability above all.** Nodes transition through explicit states, dependencies resolve via DAG, and teardown is guaranteed clean by kernel reaping.

---

## 2. System Overview

### 2.1 Three-Layer Architecture

```
┌─────────────────────────────────────────────────────────┐
│                      Python SDK                         │
│   (Public API — the only interface users interact with) │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│                    Range Engine                          │
│  ┌──────────────┐ ┌──────────────┐ ┌─────────────────┐  │
│  │  State       │ │  Dependency  │ │  Image          │  │
│  │  Machine     │ │  Resolver    │ │  Registry       │  │
│  │              │ │  (DAG)       │ │                 │  │
│  └──────────────┘ └──────────────┘ └─────────────────┘  │
│  ┌──────────────┐ ┌──────────────┐ ┌─────────────────┐  │
│  │  Network     │ │  Lifecycle   │ │  Firewall       │  │
│  │  Manager     │ │  Manager     │ │  Manager        │  │
│  └──────────────┘ └──────────────┘ └─────────────────┘  │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│            Linux Kernel Primitives + Libvirt             │
│  ┌────────────┐ ┌────────────┐ ┌──────────┐             │
│  │  Network   │ │  PID       │ │  Mount   │             │
│  │  Namespace │ │  Namespace │ │  Namespace│            │
│  └────────────┘ └────────────┘ └──────────┘             │
│  ┌────────────┐ ┌────────────┐ ┌──────────┐             │
│  │  cgroups   │ │  libvirtd  │ │  veth    │             │
│  │   (v2)     │ │  (per-ns)  │ │  pairs   │             │
│  └────────────┘ └────────────┘ └──────────┘             │
└─────────────────────────────────────────────────────────┘
```

- **Layer 1 — Python SDK.** Declarative API for defining ranges, nodes, links, and policies. Users describe what they want.
- **Layer 2 — Range Engine.** State machines, dependency resolution, networking, lifecycle, image management. This is where orchestration logic lives.
- **Layer 3 — Kernel + Libvirt.** Direct kernel-primitive operations for isolation; libvirt for QEMU process spawning, domain XML, and snapshots. Critically, **one libvirtd runs inside each range's namespaces** rather than one global libvirtd.

### 2.2 Why Libvirt-per-Namespace

This is the central architectural decision. It was validated empirically on c5.metal / Ubuntu 22.04 / libvirt 8.0.

**Why not a single host-level libvirtd?** Libvirt creates TAP devices and bridges in whichever namespace it runs in. A host-level libvirtd cannot attach TAPs to bridges inside a range's network namespace without an explicit veth-pair-per-VM-interface workaround. Running libvirtd inside the range's namespaces lets it create TAPs, attach bridges, and spawn QEMU all in the correct namespace.

**Why not drop libvirt entirely?** Libvirt provides:

- Domain XML — declarative VM definitions.
- QEMU command construction across versions.
- Snapshot management.
- `virsh console` — break-glass serial console access when SSH or networking is broken.

Replacing all of that is ~500–800 lines of Python plus a QMP wrapper. Unnecessary. Libvirt-per-namespace preserves all the tooling and gains full isolation.

**What libvirt does NOT manage.** Bridges, TAPs, and the topology graph. The engine owns all networking directly. Domain XML uses `<interface type='bridge'><source bridge='mgmt-br'/></interface>` to attach to engine-managed bridges. Libvirt's own network definitions (`virsh net-define`) are deliberately never used — otherwise libvirt would spawn its own dnsmasq and fight the engine's network model.

### 2.3 Feasibility Validation Results

Spike on c5.metal / Ubuntu 22.04 / libvirt 8.0 (see `scratch/scripts/libvirtd-ns-experiment.sh`):

| Phase | Test | Result |
|---|---|---|
| A | libvirtd starts in PID+net+mount ns; virsh from host connects via per-range socket | Pass |
| B | Define & start VM through per-ns libvirtd; QEMU is child of libvirtd in the ns | Pass |
| C | Kill libvirtd → kernel SIGKILLs every process in the ns. Clean reap. | Pass |
| D | `virsh console` — captured 55,886 bytes of authentic Ubuntu boot output | Pass |
| E | Two concurrent ranges, fully isolated; tearing down range B leaves range A untouched | Pass |

---

## 3. Range Primitive

A **range** is the top-level unit of deployment. It encapsulates everything needed to run an isolated testbed.

### 3.1 What a Range Contains

```
Range "my-network-lab"
│
├── Network Namespace
│   ├── Management bridge (10.255.1.0/24)
│   ├── Data plane bridges (user-defined topologies)
│   └── Veth pair to host namespace (management access)
│
├── PID Namespace
│   └── libvirtd (PID 1, owns all QEMU processes)
│       ├── qemu: router
│       ├── qemu: server
│       └── qemu: client
│
├── Mount Namespace (required for libvirt state isolation)
│   ├── Per-range bind mounts over libvirt state dirs
│   ├── /var/lib/libvirt/images remains shared (image registry)
│   └── /run/dbus blocked (prevents systemd-machined cross-ns bug)
│
├── Cgroup (v2 — resource limits)
│   ├── memory.max  — hard memory ceiling
│   ├── cpu.max     — CPU time quota
│   ├── pids.max    — fork bomb protection
│   └── freezer     — pause/resume all processes
│
├── Network Policy (internet access control)
│   └── none | full
│
└── VM Instances
    ├── qcow2 overlays (copy-on-write on golden images)
    └── Per-VM configuration (vCPU, memory, interfaces)
```

### 3.2 Range Lifecycle States

```
DEFINED ──► PROVISIONING ──► READY ──► RUNNING ──► DESTROYING ──► DESTROYED
                                          │  ▲
                                          ▼  │
                                        FROZEN
```

| State | Meaning |
|---|---|
| **DEFINED** | Range spec exists, nothing allocated yet. |
| **PROVISIONING** | Namespaces created, cgroup allocated, qcow2 overlays created, libvirtd started, management network wired (veth pair + host route + iptables FORWARD rule established), QEMU processes starting. |
| **READY** | All VMs booted and passed health checks. Management network verified. Provisioning (Ansible-equivalent dependency injection) can begin. |
| **RUNNING** | Range is active. VMs are operational. |
| **FROZEN** | All processes suspended via cgroup freezer. Memory held, CPU released. Resume returns to RUNNING. See §3.4 caveats. |
| **DESTROYING** | libvirtd killed (PID namespace cleans up all QEMU processes), overlays deleted, cgroup removed, namespaces torn down. |
| **DESTROYED** | All resources released. Terminal state. |

### 3.3 Per-Range Process Tree

```
Range "lab-1"
├── PID Namespace
│   ├── PID 1: libvirtd (--config /ranges/lab-1/libvirtd.conf)
│   │   ├── PID N: qemu-system-x86_64 (router)
│   │   ├── PID N: qemu-system-x86_64 (server)
│   │   └── PID N: qemu-system-x86_64 (client)
│
├── Libvirt socket: /ranges/lab-1/run-libvirt/libvirt-sock
│   (on host filesystem, accessible from host)
│
└── Per-range config: /ranges/lab-1/etc-libvirt/
    ├── libvirtd.conf
    └── qemu.conf
```

**Memory overhead:** ~32 MB libvirtd RSS per range (observed), plus QEMU RSS per VM.

### 3.4 Freeze/Thaw Caveats

Freeze/thaw is valuable but has a known limitation: **clock skew on thaw.** When a range is frozen, vCPUs are paused. On thaw, wall time has jumped but guest monotonic time hasn't. Guests using kvm-clock (Linux default) handle the kernel-level clock adjustment. Userspace services with time-sensitive behavior (TLS validation, Kerberos tickets, NTP-locked services) can still break.

**Recommendation.** Freeze/thaw is best suited for short pauses (atomic snapshots, brief oversubscription). For long pauses (hours), snapshot-then-destroy and restore-from-snapshot is cleaner. The SDK supports a post-thaw hook:

```python
range.on_thaw(lambda vm: vm.exec("chronyc makestep"))
```

**NTP after thaw with `internet="none"`.** VMs can't reach external NTP. The host must run chrony as a server reachable via the veth pair on the management subnet, or accept clock drift after thaw.

**Use cases (short-duration):**

- **Atomic snapshots** — freeze range → snapshot all qcow2 overlays → thaw. All VM disk states are from the exact same moment.
- **Brief oversubscription** — run more ranges than cores available by time-slicing via freeze/thaw.
- **Pause inactive ranges** — CPU drops to zero; memory is held. Thaw on demand.

---

## 4. Kernel Primitives

### 4.1 Network Namespace

**Purpose:** Topology isolation. Each range gets its own network stack — interfaces, routes, bridges, and iptables rules invisible to other ranges.

**What this fixes vs. naive single-namespace designs:**

- Bridge name collisions disappear. Inside a netns, names are scoped — `data1` can be reused in every range.
- The 15-char IFNAMSIZ hashing problem goes away.
- iptables blast radius is bounded. Errant rules inside a range can't touch other ranges or the host.
- VM-to-VM L2 leakage across ranges becomes structurally impossible, not merely policy-impossible.

```
Host namespace                    Range "lab-1" namespace
┌─────────────────┐              ┌───────────────────────────────┐
│                 │  veth pair   │  mgmt-br (10.255.1.0/24)      │
│  host routing   │◄────────────►│   ├── tap-vm1 (vm1 mgmt)      │
│  table          │              │   ├── tap-vm2 (vm2 mgmt)      │
│                 │              │   └── tap-vm3 (vm3 mgmt)      │
│  routes:        │              │                               │
│  10.255.1.0/24  │              │  data-br-1 (10.0.1.0/24)      │
│   via veth-host │              │   ├── tap-vm1-data            │
│                 │              │   └── tap-vm2-data            │
│  10.255.2.0/24  │              │                               │
│   via veth-host │              │  data-br-2 (10.0.2.0/24)      │
└─────────────────┘              │   ├── tap-vm2-data2           │
                                 │   └── tap-vm3-data            │
Range "lab-2" namespace          └───────────────────────────────┘
┌───────────────────────────────┐
│  mgmt-br (10.255.2.0/24)      │
│   ├── tap-vm1                 │
│   └── tap-vm2                 │
└───────────────────────────────┘
```

- Each range's management network gets its own /24 (auto-assigned or user-specified).
- A veth pair connects the range's management bridge to the host namespace.
- The host has routes to every range's management subnet.
- SSH runs from the host (or remotely via the host) and reaches all VMs across all ranges via the veth endpoints.
- Data plane bridges are internal to the range — the user defines the topology and the engine creates bridges and TAPs accordingly.

**No jump box required.** The host itself is the management gateway via veth pairs.

### 4.2 PID Namespace

**Purpose:** Lifecycle management and process isolation.

Each range runs in its own PID namespace with libvirtd as PID 1. All QEMU processes are children of libvirtd. This provides two guarantees:

1. **Clean teardown.** Killing libvirtd's host-PID causes the kernel to kill every process in the namespace — every QEMU. No orphan processes, no zombies, no leaked resources. One kill, guaranteed clean.
2. **Process isolation.** QEMU processes in one range cannot see or signal processes in another range.

**Important:** reap by killing libvirtd's host-PID, not the unshare wrapper process. The unshare process is in the host namespace; libvirtd is PID 1 inside the ns.

### 4.3 Mount Namespace (Required)

**Purpose:** Libvirt state isolation between concurrent ranges. Mount namespace is **not optional** — without it, multiple libvirtd instances collide on hardcoded paths like `/run/libvirt/network/driver.pid`.

The supervisor bind-mounts per-range directories over libvirt's state paths:

| Host Path | Bind-mounted Per Range | Notes |
|---|---|---|
| `/run/libvirt` | `/ranges/<name>/run-libvirt` | Sockets, PID files, runtime state |
| `/var/log/libvirt` | `/ranges/<name>/log-libvirt` | Per-range logs |
| `/var/cache/libvirt` | `/ranges/<name>/cache-libvirt` | Cache |
| `/etc/libvirt` | `/ranges/<name>/etc-libvirt` | Per-range qemu.conf + libvirtd.conf |
| `/var/lib/libvirt/qemu` | `/ranges/<name>/lib-libvirt/qemu` | Domain state |
| `/var/lib/libvirt/dnsmasq` | `/ranges/<name>/lib-libvirt/dnsmasq` | DHCP leases (if any) |
| `/var/lib/libvirt/boot` | `/ranges/<name>/lib-libvirt/boot` | Boot files |
| `/var/lib/libvirt/swtpm` | `/ranges/<name>/lib-libvirt/swtpm` | TPM state |
| `/run/dbus` | `<empty dir>` | **Blocks dbus** — see below |

**Critical: `/var/lib/libvirt/images` is NOT bind-mounted.** This is the shared image registry. All ranges read base qcow2 images from this path via their backing-file chains.

**Critical: `/run/dbus` must be blocked.** Without this, libvirt calls systemd-machined across the PID namespace boundary. The host receives local PIDs from the namespace and rejects them. Bind-mounting an empty directory over `/run/dbus` makes libvirt fall back to manual cgroup placement.

### 4.4 Cgroups (v2)

**Purpose:** Resource control and the freeze/thaw mechanism.

Each range gets a cgroup at `/sys/fs/cgroup/<range-name>/`:

| Controller | File | Purpose | Example |
|---|---|---|---|
| memory | `memory.max` | Hard memory ceiling for all VMs in range | `32G` |
| cpu | `cpu.max` | CPU time quota (microseconds per period) | `800000 100000` (8 cores) |
| pids | `pids.max` | Max process count (fork bomb protection) | `500` |
| cpuset | `cpuset.cpus` | Pin to specific CPU cores (optional) | `0-7` |
| freezer | `cgroup.freeze` | Pause/resume all processes atomically | `1` (freeze) / `0` (thaw) |

**Implementation note.** The supervisor writes its own PID into the per-range cgroup *before* calling `unshare`, so all descendants (libvirtd + QEMU) are born into the cgroup.

### 4.5 Supervisor Runbook

The empirically validated recipe for launching a range. ~30 lines of shell, translated to Python in `rangectl/supervisor.py`:

```
Step 1:  unshare --pid --fork --net --mount --uts \
           --propagation private --mount-proc

Step 2:  Bind-mount per-range directories (see §4.3 table).
         DO NOT bind over /var/lib/libvirt/images (shared registry).

Step 3:  Block dbus:
           mount --bind <empty-dir> /run/dbus

Step 4:  Per-range qemu.conf:
           security_driver = "none"
           stdio_handler   = "file"
           dynamic_ownership = 0
           user  = "root"
           group = "root"

Step 5:  exec /usr/sbin/libvirtd \
           --config /ranges/<name>/libvirtd.conf \
           --pid-file /run/libvirt/libvirtd.pid
```

---

## 5. Networking

### 5.1 Management Network

Every range gets an auto-provisioned management network:

1. A Linux bridge (`mgmt-br`) inside the range's network namespace.
2. Every VM's last interface (a dedicated mgmt NIC, does not shift user-declared `eth0`/`eth1`) connects to this bridge.
3. A veth pair links the bridge to the host namespace.
4. The host has a route to the range's management subnet.
5. An iptables `FORWARD ACCEPT` rule for the management CIDR on the host (without this, the host kernel drops the first SSH SYN even with a route present).
6. Management IPs are auto-assigned from the subnet.

Transparent to the user — they define a range with `mgmt_network="10.255.1.0/24"` and everything is wired automatically.

### 5.2 Provisioning Order of Operations

The management network infrastructure must be established **before** VMs boot. Health checks and post-boot configuration (SSH, dependency injection) require the host to have a route and forwarding rules into the range's management subnet.

```
Step 1: Create network namespace              ─┐
Step 2: Create mgmt bridge in namespace        │ Infrastructure setup
Step 3: Create veth pair                       │ (seconds, deterministic,
Step 4: Add host route to mgmt subnet          │  no VM involvement)
Step 5: Add iptables FORWARD rule for CIDR     │
Step 6: Start libvirtd in namespace            ─┘
Step 7: Start QEMU processes (VMs boot)        ─ VM startup
Step 8: Health checks confirm VMs reachable    ─ Readiness verification
Step 9: Post-boot configuration (dep injection)─ User-defined setup
```

Steps 1–6 are pure infrastructure plumbing and complete in seconds. Step 7 starts VMs. Steps 8–9 happen after boot.

### 5.3 Cloud-Init: Default Route and DNS

VMs need a default route and nameservers configured inside the guest. Cloud-init handles this for images that support it. For images that don't (or where cloud-init is unreliable), post-boot configuration applies the settings. A per-VM concern, not a namespace concern.

### 5.4 Data Plane Network

The user defines the topology — which VMs connect to which, on which interfaces, with which subnets. The engine creates bridges and TAP interfaces inside the range's network namespace accordingly. Libvirt handles TAP creation as part of VM startup since it's running inside the namespace.

```python
topo = Topology("my-network-lab")

router = topo.node("router", image="vyos-1.4")
server = topo.node("server", image="ubuntu-22.04")
client = topo.node("client", image="ubuntu-22.04")

topo.link(router.eth1["10.0.1.1/24"], server.eth0["10.0.1.2/24"])
topo.link(router.eth2["10.0.2.1/24"], client.eth0["10.0.2.2/24"])
```

Two data plane bridges are created inside the namespace, with TAP interfaces for each VM endpoint.

### 5.5 Internet Access Policy

Per-range outbound internet control via dedicated iptables/nftables chains on the host. The veth pair is the choke point — all traffic to/from a range flows through it.

| Policy | Behavior |
|---|---|
| `none` (default) | No internet. VMs can reach each other and the host can reach VMs for management. |
| `full` | NAT (masquerade) all range traffic out through host's internet connection. |

**Implementation.** Each range gets a dedicated chain (`RANGE-<name>`). Tearing down a range flushes and deletes only its chain — no risk of affecting other ranges.

```python
range = Range("my-lab", topology=topo, internet="full")

# Runtime control
range.enable_internet()
range.disable_internet()
```

### 5.6 Developer Access to VMs

Devs should not SSH to the host. Two options depending on environment:

**Option 1: Routable management subnets (preferred).** Make the management subnets (e.g., `10.255.0.0/16`) routable from the dev's machine. The host acts as a router, not a destination. Fully transparent — devs SSH directly to VM management IPs.

- **On EC2:** Add a VPC route table entry `10.255.0.0/16 → host instance ENI`.
- **On-prem with gateway access:** static route on the gateway.
- **On-prem without gateway access:** WireGuard VPN with `10.255.0.0/16` routed through the tunnel.

```bash
# From dev's laptop — all transparent:
ssh dev@10.255.1.10        # SSH to lab-1 VM
ssh -X dev@10.255.2.10     # SSH with X11 to lab-2 VM
```

**Option 2: SSH ProxyJump (fallback).** When routing isn't possible. A single `tunnel` account on the host with no shell, forwarding only.

```
# /etc/ssh/sshd_config on host
Match User tunnel
    PermitTTY no
    X11Forwarding no
    ForceCommand /bin/false
    AllowTcpForwarding yes
    PermitOpen 10.255.0.0/16:*
```

```bash
ssh -X -J tunnel@10.0.0.1 dev@10.255.1.10
ssh -J tunnel@10.0.0.1 -N -L 3389:10.255.1.20:3389 dev@10.255.1.10
```

X11 forwarding works because ProxyJump is a TCP tunnel — the SSH session is end-to-end. The host needs no X11 libraries or config.

---

## 6. VM Image Management

### 6.1 qcow2 Copy-on-Write Architecture

The platform uses QEMU's native qcow2 backing file mechanism for efficient image management.

```
Image Registry (immutable, read-only, /var/lib/libvirt/images — shared)
├── ubuntu-22.04-base.qcow2          (8 GB, golden image)
├── centos-9-base.qcow2              (6 GB, golden image)
└── vyos-1.4-base.qcow2              (2 GB, golden image)

Range Overlays (thin, per-VM, writable)
├── ranges/lab-1/images/
│   ├── router.qcow2     → backing: .../vyos-1.4-base.qcow2     (~200 KB initially)
│   ├── server.qcow2     → backing: .../ubuntu-22.04-base.qcow2  (~200 KB initially)
│   └── client.qcow2     → backing: .../ubuntu-22.04-base.qcow2  (~200 KB initially)
└── ranges/lab-2/images/
    ├── node-1.qcow2     → backing: .../ubuntu-22.04-base.qcow2
    └── node-2.qcow2     → backing: .../centos-9-base.qcow2
```

**Key properties.**

- Golden images are immutable and versioned. Once registered, they are set read-only (0o444).
- Per-VM overlays are created in milliseconds.
- 20 Ubuntu VMs share one 8 GB base image. Total disk is 8 GB + (20 × delta), not 160 GB.
- Destroying a range deletes only overlay files. Base images are untouched.
- Overlays can be chained: `base → customized → per-range`.

### 6.2 Registry Path Is an On-Disk Contract

The qcow2 backing file path is stored as an absolute path inside each overlay file. `qemu-img info --backing-chain` resolves paths at runtime, not bake-time. **Moving or renaming the image registry directory breaks every overlay that references it.**

Treat the registry path as immutable infrastructure. If it must change, use `qemu-img rebase` to update affected overlays. This path is shared into every range's mount namespace (supervisor deliberately does *not* bind-mount over `/var/lib/libvirt/images`).

### 6.3 Overlay Chains

```
ubuntu-22.04-base.qcow2              (golden, read-only, 8 GB)
  └── ubuntu-webstack.qcow2          (nginx + postgres pre-installed, read-only, ~500 MB delta)
        ├── lab-1/server.qcow2       (user 1's overlay, starts ~200 KB)
        └── lab-2/server.qcow2       (user 2's overlay, starts ~200 KB)
```

Resetting a user's environment = delete their overlay, create a fresh one. Sub-second operation.

**Constraint.** A base image cannot be modified once overlays reference it. Overlays store block offsets into the backing file. The image registry enforces this — base images are immutable and versioned.

### 6.4 Snapshot and Rollback

Snapshots use libvirt's snapshot management (preserved via the per-namespace libvirtd), combined with the cgroup freezer for consistency:

```python
range.freeze()
range.snapshot("baseline")  # libvirt snapshot per VM, all from the same instant
range.thaw()

# Later...
range.freeze()
range.rollback("baseline")
range.thaw()
```

---

## 7. Node State Machine

Every node (VM or container) transitions through deterministic states. Transitions happen when preconditions are **verified**, not assumed. This is the core fix for GNS3's race conditions.

```
DEFINED ──► PROVISIONING ──► BOOTING ──► READY ──► LINKED ──► RUNNING
                                                                  │
                                                           DESTROYING
                                                                  │
                                                             DESTROYED
```

| State | What's happening | Exit condition |
|---|---|---|
| DEFINED | Node spec exists in topology graph | Engine begins provisioning |
| PROVISIONING | qcow2 overlay created, TAPs allocated | Disk and network resources confirmed |
| BOOTING | QEMU process started via libvirt | Health check passes |
| READY | VM up, management interface reachable | All dependencies also READY |
| LINKED | Data plane interfaces wired to bridges | Both endpoints of every link confirmed |
| RUNNING | Fully operational | User or engine initiates teardown |
| DESTROYING | QEMU process terminated, resources released | All resources confirmed freed |
| DESTROYED | Terminal state | — |

### 7.1 Health Checks

A node is not READY until a health check passes. This replaces "API returned 200 → up."

Health checks run from the host via the management network (SSH over veth route). The management network infrastructure is established during range provisioning, *before* any VMs boot.

```python
# Default: SSH port reachable on management interface (L2)
server = topo.node("server", image="ubuntu-22.04")

# L3 — specific service must be running
dns_server = topo.node("dns", image="ubuntu-22.04",
    ready_when=port_open(53, proto="udp"))

# L3 — HTTP endpoint returns 200
web = topo.node("web", image="ubuntu-22.04",
    ready_when=http_check("/health", status=200))

# L3 — arbitrary command exits 0 (via SSH)
db = topo.node("db", image="ubuntu-22.04",
    ready_when=command_succeeds("pg_isready"))
```

**Three readiness levels:**

- **L1** — hypervisor reports running. What GNS3 does. Nearly useless.
- **L2** — OS booted, ping (or SSH) passes on mgmt interface. **Default.**
- **L3** — service ready, user-defined via `ready_when=…`.

### 7.2 Dependency Resolution

Dependencies between nodes are modeled as a DAG. The engine topo-sorts and deploys in **waves** — nodes within a wave start in parallel; the next wave blocks until the current wave is READY.

```python
router = topo.node("router", image="vyos-1.4")
dhcp   = topo.node("dhcp",   image="ubuntu-22.04",
    depends_on=[router])
client = topo.node("client", image="ubuntu-22.04",
    depends_on=[dhcp],
    ready_when=port_open(22))

# Deploy order:
# Wave 1: router (no dependencies)
# Wave 2: dhcp   (waits for router READY)
# Wave 3: client (waits for dhcp READY)
```

**Links are deferred.** A `Link` object is created in the graph immediately, but actual bridge/TAP wiring only happens when **both** endpoints are in READY state. This eliminates GNS3's race condition of wiring links to nodes that aren't booted yet.

---

## 8. Python SDK

### 8.1 Design Goals

- A new engineer reads the README and has a working 3-node topology in 20 minutes.
- Declarative-first with imperative escape hatches.
- No knowledge of namespaces, cgroups, veth pairs, or qcow2 required.

### 8.2 At-a-Glance Example

```python
from rangectl import Range, Topology, Resources, port_open

topo = Topology("my-network-lab")

router = topo.node("router", image="vyos-1.4",      vcpu=2, memory=2048)
server = topo.node("server", image="ubuntu-22.04",  vcpu=2, memory=4096)
client = topo.node("client", image="ubuntu-22.04",
    memory=2048, depends_on=[router], ready_when=port_open(22))

topo.link(router.eth0["10.0.1.1/24"], server.eth0["10.0.1.2/24"])
topo.link(router.eth1["10.0.2.1/24"], client.eth0["10.0.2.2/24"])

range = Range("my-network-lab",
    topology=topo,
    mgmt_network="10.255.1.0/24",
    resources=Resources(memory="32G", cpus=8),
    internet="full")

range.deploy()                                  # blocks until all nodes RUNNING

range["server"].exec("apt install -y nginx")
server_ip = range["server"].ip("eth0")

range.snapshot("baseline")
range.freeze()
range.thaw()
range.rollback("baseline")

range.destroy()                                 # guaranteed clean
```

### 8.3 Context Manager Support

```python
with topo.deploy() as rng:
    run_tests(rng)
# Automatically destroyed on exit, even on exception.
```

### 8.4 YAML Configuration

```yaml
# range.yaml
name: my-network-lab
mgmt_network: 10.255.1.0/24
internet: full
resources:
  memory: 32G
  cpus: 8

nodes:
  - name: router
    image: vyos-1.4
    vcpu: 2
    memory: 2048
    interfaces: [eth0, eth1]
  - name: server
    image: ubuntu-22.04
    vcpu: 2
    memory: 4096
  - name: client
    image: ubuntu-22.04
    memory: 2048
    depends_on: [router]
    ready_when:
      type: port
      port: 22

links:
  - endpoints: [router.eth0, server.eth0]
    network: 10.0.1.0/24
  - endpoints: [router.eth1, client.eth0]
    network: 10.0.2.0/24
```

```python
topo = Topology.from_yaml("range.yaml")
topo.deploy()
```

### 8.5 SDK API Reference

#### Topology

The root object. Declares nodes, links, and drives deployment.

```python
topo = Topology(name)
```

| Method | Returns | Description |
|---|---|---|
| `topo.node(name, image, vcpu=1, memory=1024, os="linux", depends_on=None, ready_when=None)` | `Node` | Declare a node. |
| `topo.link(if_a, if_b)` | `Link` | Declare a link between two interfaces with static IPs. |
| `topo.deploy(cleanup_on_fail=True)` | `Range` | Deploy; returns a context-managed live `Range`. |
| `topo.export(path)` | — | Export topology to YAML. Works without deploying. |
| `Topology.from_yaml(path)` | `Topology` | Reconstruct from a previously exported YAML. |
| `topo.destroy()` | — | Destroy all resources. Called automatically by ctx-mgr exit. |

#### Node

Returned by `topo.node()`. Declares interfaces, dependencies, and configuration.

- **Interface access:** `node.ethN` → `InterfaceSpec`.
- **IP binding:** `node.ethN["10.0.1.1/24"]` → `InterfaceSpec` with IP set.
- **IP lookup:** `node.ethN.ip` (e.g., inside a `@configure` function).

Dependency methods (also available on `DependencySet`):

| Method | Purpose |
|---|---|
| `node.packages(["nginx", "curl"])` | Install system packages; engine resolves the package manager from the image. |
| `node.powershell("Install-…")` | Run a PowerShell command (Windows). |
| `node.install(name, src, install_cmd, verify_cmd=None)` | Install custom software from a local archive. |
| `node.file(dst, src)` | Upload a file. |
| `node.user(name, ssh_key=None, password=None)` | Create a user. |
| `node.run_on_boot("systemctl enable nginx")` | Register a command for first boot. |
| `node.service(name, enabled=False, start_cmd=None, ready_when=None)` | Declare a service. |
| `@node.configure` | Register a Python function to run after deps install, before services start. |
| `node.apply(dep_set)` | Apply a reusable `DependencySet`. |

```python
@target.configure
def setup_app(node):
    node.exec("mkdir -p /opt/app")
    node.upload("./app/", "/opt/app/")
    db_ip = db_server.eth0.ip
    node.template("./templates/config.j2", "/opt/app/config.yaml",
                  vars={"db_host": db_ip, "env": "test"})
```

#### DependencySet

Reusable, composable collection of dependencies. Same methods as `Node`.

```python
web_stack = DependencySet("web-stack")
web_stack.packages(["nginx", "certbot", "flask", "gunicorn"])
web_stack.service("nginx", enabled=True)

target.apply(web_stack)
```

Cross-platform sets are supported via the `os` parameter:

```python
ad = DependencySet("active-directory", os="windows")
ad.packages(["rsat-ad-tools"])
ad.powershell("Install-WindowsFeature AD-Domain-Services")
```

#### Range

Live handle to a deployed topology. Returned by `topo.deploy()`.

| Method | Purpose |
|---|---|
| `rng[node_name]` → `LiveNode` | Access a running node. |
| `rng.link(node_a, node_b)` → `Link` | Access a link for toggling. |
| `rng.freeze()` / `rng.thaw()` | Suspend/resume all VMs via cgroup freezer. |
| `rng.enable_internet()` / `rng.disable_internet()` | Runtime internet policy toggle. |
| `rng.snapshot(name)` / `rng.restore(name)` | Topology-wide snapshot/restore. |
| `rng.logs(level=None)` | Fetch structured logs (filterable by level). |

Teardown is handled via the context manager (`with topo.deploy() as rng:`) or by calling `engine.destroy(topology)` directly.

#### LiveNode

Handle to a running node. Accessed via `rng["node_name"]`.

| Method | Purpose |
|---|---|
| `live_node.mgmt_ip` | Management network IP. |
| `live_node.exec(cmd)` → `ExecResult` | Run a command via SSH over mgmt. |
| `live_node.upload(src, dst)` | Upload a file or directory via SFTP. |
| `live_node.template(src, dst, vars=None)` | Render a Jinja2 template and upload the result. |
| `live_node.logs(level=None)` | Logs for this node only. |
| `live_node.snapshot(name)` / `live_node.restore(name)` | Per-node snapshot/restore. |

```python
@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
```

#### Link

Handle to a link between two nodes.

```python
rng.link("router", "target").down()   # simulate link failure
rng.link("router", "target").up()     # restore
```

#### Readiness Probes

Factory functions returning `ReadinessProbe`. Used in `ready_when=…`.

```python
from rangectl import port_open, ping, process_running, command_succeeds
```

| Probe | Description |
|---|---|
| `port_open(port, timeout=300)` | TCP port accepting connections. |
| `ping(timeout=300)` | ICMP reachability on mgmt interface (default L2). |
| `process_running(name, timeout=300)` | Named process running. |
| `command_succeeds(cmd, timeout=300)` | Command exits with code 0. |

All probes have a `timeout` (max seconds) and check every 5 seconds.

#### ImageRegistry & ImageBuilder

```python
from rangectl import ImageRegistry, ImageBuilder

registry = ImageRegistry()
registry.add("ubuntu-22.04",   "./ubuntu-22.04.qcow2", inject="cloud-init")
registry.add("win-server-2022","./win2022.qcow2",      inject="cloudbase-init")
registry.add("my-custom",      "./custom.qcow2")        # default: pre-baked
```

Inject methods:

| Value | When to use |
|---|---|
| `pre-baked` (default) | Image already has SSH key + guest agent (ImageBuilder output). |
| `cloud-init` | Linux images shipping cloud-init (Ubuntu, Debian, Fedora, Kali, VyOS). |
| `cloudbase-init` | Windows images with cloudbase-init. |
| `guest-agent` | Explicit opt-in — requires QEMU guest agent. |

```python
web_server = (ImageBuilder("ubuntu-22.04")
    .packages(["nginx", "curl", "htop"])
    .run("systemctl enable nginx")
    .build("my-web-server:v1"))
```

`ImageBuilder` supports chainable `packages()` and `run()` methods. `build()` is stubbed (raises `NotImplementedError`) — image baking is deferred to a future phase. For now, use pre-built cloud images registered via `ImageRegistry.add()`.

#### Operational Commands (planned CLI — not yet implemented)

The SDK is the only interface today (per D9). A future CLI will wrap the SDK:

```bash
rangectl list                                  # list all ranges
rangectl exec lab-1 -- ip link show            # run command inside netns
rangectl virsh lab-1 list                      # virsh scoped to a range
rangectl virsh lab-1 console router            # break-glass serial console
rangectl freeze  lab-1
rangectl thaw    lab-1
rangectl destroy lab-1
```

Until then, the equivalent operations are available via the Python SDK or direct virsh/ip-netns commands with the per-range socket path.

### 8.6 Full End-to-End Example

```python
from rangectl import Topology, DependencySet, ImageBuilder, port_open

# Reusable dep sets
web_stack = DependencySet("web-stack")
web_stack.packages(["nginx", "certbot", "flask", "gunicorn"])
web_stack.service("nginx", enabled=True)

monitoring = DependencySet("monitoring")
monitoring.install(name="node-exporter", src="./builds/node-exp.tar.gz",
                   install_cmd="./install.sh")
monitoring.service("node-exporter",
    start_cmd="/usr/local/bin/node_exporter",
    ready_when=port_open(9100))

# Topology
topo = Topology("pentest-lab")

router   = topo.node("router", image="vyos-1.4", vcpu=2, memory=2048)
attacker = topo.node("kali",   image="kali-2024", vcpu=4, memory=4096)
target   = topo.node("target", image="ubuntu-22.04", memory=2048,
                     depends_on=[router],
                     ready_when=port_open(22))

target.apply(web_stack)
target.apply(monitoring)

target.install(name="vuln-app", src="./builds/vuln-app.tar.gz",
               install_cmd="./install.sh",
               verify_cmd="curl -s localhost:5000/health")

@target.configure
def setup_target(node):
    gateway = router.eth1.ip
    node.template("./templates/network.j2",
                  "/etc/network/interfaces.d/static",
                  vars={"gateway": gateway})
    node.exec("systemctl restart networking")

topo.link(router.eth0["10.0.1.1/24"], attacker.eth0["10.0.1.2/24"])
topo.link(router.eth1["10.0.2.1/24"], target.eth0["10.0.2.2/24"])

# Export without deploying
topo.export("pentest-lab.yaml")

# Deploy and interact
with topo.deploy() as rng:
    print(rng["target"].mgmt_ip)
    print(rng["target"].exec("hostname"))

    rng["target"].exec("systemctl start vuln-app")
    rng["attacker"].exec("nmap -sV 10.0.2.2")

    rng.link("router", "target").down()
    rng["attacker"].exec("ping -c 3 10.0.2.2")
    rng.link("router", "target").up()

    rng.snapshot("post-attack")

    for entry in rng["target"].logs(level="error"):
        print(entry["timestamp"], entry["message"])
```

---

## 9. Requirements Catalogue

The hardened requirements for v1. Each is the answer to a specific failure mode in GNS3 (or in cloud-orchestration tools repurposed for testbeds).

| ID | Requirement |
|---|---|
| **R1** | **Declarative topology deployment.** User declares nodes, links, IPs; engine does topo-sort, starts in waves, waits for readiness, wires links when both sides are up. |
| **R2** | **Imperative interaction post-deploy.** After deploy: inject traffic, run exploits, break links, observe behavior. |
| **R3** | **Named topologies with isolation.** Multiple topologies coexist on one host. Name prefixes all resources. Independent lifecycle. |
| **R4** | **Explicit dependencies with readiness.** `depends_on=…` resolves via DAG. Nothing proceeds until readiness confirmed. L1/L2/L3 readiness model. |
| **R5** | **Static IPs in topology declaration.** Interfaces and IPs declared inline so topology is fully described before deploy. |
| **R6** | **Management network.** Every range gets an isolated mgmt bridge. Host on every mgmt bridge at .254. All engine ops over mgmt, never over user topology links. Subnets allocated as sequential /24s. |
| **R7** | **Image registry and builder.** Local registry with add/list/remove. `ImageBuilder` bakes custom images via boot-and-snapshot. |
| **R8** | **Composable dependency model.** Layered: system packages → language packages → custom installs → configure → services. Reusable via `DependencySet`. |
| **R9** | **Custom Python in configuration.** `@configure` decorators register callables running after deps install, before services start. Real Python, with cross-node references. |
| **R10** | **Windows support.** First-class. Platform-specific (`choco`, `powershell`), generic (`file`, `user`, `service`) work cross-platform. UEFI boot, virtio drivers, cloudbase-init / unattend.xml. |
| **R11** | **Topology export without deploy.** YAML export from declared topology, importable. |
| **R12** | **SDK-only for v1.** Python SDK is the only interface. CLI wraps it later. |
| **R13** | **Deployable on any Ubuntu box.** `apt install qemu-kvm libvirt-daemon-system && pip install rangectl`. |
| **R14** | **Gated TDD workflow.** Unit tests gate commits; integration tests gate merges. |
| **R15** | **Separated test layers.** `tests/unit/` (MockBackend, in-memory SQLite, no infra) and `tests/integration/` (real libvirt, KVM host). |

---

## 10. Design Decisions

The locked-in decisions and the reasoning behind each.

### D1 — Libvirt over Proxmox
**Reason.** Proxmox requires installing a full OS — a dealbreaker for adoption. `pip install on any Ubuntu box` is the install story we want. We only need a narrow slice of Proxmox (create, configure, snapshot, destroy). Backend protocol leaves Proxmox addable later.

### D2 — Declarative deploy, imperative interact
**Reason.** Declarative eliminates race conditions by making ordering the engine's problem. Pure declarative is too rigid for testbed scenarios where you poke things mid-run.

### D3 — Readiness over optimism
**Reason.** GNS3's core failure: "API returned 200" ≠ "operation complete." Our engine probes. Default L2 (ping/SSH); L3 user-defined.

### D4 — One management bridge per topology (not global)
**Reason.** Topology isolation. Nodes in one topology can't see mgmt traffic from another. Lets us add a jump box later for multi-user cleanly.

### D5 — Host as jump box (for now)
**Reason.** A jump box VM adds boot time, memory, disk just for SSH. The host already has connectivity. Mgmt-bridge isolation makes adding it clean when needed.

### D6 — Boot-and-snapshot for ImageBuilder
**Reason.** Uses the same mechanisms as deploy-time config (cloud-init, exec, apt). Same code paths, same behavior, no surprises.

### D7 — Config injection abstracted from the user
**Reason.** Users shouldn't need to know cloud-init YAML schema or differentiate cloud-init vs cloudbase-init. They express intent; engine handles mechanism.

### D8 — DependencySet for reuse
**Reason.** Teams build libraries of `DependencySet`s and share them. Same vocabulary across `ImageBuilder` and `Node` — two execution strategies.

### D9 — SDK-only, no CLI in v1
**Reason.** CLI adds a second surface to maintain. SDK-first forces the API to be good enough to stand alone.

### D10 — Sequential /24 mgmt subnets
**Reason.** Simple, deterministic, no DHCP. `.1, .2, …` by index; host at `.254`.

### D11 — Linux bridges, not OVS
**Reason.** Simpler, fewer deps, good enough for single-host. OVS later for traffic shaping/mirroring.

### D12 — Single SQLite DB for state
**Reason.** Atomic transactions for free during deploy/destroy. One file to back up or inspect.

### D13 — Cleanup on failure (default)
**Reason.** Fail-fast without cleanup leaves orphans. Debug mode (`cleanup_on_fail=False`) for inspection.

### D14 — SSH via Paramiko, not Ansible
**Reason.** Ansible is a large dep with its own YAML DSL — over-engineering for SSH commands. Paramiko gives exec, SFTP, key-based auth in native Python.

### D15 — Pre-baked is the default inject method
**Order of preference.** `pre-baked` → `cloud-init` → `cloudbase-init` → `guest-agent` (opt-in).
**Reason.** Cloud-init is not universal. Pre-baked sidesteps the problem for any image you build yourself.

### D16 — Structured logging in SQLite
**Reason.** GNS3 failures are opaque. We log every state transition, SSH command, readiness probe — keyed by topology and node — for post-mortems.

### D17 — qcow2 CoW overlays as the only strategy
**Reason.** 5 Ubuntu nodes shouldn't mean 5 copies of a 4 GB base image. Fast create, minimal disk, cheap snapshots.

### D18 — Resource validation before deploy
**Reason.** Fail before wasting 10 minutes booting VMs only to OOM on node 8 of 10.

### D19 — No serial console as a workaround for broken SSH
**Reason.** If SSH doesn't work, something is fundamentally wrong (key injection failed, network misconfigured). Surface as a clear error, don't paper over with a serial fallback.

> Note: `virsh console` is preserved as a *debugging* tool for VyOS bootstrap and break-glass scenarios; it is not a normal control plane path.

### D20 — Wave-based parallel deploy
**Reason.** Topo-sort produces waves. Threading is sufficient for single-host — no need for asyncio.

### D21 — DNS deferred (nice-to-have)
**Reason.** No per-topology dnsmasq for now. Cross-node references use node objects (`target.eth0.ip`) rather than hostname resolution.

### D22 — Gated TDD with separated test layers
**Reason.** Agents need fast feedback loops. Unit tests in seconds; integration in minutes on EC2.

### D23 — MockBackend for unit testing
**Reason.** The `Backend` protocol allows the entire engine (wave ordering, readiness flow, dependency injection) to be tested without libvirt. Paired with in-memory SQLite.

---

## 11. Infrastructure & Deployment

### 11.1 Host Requirements

- Linux kernel 5.10+ (cgroups v2, all namespace types)
- KVM enabled (`/dev/kvm` accessible)
- QEMU 6.0+
- libvirt 8.0+ (validated)
- Python 3.10+

### 11.2 EC2 Reference Sizing

The platform targets AWS EC2 bare metal instances with KVM. **Metal instances are required for v1** — nested KVM on non-metal Nitro instances is region- and instance-type-limited.

| Instance | vCPUs | RAM | On-Demand/hr | Use Case |
|---|---|---|---|---|
| c5.metal | 96 | 192 GiB | ~$4.08 | Dev / testing, cheapest metal |
| c6a.metal | 192 | 384 GiB | ~$5.50 | Most cores per dollar |
| m5.metal | 96 | 384 GiB | ~$4.61 | Memory-heavy topologies |
| m6a.metal | 192 | 768 GiB | ~$5.98 | Large ranges, best RAM density |
| i3.metal | 72 | 512 GiB | ~$4.99 | Fast boot (local NVMe for images) |

**Cost optimization.** Spot for metal types reduces cost 60–70%. Testbed workloads tolerate interruption — combine with freeze/snapshot for graceful handling.

### 11.3 Testing Gates

| Gate | Scope | Where it runs |
|---|---|---|
| **Gate 1** | `pytest tests/unit` — MockBackend + in-memory SQLite | Anywhere |
| **Gate 2** | `pytest tests/integration` — real VMs, bridges, SSH | EC2 (KVM required) |

Gate 1 gates every commit (216 unit tests as of Phase 12). Gate 2 gates phase completion — test topologies (Topo 1–7) plus namespace-specific tests (freeze/thaw, internet policy, multi-range isolation, resource limits) run on EC2.

---

## 12. Implementation Status

The platform is delivered in numbered phases. Phases 0–12 are complete (pending a teardown fix in Gate 2). The namespace-isolation pillar (Phases 8–12) rewrote the platform from host-level bridges to per-range kernel namespaces.

| Phase | Scope | Status |
|---|---|---|
| 0 | EC2 environment bootstrap (qemu-kvm, libvirt, cloud images) | Done |
| 1 | Backend interface + LibvirtBackend (VM lifecycle, qcow2 overlays, SQLite schema) | Done |
| 2 | Networking (bridges, TAPs, per-topology mgmt bridge, static IPs) | Done |
| 3 | State machine + DAG dependency resolver + wave deploy + readiness probes | Done |
| 4 | Image registry + ImageBuilder (boot-and-snapshot) | Done |
| 5 | Dependency & config injection (packages, install, configure, service, DependencySet, Paramiko SSH) | Done |
| 6 | SDK API surface (Topology, Node, Link, Range, LiveNode, YAML export/import) | Done |
| 7 | Docker container nodes — mixed VM + container topologies | Done |
| 8 | Namespace isolation — supervisor + netns (libvirtd-per-namespace, veth pair, host route, FORWARD rule) | Done |
| 9 | Namespace isolation — cgroups (resource limits, freeze/thaw) | Done |
| 10 | Namespace isolation — backend rewrite (virsh via per-range socket, clean bridge names) | Done |
| 11 | Namespace isolation — engine integration (supervisor in deploy/destroy lifecycle) | Done |
| 12 | SDK surface for namespace features + internet policy + full Topo 1–7 regression | Done (teardown fix pending) |
| 13 | Windows support (UEFI, virtio, cloudbase-init, WinRM) | Planned |

### 12.1 Module Map

```
rangectl/
├── backend.py            # Backend protocol
├── libvirt_backend.py    # libvirt + qcow2 + cloud-init seed ISOs
├── container_backend.py  # Docker container nodes (veth into range netns)
├── supervisor.py         # unshare + bind-mounts + libvirtd in ns
├── netns.py              # mgmt bridge, veth pair, host route, FORWARD rule
├── cgroup.py             # cgroup v2 create/destroy, limits, freeze/thaw
├── internet.py           # per-range iptables chain (RANGE-<name>), MASQUERADE
├── networking.py         # bridge/TAP helpers, IP allocation
├── images.py             # ImageRegistry, ImageBuilder
├── dependencies.py       # DependencySet, layered dep model
├── readiness.py          # port_open, ping, process_running, command_succeeds
├── cloudinit.py          # cloud-init seed ISO generation
├── engine.py             # state machine, DAG, wave deploy
├── state.py              # SQLite schema and ops
├── topology.py           # Topology, Node, Link, Range, LiveNode
├── types.py              # ExecResult, InjectMethod, OSType
└── __init__.py           # public exports
```

---

## 13. Comparison with GNS3

| Problem | GNS3 Approach | rangectl |
|---|---|---|
| Node lifecycle | Fire REST call, assume success | State machine with health checks; transitions on verified preconditions |
| Dependencies | Sleep timers, manual ordering | DAG with topological sort, parallel waves |
| Link management | Create optimistically; break if endpoint not ready | Deferred — wired only when both endpoints READY |
| Process cleanup | Track PIDs, hope nothing leaks | PID namespace — kill libvirtd, kernel cleans up everything |
| Resource isolation | None — one bad VM kills the host | Cgroups — per-range memory, CPU, PID limits |
| Network isolation | Cloud nodes, manual bridge config | Network namespaces — structural L2 isolation, auto-provisioned mgmt |
| Bridge naming | Global namespace, IFNAMSIZ hashing | Per-netns scoping, clean names |
| Image management | Full disk copy per node | qcow2 CoW overlays — millisecond provisioning, shared base images |
| Pause/resume | Not supported | Cgroup freezer — atomic pause, zero CPU while frozen |
| Internet control | Manual NAT rules | Per-range iptables chain — `none` or `full`, runtime toggleable |
| SDK | REST API + web UI | Python SDK — declarative topology, context managers, YAML config |
| Snapshot | Per-node, inconsistent timing | Freeze → snapshot all → thaw — atomic consistency across entire range |
| VM management | N/A | Libvirt per namespace — virsh scoped per range, debugging tooling preserved |
| Range isolation | N/A | Four kernel primitives (netns + pidns + mntns + cgroup), empirically validated |

---

## 14. Glossary

| Term | Meaning |
|---|---|
| **Range** | A self-contained testbed instance: a set of VMs in a topology with isolated namespaces, cgroup, and lifecycle. |
| **Topology** | The declarative graph of nodes and links a range realizes. |
| **Node** | A VM (or container) in a topology. |
| **Link** | A logical L2 connection between two node interfaces, realized as a bridge with TAP/veth members. |
| **Wave** | A set of nodes that can be deployed in parallel because they share the same depth in the dependency DAG. |
| **L1 / L2 / L3 readiness** | Hypervisor running / OS up & SSH reachable / user-defined service check. |
| **Mgmt network** | Per-range management bridge + veth + host route used for engine-driven SSH, never for user topology traffic. |
| **Inject method** | How configuration (SSH key, network config) is delivered to a VM: `pre-baked`, `cloud-init`, `cloudbase-init`, or `guest-agent`. |
| **Overlay** | A qcow2 file storing the per-VM delta against an immutable golden base image. |
| **Supervisor** | The process that `unshare`s into the range's namespaces, bind-mounts libvirt state paths, and `exec`s libvirtd as PID 1. |
| **Freeze / thaw** | Cgroup-freezer-based pause/resume of every process in a range. |
| **Internet policy** | Per-range outbound rule: `none` (isolated) or `full` (MASQUERADE through host). |

---

*End of document.*
