# rangectl — Requirements & Design Decisions
**Created**: 2026-05-27
**Related Issues**: `20260527-1-vm-testbed-platform-design.md` - platform design and implementation phases

## Problem Statement
GNS3 has the right mental model (nodes, links, topologies as graphs) but a brittle implementation — race conditions in node lifecycle, "API returned 200" treated as "operation complete", optimistic link creation, and a REST API that's painful to wrap. Other tools (Terraform, Vagrant) weren't designed for network/security lab topologies. We need that same intuitive model with rock-solid orchestration and a SDK a 3rd party team can pick up in an afternoon.

## Requirements

### R1: Declarative Topology Deployment
Describe the desired end state, engine figures out ordering. User declares nodes, links, IPs — engine does topo-sort, starts in waves, waits for readiness, wires links when both sides are up. Race conditions become the engine's problem, not the user's.

### R2: Imperative Interaction Post-Deploy
Pure declarative breaks down for testbed use cases. After deploying, you need to inject traffic, run exploits, break links, observe behavior. Model is: declarative for setup, imperative for interaction.

```python
with topo.deploy() as lab:
    lab["target"].exec("systemctl start vulnerable-service")
    lab.link("router", "target").down()   # simulate failure
    lab["attacker"].exec("nmap -sV 10.0.2.0/24")
    lab.link("router", "target").up()     # restore
    lab.snapshot("post-attack")
```

### R3: Named Topologies with Isolation
Multiple topologies coexist on the same host. Name prefixes all resources — VMs, bridges, volumes. Independent deploy/destroy lifecycle.

```python
pentest = Topology("pentest-lab")
blueteam = Topology("blueteam-lab")
# fully isolated, deploy independently
```

### R4: Explicit Dependencies with Readiness
Nodes declare dependencies via `depends_on`. Nothing proceeds until readiness is confirmed — not assumed. Three levels:
- L1: VM running (hypervisor) — what GNS3 does, nearly useless
- L2: OS booted, ping passes — our default
- L3: Service ready, user-defined — `ready_when=port_open(22)`

### R5: Static IPs in Topology Declaration
Interfaces and IPs declared inline. Topology is fully described before deploy — no runtime state needed for export.

```python
topo.link(router.eth0["10.0.1.1/24"], target.eth0["10.0.1.2/24"])
```

### R6: Management Network
Every topology gets an isolated mgmt bridge (`testbed-mgmt-{name}`). Every node gets a mgmt NIC (last interface, doesn't shift user's eth0/eth1). Host is on every mgmt bridge at .254. All engine operations (exec, upload, health checks) go over mgmt, never over user topology links.

Subnets are sequential /24s from a pool (192.168.100.0/24, 192.168.101.0/24, ...).

```
Host machine
  ├── testbed-mgmt-pentest (.254)
  │     ├── pentest-router (.1)
  │     └── pentest-target (.2)
  ├── testbed-mgmt-blueteam (.254)
  │     ├── blueteam-siem (.1)
  │     └── blueteam-sensor (.2)
  ├── pentest-br0 (10.0.1.0/24)   ← user links
  └── blueteam-br0 (172.16.0.0/24)
```

### R7: Image Registry and Builder
Local registry with add/list/remove. ImageBuilder bakes custom images via boot-and-snapshot. Deploy-time deps for prototyping, promote to baked image when stable.

```python
# bake a reusable image
web_server = (ImageBuilder("ubuntu-22.04")
    .packages(["nginx", "curl"])
    .file("/etc/nginx/conf.d/app.conf", src="./configs/app.conf")
    .run("systemctl enable nginx")
    .build("my-web-server:v1"))

# or prototype with deploy-time deps
target = topo.node("target", image="ubuntu-22.04")
target.apt(["nginx"])
```

### R8: Composable Dependency Model
Dependencies are layered: system packages → language packages → custom installs → configure functions → services. Reusable via `DependencySet`.

