# Range Orchestrator Platform — Architecture Design Document (v3)

**Status:** Architecture validated. Feasibility spike passed all 5 phases on c5.metal / Ubuntu 22.04 / libvirt 8.0. See `scratch/scripts/libvirtd-ns-experiment.sh`.

### Implementation Status vs This Document

This document is the **target architecture**. Not everything is implemented yet. Key differences:

| Feature | Doc Says | Current Implementation | Phase |
|---------|----------|----------------------|-------|
| Management namespace (three-tier) | Host → mgmt ns → range ns | Host → range ns (direct) | Phase 16 |
| Host protection | Host never modified after setup | Host iptables/routes modified per range | Phase 16 |
| QEMU user | `libvirt-qemu` (unprivileged) | `root` | Phase 18 |
| `nsenter` for namespace access | Namespaces via `unshare`, use `nsenter` | Namespaces via `ip netns add`, use `ip netns exec` | — (works, different approach) |
| SDK API | Range lifecycle class, OS drivers | Topology + Engine + Backend (internal wiring exposed) | Phase 15 |
| Persistent ranges | `Range.connect()`, `Range.list()` | Ephemeral only (process-bound) | Phase 13 |
| CLI | `rangectl list/exec/virsh/...` | No CLI (SDK only) | Phase 14 |

**What IS implemented and matches this doc:** Per-range PID/net/mount namespaces, libvirtd-per-namespace, cgroup v2 resource limits + freeze/thaw, veth management network, per-range internet policy (iptables chains), clean bridge names in netns, qcow2 CoW overlays, DAG wave deploy, node state machine, health checks (L2), snapshot/restore, link toggle, mixed VM+container topologies, VyOS serial console bootstrap.

---

## 1. Overview

A VM testbed orchestration platform that deploys isolated "ranges" — sets of VMs stitched together in arbitrary network topologies — with a developer-friendly Python SDK. Designed as a replacement for tools like GNS3 that have good conceptual models but poor implementations (race conditions, fragile lifecycle management, complex APIs).

### Core Design Principles

- **Ranges are the primary abstraction.** A range is a self-contained topology of VMs with isolated networking, resource limits, and lifecycle guarantees.
- **No external dependencies beyond libvirt/QEMU.** The platform uses Linux kernel primitives directly for isolation and resource control. Libvirt is retained for VM lifecycle management.
- **SDK-first.** The Python SDK is the only public interface. It must be simple enough that a new engineer has a working topology in 20 minutes.
- **Lifecycle reliability above all.** The #1 lesson from GNS3: every operation must be deterministic. No "fire API call and hope." Nodes transition through explicit states, dependencies are resolved via DAG, and teardown is guaranteed clean.
- **Host network is untouchable.** All per-range networking operations happen inside a persistent management namespace, never on the host. An orchestrator bug cannot damage host connectivity.

---

## 2. Key Architecture Decision: Libvirt Per Namespace

**Empirically validated.** See experiment results at end of this section.

Each range gets its own libvirtd instance running inside the range's PID, network, and mount namespaces. This preserves all existing libvirt tooling while gaining full namespace isolation.

### 2.1 Why Libvirt Per Namespace

**Why not a single host-level libvirtd?** Libvirt creates TAP devices and network resources in whatever namespace it runs in. If libvirtd runs in the host namespace, it can't attach TAPs to bridges inside a range's network namespace without veth-pair-per-VM-interface plumbing. Running libvirtd inside the namespace means it creates TAPs, attaches them to bridges, and spawns QEMU all within the correct namespace.

**Why not drop libvirt entirely?** Libvirt provides VM lifecycle management (domain XML, QEMU command construction, snapshot management), and critically, `virsh console` for serial console access to VMs when SSH is broken. Replacing all of that is ~500-800 lines of Python and a QMP wrapper. Unnecessary — libvirt-per-namespace gives us full isolation without sacrificing any tooling.

**What this preserves:**

- `virsh list` — scoped per range via the range's libvirt socket
- `virsh console <vm>` — break-glass serial console access when SSH/networking is broken (validated: 55,886 bytes of real boot output captured in Phase D)
- `virsh snapshot-create` / `virsh snapshot-revert` — snapshot management
- Domain XML — declarative VM definitions

**What libvirt does NOT manage:** Bridges, TAPs, and network topology. The engine owns all networking directly. Libvirt domain XML uses `<interface type='bridge'><source bridge='mgmt-br'/></interface>` to attach to engine-managed bridges. Libvirt's own network definitions (`virsh net-define`) are never used — otherwise libvirt spawns its own dnsmasq and fights the engine's network model.

**QEMU runs as `libvirt-qemu`, not root.** With AppArmor disabled (`security_driver = "none"`), the compensating control is running QEMU as the unprivileged `libvirt-qemu` user. The stock Ubuntu image registry (`/var/lib/libvirt/images`) is already group-readable by `libvirt-qemu`. See issue 20260528-1.

### 2.2 How It's Accessed

Each libvirtd listens on a unix socket on the host filesystem. virsh connects as a client over the socket — it doesn't need to be inside the namespace.

```bash
# Wrapper so the team doesn't type socket paths:
rangectl virsh lab-1 list
rangectl virsh lab-1 console router

# Under the hood:
virsh -c qemu+unix:///system?socket=/tmp/lab-1/run-libvirt/libvirt-sock list
```

**Primary VM access is SSH over the management network**, not virsh console. virsh console is the break-glass fallback for when SSH/networking is broken.

### 2.3 Process Tree Per Range

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

**Memory overhead:** ~32MB libvirtd RSS per range (observed), plus QEMU RSS per VM.

### 2.4 Experiment Results

Feasibility spike on c5.metal / Ubuntu 22.04 / libvirt 8.0:

| Phase | Test | Result |
|---|---|---|
| A | libvirtd starts in PID+net+mount ns, virsh from host connects via per-range socket | ✅ |
| B | Define & start VM through per-ns libvirtd; QEMU is child of libvirtd in the ns | ✅ |
| D | virsh console — captured 55,886 bytes of authentic Ubuntu boot output | ✅ |
| E | Two concurrent ranges, fully isolated; teardown of range B kills only range B's VMs | ✅ |
| C | Kill libvirtd → kernel SIGKILLs every process in the ns. Clean reap. | ✅ |

---

## 3. System Architecture

```
┌─────────────────────────────────────────────────────────┐
│                      Python SDK                         │
│   (Public API — the only interface users interact with) │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│                    Range Engine                          │
│  ┌──────────────┐ ┌──────────────┐ ┌─────────────────┐  │
│  │  State       │ │  Dependency  │ │  Image           │  │
│  │  Machine     │ │  Resolver    │ │  Registry         │  │
│  │              │ │  (DAG)       │ │                   │  │
│  └──────────────┘ └──────────────┘ └─────────────────┘  │
│  ┌──────────────┐ ┌──────────────┐ ┌─────────────────┐  │
│  │  Network     │ │  Lifecycle   │ │  Firewall        │  │
│  │  Manager     │ │  Manager     │ │  Manager          │  │
│  └──────────────┘ └──────────────┘ └─────────────────┘  │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│              Linux Kernel Primitives + Libvirt           │
│  ┌────────────┐ ┌────────────┐ ┌──────────┐             │
│  │  Network   │ │  PID       │ │  Mount   │             │
│  │  Namespace │ │  Namespace │ │  Namespace│             │
│  └────────────┘ └────────────┘ └──────────┘             │
│  ┌────────────┐ ┌────────────┐ ┌──────────┐             │
│  │  cgroups   │ │  libvirtd  │ │  veth    │             │
│  │            │ │  (per-ns)  │ │  pairs   │             │
│  └────────────┘ └────────────┘ └──────────┘             │
│  ┌─────────────────────────────────────────┐             │
│  │  Management Namespace                   │             │
│  │  (persistent, isolates host networking) │             │
│  └─────────────────────────────────────────┘             │
└─────────────────────────────────────────────────────────┘
```

### 3.1 Three-Layer Model

**Layer 1 — Python SDK:** Declarative API for defining ranges, nodes, links, and policies. Users describe what they want; the engine figures out how to build it.

**Layer 2 — Range Engine:** Manages state machines, dependency resolution, networking, lifecycle, and image management. This is where orchestration logic lives.

**Layer 3 — Kernel Primitives + Libvirt:** Direct syscalls and commands to Linux kernel features for namespace and cgroup management. Libvirt for VM lifecycle management (QEMU process spawning, domain XML, snapshots). Libvirt runs inside each range's namespace.

---

## 4. Range Primitive

A range is the top-level unit of deployment. It encapsulates everything needed to run an isolated testbed.

### 4.1 What a Range Contains

```
Range "my-network-lab"
│
├── Network Namespace
│   ├── Management bridge (10.255.1.0/24)
│   ├── Data plane bridges (user-defined topologies)
│   └── Veth pair to management namespace (management access)
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
├── Cgroup (resource limits)
│   ├── memory.max — hard memory ceiling
│   ├── cpu.max — CPU time quota
│   ├── pids.max — fork bomb protection
│   └── freezer — pause/resume all processes
│
├── Network Policy (internet access control)
│   └── none | full
│
└── VM Instances
    ├── qcow2 overlays (copy-on-write on golden images)
    └── Per-VM configuration (vCPU, memory, interfaces)
```

### 4.2 Range Lifecycle States

```
DEFINED ──► PROVISIONING ──► READY ──► RUNNING ──► DESTROYING ──► DESTROYED
                                          │  ▲
                                          ▼  │
                                        FROZEN
```

- **DEFINED:** Range spec exists, nothing allocated yet.
- **PROVISIONING:** Namespaces created, cgroups allocated, qcow2 overlays created, libvirtd started, management network wired (veth pair to management namespace + route + iptables FORWARD rule established in management namespace), QEMU processes starting.
- **READY:** All VMs booted and passed health checks. Management network verified. Ansible/provisioning can begin.
- **RUNNING:** Range is active. VMs are operational.
- **FROZEN:** All processes suspended via cgroup freezer. Memory held, CPU released. Resume returns to RUNNING. See section 5.5 for caveats.
- **DESTROYING:** libvirtd killed (PID namespace cleans up all QEMU processes), overlays deleted, cgroup removed, namespaces torn down.
- **DESTROYED:** All resources released. Terminal state.

---

## 5. Kernel Primitives Used

### 5.1 Network Namespace

**Purpose:** Topology isolation. Each range gets its own network stack — interfaces, routes, bridges, and iptables rules are invisible to other ranges.

**What this fixes vs. the current design:**