```python
web_stack = DependencySet("web-stack")
web_stack.apt(["nginx", "certbot"])
web_stack.pip(["flask", "gunicorn"])
web_stack.service("nginx", enabled=True)

monitoring = DependencySet("monitoring")
monitoring.install(name="node-exporter", src="./builds/node-exp.tar.gz",
                   install_cmd="./install.sh")
monitoring.service("node-exporter", start_cmd="/usr/local/bin/node_exporter",
                   ready_when=port_open(9100))

target = topo.node("target", image="ubuntu-22.04")
target.apply(web_stack)
target.apply(monitoring)
target.apt(["vim"])  # one-off additions still work
```

### R9: Custom Python in Configuration
`@node.configure` and `@depset.configure` decorators register callables that run after deps install but before services start. Real Python logic — conditionals, templating, cross-node references.

```python
@target.configure
def setup_app(node):
    node.exec("mkdir -p /opt/app")
    node.upload("./app/", "/opt/app/")
    db_ip = db_server.eth0.ip  # reference node object directly
    node.template("./templates/config.j2", "/opt/app/config.yaml",
                  vars={"db_host": db_ip, "env": "test"})
    if node.exec("which python3").exit_code != 0:
        node.apt(["python3"])
```

### R10: Windows Support
First-class, not bolted on. Platform-specific methods (`choco`, `powershell`), generic methods work cross-platform (`file`, `user`, `service`). Engine handles UEFI boot, virtio drivers, cloudbase-init/unattend.xml.

```python
dc = topo.node("dc01", image="win-server-2022", os="windows")
dc.choco(["wireshark", "nmap"])
dc.powershell("Install-WindowsFeature AD-Domain-Services")
dc.service("dns", enabled=True)
```

### R11: Topology Export Without Deploy
YAML export from the declared topology — no deploy needed. Schema defined later. Should also be importable.

```python
topo.export("topology.yaml")
Topology.from_yaml("topology.yaml")
```

### R12: SDK-Only for v1
No CLI. Python SDK is the only interface. Clean enough that a README + examples gets someone to a working topology in 20 minutes.

### R13: Deployable on Any Ubuntu Box
Install story: `apt install qemu-kvm libvirt-daemon-system && pip install rangectl`. No special OS, no Proxmox, no web UI.

### R14: Gated TDD Workflow
Every code change follows: write unit tests → write code → pass unit tests → write integration tests → pass integration tests. Unit tests gate commits, integration tests gate merges. All tests serve as regression tests for future changes.

### R15: Separated Test Layers
Unit tests (`tests/unit/`) run anywhere, use MockBackend and in-memory SQLite. Integration tests (`tests/integration/`) require a KVM host, test real VMs/bridges/SSH. No mixing — clear boundary between what needs infra and what doesn't.

## Design Decisions

### D1: Libvirt over Proxmox
**Decision**: Raw libvirt/QEMU
**Reason**: Proxmox requires installing a full OS — that's a dealbreaker for adoption. "pip install on any Ubuntu box" is the install story we want. We only need a narrow slice of what Proxmox provides (create, configure, snapshot, destroy). The complexity of rolling our own is manageable because we're NOT building clustering, HA, multi-tenancy, or a web UI.
**Tradeoff**: We own edge cases (UEFI boot, virtio drivers, QEMU version differences). Proxmox has already eaten those bugs.
**Mitigation**: Backend protocol interface so Proxmox can be added later.

### D2: Declarative Deploy, Imperative Interact
**Decision**: Topology definition is declarative (describe desired state, engine converges). Post-deploy interaction is imperative (exec, toggle links, snapshot).
**Reason**: Declarative eliminates race conditions by making ordering the engine's problem. But pure declarative is too rigid for testbed scenarios where you poke things mid-run.

### D3: Readiness Over Optimism
**Decision**: Nothing proceeds until readiness is verified, not assumed.
**Reason**: GNS3's core failure — "API returned 200" != "operation complete." They create links optimistically before interfaces exist, treat hypervisor "running" as "ready." Our engine uses actual probes. Default is L2 (ping passes), user can specify L3 (service checks). Links are deferred until both endpoints report ready.