- Bridge name collisions disappear. Inside a netns, names are scoped — you can use `data1` in every range without global uniqueness concerns.
- The 15-char IFNAMSIZ hashing problem goes away. No more `rlmgt-{hash}` — names are scoped to the namespace.
- iptables blast radius is bounded. Errant rules from inside a range can't touch other ranges or the host.
- VM-to-VM L2 leakage across ranges becomes structurally impossible, not just policy-impossible.

**How it works — three-tier namespace model:**

```
Host namespace (NEVER modified after initial setup)
│
│  Single veth pair (created once)
│  10.254.0.1/30
│  One route: 10.255.0.0/16 via 10.254.0.2
│  One FORWARD ACCEPT + one MASQUERADE
│  ip_forward = 1
│  (4 operations, done once, never touched again)
│
└────── veth ──────────────────────────────────────────────

Management namespace (persistent, orchestrator's playground)
│
│  10.254.0.2/30, default route via host
│
│  Per-range veth pairs, routes, iptables chains:
│  ├── veth-lab1 → 10.255.1.0/24 ──► lab-1 namespace
│  ├── veth-lab2 → 10.255.2.0/24 ──► lab-2 namespace
│  └── veth-lab3 → 10.255.3.0/24 ──► lab-3 namespace
│
│  All per-range network operations happen HERE,
│  never on the host. A bug here cannot damage host networking.
│
└──────────────────────────────────────────────────────────

Range "lab-1" namespace            Range "lab-2" namespace
┌───────────────────────────┐     ┌───────────────────────────┐
│  mgmt-br (10.255.1.0/24) │     │  mgmt-br (10.255.2.0/24) │
│   ├── tap-vm1 (mgmt)     │     │   ├── tap-vm1 (mgmt)     │
│   ├── tap-vm2 (mgmt)     │     │   └── tap-vm2 (mgmt)     │
│   └── tap-vm3 (mgmt)     │     │                           │
│                           │     │  data-br-1                │
│  data-br-1 (10.0.1.0/24) │     │   ├── tap-vm1-data       │
│   ├── tap-vm1-data        │     │   └── tap-vm2-data       │
│   └── tap-vm2-data        │     └───────────────────────────┘
│                           │
│  data-br-2 (10.0.2.0/24) │
│   ├── tap-vm2-data2       │
│   └── tap-vm3-data        │
└───────────────────────────┘
```

- Each range's management network gets its own /24 (auto-assigned or user-specified).
- A veth pair connects each range to the **management namespace** (not the host).
- The management namespace has routes to every range's management subnet.
- The host connects to the management namespace via a single veth pair, created once at provisioning.
- SSH from the host (or remotely via the host) reaches VMs by traversing: host → mgmt namespace → range namespace → VM. Extra hop is a veth pair in memory — microseconds of latency.
- Data plane bridges are internal to the range — user defines the topology and the engine creates bridges and TAP interfaces accordingly.

**No jump box required.** The host routes through the management namespace to reach all ranges.

**Host protection:** The orchestrator never modifies host networking after initial setup. If an orchestrator bug corrupts iptables rules, routes, or interfaces, it corrupts the management namespace only. Host SSH and host internet are unaffected. Recovery: kill the management namespace, recreate it from orchestrator state, reconnect ranges. Seconds, not a reboot.

### 5.2 PID Namespace

**Purpose:** Lifecycle management and process isolation.

Each range runs inside its own PID namespace with libvirtd as PID 1. All QEMU processes are children of libvirtd. This provides two guarantees:

1. **Clean teardown:** Killing libvirtd's host-PID causes the kernel to kill every process in the namespace — all QEMU instances. No orphan processes, no zombies, no leaked resources. One kill, guaranteed clean. This replaces today's N best-effort `delete_bridge`/`undefine` calls. **Empirically validated in Phase C/E of the feasibility spike.**
2. **Process isolation:** QEMU processes in one range cannot see or signal processes in another range. **Validated in Phase E — killing range B left range A completely unaffected.**

**Important:** Reap by killing libvirtd's host-PID, not the unshare wrapper process. The unshare process is in the host namespace; libvirtd is PID 1 inside the ns.

**Discovering libvirtd's host PID:** After `unshare --fork`, the supervisor records the child PID. That child's first descendant is libvirtd. Alternatively, read `/proc/<unshare-pid>/task/<unshare-pid>/children` to find it. The engine should record this PID at launch time so `destroy()` doesn't have to rediscover it.

### 5.3 Mount Namespace (Required)

**Purpose:** Libvirt state isolation between concurrent ranges.

Mount namespace is not optional — it is required for the libvirt-per-namespace architecture. Without it, multiple libvirtd instances collide on hardcoded paths like `/run/libvirt/network/driver.pid`.

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

### 5.4 Cgroups (v2)

**Purpose:** Resource control and the freeze/thaw mechanism.

Each range gets a cgroup at `/sys/fs/cgroup/<range-name>/` with the following controllers:

| Controller | File | Purpose | Example |
|---|---|---|---|
| memory | `memory.max` | Hard memory ceiling for all VMs in range | `32G` |
| cpu | `cpu.max` | CPU time quota (microseconds per period) | `800000 100000` (8 cores) |
| pids | `pids.max` | Max process count (fork bomb protection) | `500` |
| cpuset | `cpuset.cpus` | Pin to specific CPU cores (optional) | `0-7` |
| freezer | `cgroup.freeze` | Pause/resume all processes atomically | `1` (freeze) / `0` (thaw) |

**Implementation note:** The supervisor must write its own PID into the per-range cgroup before calling `unshare`, so all descendants (libvirtd + QEMU) are born into the cgroup.

### 5.5 Freeze/Thaw Caveats

Freeze/thaw is valuable but has a known limitation: **clock skew on thaw.**

When a range is frozen, vCPUs are paused. On thaw, wall time has jumped but guest monotonic time hasn't. Guests using kvm-clock (Linux default) handle the kernel-level clock adjustment. However, userspace services with time-sensitive behavior can break: TLS certificate validation, Kerberos tickets, NTP-locked services.

**Recommendation:** Freeze/thaw is best suited for short-duration pauses (atomic snapshots, brief oversubscription). For long pauses (hours+), snapshot-then-destroy and restore-from-snapshot is cleaner. The SDK supports a post-thaw hook for time resynchronization. **The hook is blocking** — the range stays in FROZEN→RUNNING transition until the hook returns, preventing races with health checks or user commands:

```python
range.on_thaw(lambda vm: vm.exec("chronyc makestep"))
```

**NTP after thaw with `internet=none`:** VMs can't reach external NTP servers. The host must run chrony as a server reachable via the veth pair on the management subnet, or accept clock drift after thaw.

**Freeze/thaw use cases (short-duration):**

- **Atomic snapshots:** Freeze range → snapshot all qcow2 overlays → thaw. All VM disk states are from the exact same moment.
- **Brief oversubscription:** Run more ranges than cores available by time-slicing via freeze/thaw.
- **Pause inactive ranges:** CPU drops to zero; memory is held. Thaw on demand.

---

## 6. Supervisor Runbook

The empirically validated recipe for launching a range. ~30 lines of shell, directly translatable to Python.

```
Step 0:  Attach to per-range cgroup (before unshare):
           echo $$ > /sys/fs/cgroup/<range-name>/cgroup.procs
         This ensures all descendants (libvirtd + QEMU) are born into
         the cgroup. Must happen before unshare, not after.

Step 1:  unshare --pid --fork --net --mount --uts \
           --propagation private --mount-proc

Step 2:  Bind-mount per-range directories:
           mount --bind /ranges/<name>/run-libvirt   /run/libvirt
           mount --bind /ranges/<name>/log-libvirt   /var/log/libvirt
           mount --bind /ranges/<name>/cache-libvirt /var/cache/libvirt
           mount --bind /ranges/<name>/etc-libvirt   /etc/libvirt
           mount --bind /ranges/<name>/lib-libvirt/qemu     /var/lib/libvirt/qemu
           mount --bind /ranges/<name>/lib-libvirt/dnsmasq  /var/lib/libvirt/dnsmasq
           mount --bind /ranges/<name>/lib-libvirt/boot     /var/lib/libvirt/boot
           mount --bind /ranges/<name>/lib-libvirt/swtpm    /var/lib/libvirt/swtpm
         DO NOT bind over /var/lib/libvirt/images (shared registry).

Step 3:  Block dbus (guard for hosts where /run/dbus may not exist):
           [[ -d /run/dbus ]] && mount --bind <empty-dir> /run/dbus

Step 4:  Per-range qemu.conf:
           security_driver = "none"
           stdio_handler = "file"
           dynamic_ownership = 0
           user = "libvirt-qemu"
           group = "libvirt-qemu"
         Rationale: with AppArmor disabled, running QEMU as the unprivileged
         libvirt-qemu user (not root) is the primary compensating control.
         The stock Ubuntu image registry is already group-readable by this user.

Step 5:  exec /usr/sbin/libvirtd \
           --config /ranges/<name>/libvirtd.conf \
           --pid-file /run/libvirt/libvirtd.pid
```

---

## 7. Networking Architecture

### 7.1 Management Namespace

A persistent network namespace that sits between the host and all range namespaces. Created once during host provisioning, never torn down. All per-range networking operations happen here — the host is never modified after initial setup.

**Initial setup (done once, by Ansible/cloud-init at host provisioning):**

```bash
# Create the management namespace
ip netns add mgmt

# Create veth pair between host and mgmt namespace
ip link add veth-mgmt-host type veth peer name veth-mgmt-ns
ip link set veth-mgmt-ns netns mgmt

# Assign addresses
ip addr add 10.254.0.1/30 dev veth-mgmt-host
ip link set veth-mgmt-host up
ip netns exec mgmt ip addr add 10.254.0.2/30 dev veth-mgmt-ns
ip netns exec mgmt ip link set veth-mgmt-ns up
ip netns exec mgmt ip link set lo up

# Host route to all management subnets (once, never changes)
ip route add 10.255.0.0/16 via 10.254.0.2

# Host forwarding + NAT (once, never changes)
echo 1 > /proc/sys/net/ipv4/ip_forward
iptables -A FORWARD -i veth-mgmt-host -j ACCEPT
iptables -A FORWARD -o veth-mgmt-host -m state \
  --state RELATED,ESTABLISHED -j ACCEPT
iptables -t nat -A POSTROUTING -s 10.255.0.0/16 \
  -o $(ip route show default | awk '{print $5}') -j MASQUERADE

# Default route inside mgmt namespace (for internet egress)
ip netns exec mgmt ip route add default via 10.254.0.1
```

After this, the host network config is locked. The orchestrator does all subsequent work inside the management namespace.

### 7.2 Per-Range Management Network

Every range gets an auto-provisioned management network, wired to the management namespace (not the host):