### D4: Single Shared Management Bridge Per Topology (Not Global)
**Decision**: Each topology gets its own mgmt bridge. Not a single shared bridge.
**Reason**: Topology isolation. Nodes in one topology can't see mgmt traffic from another. Architecture supports adding a jump box later for multi-user access without host access.

### D5: Host as Jump Box (For Now)
**Decision**: No dedicated jump box VM. Host sits on every mgmt bridge at .254.
**Reason**: A jump box adds a real VM to every topology — boot time, memory, disk overhead — just for SSH access. The host already has connectivity. Jump box makes sense later for multi-user scenarios where you give someone topology access without host access. The mgmt bridge isolation makes adding it clean when needed.

### D6: Boot-and-Snapshot for Image Builder
**Decision**: ImageBuilder boots a base image, applies changes, shuts down, snapshots.
**Alternative considered**: libguestfs (modify qcow2 without booting) — faster but more limited.
**Reason**: Boot-and-snapshot uses the same mechanisms as deploy-time config (cloud-init, exec, apt). Same code paths, same behavior, no surprises. Slower to build but the result is a pre-baked image that deploys fast.

### D7: Config Injection Abstracted From User
**Decision**: User calls `apt()`, `file()`, `user()` etc. Engine picks the injection mechanism (cloud-init, guest agent, unattend.xml) based on OS.
**Reason**: Users shouldn't need to know cloud-init YAML schema or understand the difference between cloud-init and cloudbase-init. They express intent, engine handles mechanism.

### D8: DependencySet for Reuse
**Decision**: Composable `DependencySet` objects that can be `apply()`'d to any node.
**Reason**: This is where "3rd party picks it up easily" gets real. Teams build a library of DependencySets, share them. Someone publishes a set for their tool, others just `apply()` it. Same methods work on ImageBuilder and Node — same vocabulary, two execution strategies.

### D9: SDK-Only, No CLI for v1
**Decision**: Python SDK is the only interface.
**Reason**: CLI adds a second API surface to maintain. SDK-first means the API has to be good enough to stand alone. CLI can wrap the SDK later.

### D10: Sequential /24 Mgmt Subnets
**Decision**: Management subnets allocated as sequential /24s from a pool (192.168.100.0/24, 192.168.101.0/24, ...).
**Reason**: Simple, deterministic, no DHCP needed. Node gets .1, .2, etc by index. Host is .254.

### D11: Linux Bridges (Not OVS)
**Decision**: Linux bridges for v1.
**Reason**: Simpler, fewer dependencies, good enough for single-host. OVS adds complexity (flow tables, OpenFlow) that's not needed yet. Can migrate later if needed for advanced features (traffic shaping, mirroring).

### D12: Single SQLite DB for State
**Decision**: One `testbed.db` for all state — topologies, nodes, bridges, IP allocations, mgmt subnet assignments, state machine states.
**Reason**: Cleaner than per-topology JSON files. Atomic transactions for free — important when updating state during deploy/destroy. Single file to back up or inspect.

### D13: Cleanup on Failure
**Decision**: `deploy(cleanup_on_fail=True)` as default. Debug mode leaves nodes/bridges up for inspection.
**Reason**: Fail-fast without cleanup leaves orphaned VMs and bridges. But sometimes you need to SSH into a half-deployed topology to figure out what went wrong.

### D14: SSH via Paramiko (Not Ansible)
**Decision**: Paramiko for all node communication. No Ansible.
**Reason**: Ansible adds a large dependency and its own YAML DSL — over-engineering for what amounts to SSH commands. Paramiko gives exec, SFTP, key-based auth in native Python. Engine generates a keypair per topology, injects public key via cloud-init, uses private key for all operations. User never touches SSH.