1. A Linux bridge (`mgmt-br`) inside the range's network namespace.
2. Every VM's first interface connects to this bridge.
3. A veth pair links the range to the management namespace.
4. A route in the management namespace points to the range's management subnet.
5. An iptables FORWARD ACCEPT rule in the management namespace for the management CIDR.
6. Management IPs are auto-assigned from the subnet.

This is transparent to the user — they define a range with `mgmt_network="10.255.1.0/24"` and everything is wired automatically.

### 7.3 Provisioning Order of Operations

**Critical:** The management network infrastructure must be established before VMs boot. Health checks and post-boot configuration (Ansible, SSH) require the management namespace to have a route into the range's management subnet. The ordering is:

```
Step 1: Create range network namespace                    ─┐
Step 2: Create mgmt bridge in range namespace              │ Infrastructure setup
Step 3: Create veth pair (mgmt ns ↔ range ns)             │ (seconds, deterministic,
Step 4: Add route in mgmt namespace to range subnet       │  no VM involvement,
Step 5: Add iptables FORWARD rule in mgmt namespace       │  host never touched)
Step 6: Start libvirtd in range namespace                  ─┘
Step 7: Start QEMU processes (VMs boot)                    ─ VM startup
Step 8: Health checks confirm VMs reachable                ─ Readiness verification
Step 9: Post-boot configuration (Ansible)                  ─ User-defined setup
```

Steps 1-6 are pure infrastructure plumbing that completes in seconds. All network operations in steps 3-5 happen inside the management namespace — the host is not modified. Step 7 starts VMs. Steps 8-9 happen after boot — health checks verify the VM is SSH-reachable on the management interface, then Ansible runs post-boot configuration. This supports workflows where VMs need post-boot commands (e.g., VyOS configuration that must be applied after the OS is up).

### 7.4 Cloud-Init: Default Route and DNS

VMs need a default route and nameservers configured inside the guest. Cloud-init handles this for images that support it. For images that don't (or where cloud-init is unreliable), post-boot Ansible configuration applies the settings. This is a per-VM concern, not a namespace concern.

### 7.5 Data Plane Network

The user defines the topology — which VMs connect to which, on which interfaces, with which subnets. The engine creates bridges and TAP interfaces inside the range's network namespace accordingly. Libvirt handles TAP creation as part of VM startup since it's running inside the namespace.

```python
topo = Topology("my-network-lab")

router = topo.node("router", image="vyos-1.4")
server = topo.node("server", image="ubuntu-22.04")
client = topo.node("client", image="ubuntu-22.04")

topo.link(router.eth1, server.eth0, network="10.0.1.0/24")
topo.link(router.eth2, client.eth0, network="10.0.2.0/24")
```

This creates two data plane bridges inside the namespace, with TAP interfaces for each VM endpoint.

**Verification note:** Libvirt creates TAPs via netlink from inside the namespace. The host's system-level AppArmor profile for libvirt must permit netlink operations from inside a netns. Expected to work but should be verified in the networking spike.

### 7.6 Internet Access Policy

Internet access is controlled via iptables/nftables rules in the **management namespace**, applied per range using dedicated chains. The veth pair between the management namespace and each range is the choke point.

| Policy | Behavior |
|---|---|
| `none` | No internet. VMs can reach each other and management namespace can reach VMs. Default. |
| `full` | Traffic from the range is forwarded through the management namespace to the host and NATed out. |

**Egress path:** Range VM → range mgmt bridge → veth to mgmt namespace → mgmt namespace routing → veth to host → host MASQUERADE → internet. The host's single MASQUERADE rule (set up once during provisioning) handles all ranges.

**Implementation:** Each range gets a dedicated iptables chain (`RANGE-<name>`) **inside the management namespace**. Tearing down a range flushes and deletes only its chain within the management namespace — the host's iptables are never touched.

```python
range = Range("my-lab", internet="full")

# Runtime control
range.enable_internet()
range.disable_internet()
```

---

## 8. Dev Access Model

Devs should not SSH to the host. Two options depending on environment:

### 8.1 Option 1: Routable Management Subnets (Preferred)

Make the management subnets (10.255.0.0/16) routable from the dev's machine. The host acts as a router, not a destination. Fully transparent — devs SSH directly to VM management IPs.

**On EC2:** Add a VPC route table entry: `10.255.0.0/16 → host instance ENI`. One route, native to AWS.

**On-prem with gateway access:** Add a static route on the network gateway: `10.255.0.0/16 via <host-ip>`.

**On-prem without gateway access:** WireGuard VPN. Dev installs WireGuard, gets a config file, connects. All management subnets routed through the tunnel. Dev types `ssh user@10.255.1.10` and it works. One-time client setup. WireGuard can optionally run inside the management namespace instead of on the host, so even VPN traffic is isolated from host networking.

**Dev access path:** Dev laptop → host → veth to mgmt namespace → mgmt namespace routing → veth to range namespace → range mgmt bridge → VM. The extra hop through the management namespace is a veth pair in memory — microseconds of latency, invisible to the user.

```bash
# From dev's laptop — all transparent:
ssh dev@10.255.1.10        # SSH to lab-1 VM
ssh -X dev@10.255.2.10     # SSH with X11 to lab-2 VM
# RDP to Windows VM: open RDP client → 10.255.1.20
```

### 8.2 Option 2: SSH ProxyJump (Fallback)

When routing isn't possible. A single `tunnel` account on the host with no shell, forwarding only.

```
# /etc/ssh/sshd_config on host
Match User tunnel
    PermitTTY no
    X11Forwarding no
    ForceCommand /bin/false
    AllowTcpForwarding yes
    PermitOpen 10.255.0.0/16:*
```

One account, created once, never changes regardless of how many ranges exist.

```bash
# SSH with X11 (end-to-end, host just passes bytes):
ssh -X -J tunnel@10.0.0.1 dev@10.255.1.10

# RDP to Windows VM (tunnel RDP port):
ssh -J tunnel@10.0.0.1 -N -L 3389:10.255.1.20:3389 dev@10.255.1.10
# Then RDP client → localhost:3389
```

X11 forwarding works because ProxyJump is a TCP tunnel — the SSH session is end-to-end between the dev's laptop and the VM. The host doesn't need X11 libraries or configuration.

---

## 9. VM Image Management

### 9.1 qcow2 Copy-on-Write Architecture

The platform uses QEMU's native qcow2 backing file mechanism for efficient image management.

```
Image Registry (immutable, read-only, at /var/lib/libvirt/images — shared across all ranges)
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

**Key properties:**

- Golden images are immutable and versioned. Once registered, they are set read-only (0o444). They are never modified.
- Per-VM overlays are created in milliseconds — just a thin file that records diffs from the base.
- 20 Ubuntu VMs share one 8 GB base image. Total disk is 8 GB + (20 × delta), not 160 GB.
- Destroying a range deletes only the overlay files. Base images are untouched.
- Overlays can be chained for layered image hierarchies (e.g., `base → customized → per-range`).

### 9.2 Registry Path Is an On-Disk Contract

The qcow2 backing file path is stored as an absolute path inside each overlay file. `qemu-img info --backing-chain` resolves paths at runtime, not bake-time. **Moving or renaming the image registry directory breaks every overlay that references it.**

The registry path must be treated as immutable infrastructure:

- Pin the registry path in config (default: `/var/lib/libvirt/images/`).
- Never move, rename, or restructure the registry.
- If a path must change, use `qemu-img rebase` to update all affected overlays.

This path is shared into every range's mount namespace (the supervisor deliberately does NOT bind-mount over `/var/lib/libvirt/images`).

### 9.3 Overlay Chains

```
ubuntu-22.04-base.qcow2              (golden, read-only, 8 GB)
  └── ubuntu-webstack.qcow2           (nginx + postgres pre-installed, read-only, ~500 MB delta)
        ├── lab-1/server.qcow2        (user 1's overlay, starts ~200 KB)
        └── lab-2/server.qcow2        (user 2's overlay, starts ~200 KB)
```

Resetting a user's environment = delete their overlay, create a fresh one. Sub-second operation.

**Constraint:** A base image cannot be modified once overlays reference it. Overlays store block offsets into the backing file. The image registry enforces this — base images are immutable and versioned.

### 9.4 Snapshot and Rollback

Snapshots use libvirt's snapshot management (preserved via the per-namespace libvirtd), combined with the cgroup freezer for consistency:

```python
range.freeze()      # Atomic — all VMs paused at same instant
range.snapshot("baseline")  # libvirt snapshot per VM
range.thaw()

# Later...
range.freeze()
range.rollback("baseline")  # Revert all VMs to that exact state
range.thaw()
```

---

## 10. Node State Machine

Every node (VM) in a range transitions through deterministic states. Transitions only happen when preconditions are verified — not assumed. This is the core fix for GNS3's race conditions.

```
DEFINED ──► PROVISIONING ──► BOOTING ──► READY ──► LINKED ──► RUNNING
                                                                  │
                                                           DESTROYING
                                                                  │
                                                             DESTROYED
```

| State | What's Happening | Exit Condition |
|---|---|---|
| DEFINED | Node spec exists in the topology graph | Engine begins provisioning |
| PROVISIONING | qcow2 overlay created, TAP interfaces allocated | Disk and network resources confirmed |
| BOOTING | QEMU process started via libvirt | Health check passes |
| READY | VM is up, management interface reachable | All dependencies also READY |
| LINKED | Data plane interfaces wired to bridges | Both endpoints of every link confirmed |
| RUNNING | Fully operational | User or engine initiates teardown |
| DESTROYING | QEMU process terminated, resources released | All resources confirmed freed |
| DESTROYED | Terminal state | — |

### 10.1 Health Checks

A node is not "ready" until a user-defined (or default) health check passes. This replaces GNS3's pattern of "API returned 200, therefore the node is up."

Health checks run from the host via the management network (SSH traverses host → management namespace → range namespace → VM). The per-range management network infrastructure (veth pair and route in the management namespace) is established during range provisioning, before any VMs boot.

```python
# Default: SSH port reachable on management interface
server = topo.node("server", image="ubuntu-22.04")

# Custom: specific service must be running
dns_server = topo.node("dns", image="ubuntu-22.04",
    ready_when=health_check(port=53, proto="udp"))

# Custom: HTTP endpoint returns 200
web = topo.node("web", image="ubuntu-22.04",
    ready_when=http_check("/health", status=200))

# Custom: arbitrary command exits 0 (via SSH)
db = topo.node("db", image="ubuntu-22.04",
    ready_when=exec_check("pg_isready"))
```

### 10.2 Dependency Resolution

Dependencies between nodes are modeled as a DAG (directed acyclic graph). The engine resolves them via topological sort and deploys in waves.

```python
router = topo.node("router", image="vyos-1.4")
dhcp = topo.node("dhcp", image="ubuntu-22.04",
    depends_on=[router])
client = topo.node("client", image="ubuntu-22.04",
    depends_on=[dhcp],
    ready_when=health_check(port=22))

# Deploy order:
# Wave 1: router (no dependencies)
# Wave 2: dhcp (waits for router READY)
# Wave 3: client (waits for dhcp READY)
```

**Links are deferred.** A link object is created in the graph immediately, but the actual bridge/TAP wiring only happens when both endpoints are in the READY state. This eliminates GNS3's race condition of wiring links to nodes that aren't booted yet.

---

## 11. Python SDK Design

### 11.1 Design Goals

- A new engineer reads the README and has a working 3-node topology in 20 minutes.
- Declarative-first with imperative escape hatches.
- No knowledge of namespaces, cgroups, veth pairs, or qcow2 required.

### 11.2 API Surface

```python
from rangectl import Range, Topology, Resources

# Define a topology
topo = Topology("my-network-lab")

# Add nodes
router = topo.node("router",
    image="vyos-1.4",
    vcpu=2,
    memory=2048)

server = topo.node("server",
    image="ubuntu-22.04",
    vcpu=2,
    memory=4096)

client = topo.node("client",
    image="ubuntu-22.04",
    memory=2048,
    depends_on=[router],
    ready_when=health_check(port=22))

# Define links
topo.link(router.eth0, server.eth0, network="10.0.1.0/24")
topo.link(router.eth1, client.eth0, network="10.0.2.0/24")

# Create and deploy the range
range = Range("my-network-lab",
    topology=topo,
    mgmt_network="10.255.1.0/24",
    resources=Resources(memory="32G", cpus=8),
    internet="full")

# Deploy — blocks until all nodes RUNNING
range.deploy()

# Interact
range["server"].exec("apt install -y nginx")
server_ip = range["server"].ip("eth0")

# Lifecycle
range.snapshot("baseline")
range.freeze()
range.thaw()
range.rollback("baseline")

# Teardown — guaranteed clean
range.destroy()
```

### 11.3 Context Manager Support

```python
with Range("ephemeral-lab", topology=topo) as range:
    range.provision()
    run_tests(range)
# Automatically destroyed on exit, even on exception
```

### 11.4 Configuration from File

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
range = Range.from_file("range.yaml")
range.deploy()
```

### 11.5 Debugging / Operational Commands

```bash
# List all ranges
rangectl list

# Inspect a range's network (enters all namespaces)
rangectl exec lab-1 -- ip link show
rangectl exec lab-1 -- brctl show

# virsh access scoped to a range
rangectl virsh lab-1 list
rangectl virsh lab-1 console router

# Lifecycle
rangectl freeze lab-1
rangectl thaw lab-1
rangectl destroy lab-1
```

**Implementation note:** `rangectl exec` uses `nsenter -t <libvirtd-host-pid> -a -- <cmd>`, NOT `ip netns exec`. The namespaces are created via `unshare`, not `ip netns add`, so there is no `/var/run/netns/<name>` symlink and `ip netns` doesn't know they exist.

---

## 12. Infrastructure Target

### 12.1 EC2 Deployment

The platform targets AWS EC2 bare metal instances with KVM support. **Metal instances are required for v1.** Nested KVM on non-metal Nitro instances is region-limited and instance-type-limited; deferred to future work.

**Recommended instance types:**

| Instance | vCPUs | RAM | On-Demand/hr | Use Case |
|---|---|---|---|---|
| c5.metal | 96 | 192 GiB | ~$4.08 | Dev/testing, cheapest metal |
| c6a.metal | 192 | 384 GiB | ~$5.50 | Most cores per dollar |
| m5.metal | 96 | 384 GiB | ~$4.61 | Memory-heavy topologies |
| m6a.metal | 192 | 768 GiB | ~$5.98 | Large ranges, best RAM density |
| i3.metal | 72 | 512 GiB | ~$4.99 | Fast boot (local NVMe for images) |

**Cost optimization:** Spot instances for metal types can reduce cost by 60-70%. Testbed workloads tolerate interruption — combine with freeze/snapshot for graceful handling.

### 12.2 Host Requirements

- **Ubuntu 22.04 LTS** (pinned for v1 — see note on modular daemons below)
- Linux kernel 5.10+ (cgroups v2, all namespace types)
- KVM enabled (`/dev/kvm` accessible)
- QEMU 6.0+
- libvirt 8.0+ monolithic mode (validated on this version)
- Python 3.10+

**Modular daemon note:** Ubuntu 24.04 / Debian 12 ship libvirt 9.0+ with a modular daemon split (`virtqemud` + `virtnetworkd` + `virtstoraged` + `virtlogd` + `virtsecretd`). The supervisor would need to start multiple daemons, and the bind-mount table would expand. Not a blocker but untested. Pinning to Ubuntu 22.04's monolithic `libvirtd` for v1; modular daemon support is a follow-up.

---

## 13. Migration Plan

### 13.1 Immediate: Unblock Topo 3

Before starting the v2 rewrite, apply the short-term fix to the current codebase:

- Add MASQUERADE rule for range subnets
- Fix cloud-init DNS + default gateway configuration

This is ~2 hours of work and unblocks Topo 3 immediately. The MASQUERADE rule deletes cleanly when the v2 architecture replaces the current networking.

### 13.2 Rewrite Scope

Code-level estimate of what changes:

| Component | Change | Estimate |
|---|---|---|
| `libvirt_backend.py` | ~50% rewrite — bridges/TAPs move into NetnsManager, libvirtd lifecycle becomes supervisor's job, `create_vm` issues `virsh -c qemu+unix://<per-range-socket>` | Major |
| `engine.py` | ~20% — `_deploy_node` calls netns-aware backend; allocation logic mostly survives | Moderate |
| New: `supervisor.py` | PID-1 process, namespace setup, libvirtd launch | New |
| New: `mgmt_namespace.py` | Persistent management namespace, per-range veth/route/iptables, host isolation | New |
| New: `netns.py` | Per-range network namespace creation, bridge setup | New |
| New: `cgroup.py` | Cgroup creation, limits, freeze/thaw | New |
| Removed | Global bridge name hashing, host-IP collision avoidance | Deleted |

Topology DAG, state machine, image registry, cloud-init builders, SSH plumbing, and tests carry over.

**Timeline:** ~2 weeks of focused work. This estimate assumes Ubuntu 22.04 with monolithic libvirtd 8.0 (what the spike validated). Modular daemon support on Ubuntu 24.04+ would add scope.

---

## 14. Known Follow-Ups

Items validated conceptually but not yet tested end-to-end:

- **Networking inside the netns:** Management namespace setup, per-range veth pairs, bridges, iptables chains. Standard Linux plumbing, no novel risk. The Topo 3 MASQUERADE + DNS work directly informs this. The management namespace adds one layer of indirection but uses the same primitives.
- **Cgroup v2 attachment:** Supervisor writes its own PID into a per-range cgroup before `unshare` so all descendants are in the cgroup. Standard.
- **AppArmor:** Punted via `security_driver = "none"`. Compensated by running QEMU as `libvirt-qemu` (not root). Acceptable for single-tenant testbed. Would need revisiting for untrusted/multi-tenant ranges. See issue 20260528-1.
- **Long-term stability:** Spike booted VMs and killed them. Did not run for hours or under load.
- **PTY serial in domain XML:** `virsh console` requires `<serial type='pty'><target port='0'/></serial>` in domain XML. Already present in current code — preserve it.

---

## 15. Future Considerations

### 15.1 Bridge Driver Abstraction

Default backend uses Linux bridges. OVS can be swapped in for advanced features:

```python
# Default: Linux bridges (simple, reliable)
range = Range("lab-1", topology=topo)

# Power user: OVS for mirroring, QoS, OpenFlow
range = Range("lab-1", topology=topo, bridge_driver="ovs")
```

OVS enables port mirroring, traffic shaping (WAN simulation), and OpenFlow-based routing without affecting the SDK interface.

### 15.2 Container Support

Add lightweight container nodes alongside VMs for workloads that don't need full hardware emulation:

```python
web = topo.node("web", container="nginx:latest")  # container
db = topo.node("db", image="ubuntu-22.04")         # full VM
topo.link(web.eth0, db.eth0, network="10.0.1.0/24")
```

Containers are moved into the range's network namespace after creation.

### 15.3 Nested KVM on Non-Metal Instances

EC2 nested KVM on Nitro-based instances (c5, m5, c6i) is region-limited and instance-type-limited. Once AWS improves coverage, this could be a cost-effective option for small ranges. Deferred from v1.

### 15.4 Libvirt Modular Daemon Support (Ubuntu 24.04+)

Ubuntu 24.04+ ships libvirt 9.0+ with a modular daemon split (`virtqemud` + `virtnetworkd` + `virtstoraged` + `virtlogd` + `virtsecretd`). The supervisor would need to start multiple daemons and the bind-mount table would expand. v1 pins to Ubuntu 22.04 monolithic `libvirtd`.

---

## 16. Summary: What Makes This Different from GNS3

| Problem | GNS3 Approach | This Platform |
|---|---|---|
| Node lifecycle | Fire REST call, assume success | State machine with health checks, transitions on verified preconditions |
| Dependencies | Sleep timers, manual ordering | DAG with topological sort, deploy in waves |
| Link management | Create optimistically, break if endpoint not ready | Deferred — wired only when both endpoints READY |
| Process cleanup | Track PIDs, hope nothing leaks | PID namespace — kill libvirtd, kernel cleans up everything |
| Resource isolation | None — one bad VM kills the host | Cgroups — per-range memory, CPU, PID limits |
| Network isolation | Cloud nodes, manual bridge config | Network namespaces — structural L2 isolation, auto-provisioned management network |
| Bridge naming | Global namespace, IFNAMSIZ hashing | Per-netns scoping, clean names |
| Image management | Full disk copy per node | qcow2 CoW overlays — millisecond provisioning, shared base images |
| Pause/resume | Not supported | Cgroup freezer — atomic pause, zero CPU while frozen |
| Internet control | Manual NAT rules | Policy flag — none or full, runtime toggleable |
| SDK complexity | REST API + web UI | Python SDK — declarative topology, context managers, YAML config |
| Snapshot | Per-node, inconsistent timing | Freeze → snapshot all → thaw — atomic consistency across entire range |
| VM management | N/A | Libvirt per namespace — virsh scoped per range, all debugging tooling preserved |
| Range isolation | N/A | Four kernel primitives (netns + pidns + mntns + cgroup), empirically validated |
| Host protection | N/A | Management namespace — orchestrator bugs cannot damage host networking |