### D15: Key/Config Injection Strategy
**Decision**: Pre-baked is the default inject method. Inject method is declared at image registration time.
**Methods (in order of preference)**:
1. `pre-baked` (default) — image already has SSH key + guest agent. Always true for `ImageBuilder` outputs. Zero deploy-time risk.
2. `cloud-init` — for Linux base images that ship with it (Ubuntu, Debian, Fedora, Kali, VyOS). Injects key + network config via seed ISO.
3. `cloudbase-init` — for Windows base images. Same concept, different format. Injects SSH key or WinRM cert.
4. `guest-agent` — explicit opt-in only. Requires QEMU guest agent installed in image. Not default because reliability depends on guest having the agent.
**Reason**: Cloud-init is not universal. Windows uses cloudbase-init, appliances (pfSense) and custom images may have neither. Pre-baked sidesteps the problem for any image you build yourself. User declares inject method once at registration, engine handles it from there.

### D16: Structured Logging in SQLite
**Decision**: All engine activity logged structured — state transitions, SSH commands, readiness probes — keyed by topology and node. Stored in SQLite. Persists after destroy. Deploy streams progress in real-time.
**Reason**: GNS3 failures are opaque. When node 3 of 5 fails, you need to trace exactly which command broke, not guess. `lab.logs()`, `lab["target"].logs()`, `lab.logs(level="error")` for post-mortems.

### D17: COW Disk Overlays (Only Strategy)
**Decision**: Every node gets a qcow2 overlay backed by a read-only base image from the registry. No full copies ever.
**Reason**: 5 Ubuntu nodes shouldn't mean 5 copies of a 4GB base image. COW overlays are fast to create, minimal disk usage, and make snapshots cheap. On destroy, delete overlays, base stays untouched.

### D18: Resource Validation Before Deploy
**Decision**: Engine sums vCPUs, memory, and disk across all nodes and validates against host capacity before starting anything.
**Reason**: Fail before wasting 10 minutes booting VMs only to OOM on node 8 of 10.

### D19: No Serial Console — Fail on SSH Failure
**Decision**: If SSH doesn't work, that's a broken state. Surface it as a clear error via logging, don't offer serial console as a workaround.
**Reason**: Serial console adds complexity for a case that means something fundamental is wrong (key injection failed, network misconfigured). Fix the root cause instead.

### D20: Wave-Based Parallel Deploy
**Decision**: Topo-sort from `depends_on` produces waves. Nodes within a wave start in parallel (one thread per node), next wave blocks until all current wave nodes pass readiness.
**Reason**: Parallelism is implicit in the DAG. No separate concurrency config needed. Threading is sufficient for single-host — no need for asyncio.

### D21: DNS — Deferred (Nice to Have)
**Decision**: No per-topology dnsmasq for now. Cross-node references use the node object directly (e.g. `target.eth0.ip` in `@configure` scripts) rather than hostname resolution.
**Reason**: dnsmasq adds a process per topology to manage and clean up. The engine already has the IP map — expose it through the SDK objects instead. Revisit if users need in-guest hostname resolution.

### D22: Gated TDD with Separated Test Layers
**Decision**: `tests/unit/` (MockBackend, in-memory SQLite, no infra) and `tests/integration/` (real libvirt, KVM host required). Unit tests gate commits, integration tests gate merges.
**Reason**: Agents need fast feedback loops to catch regressions. Unit tests run in seconds anywhere. Integration tests take minutes and need KVM — separating them means agents always run unit tests but only run integration tests on the EC2 box. All tests accumulate as regression tests.

### D23: MockBackend for Unit Testing
**Decision**: A `MockBackend` that implements the `Backend` protocol, records all calls, and returns canned responses. Paired with in-memory SQLite (`StateDB(db_path=":memory:")`).
**Reason**: The `Backend` protocol makes this clean — the entire engine (wave ordering, readiness flow, dependency injection) can be tested without libvirt. Fast, deterministic, runs anywhere.
