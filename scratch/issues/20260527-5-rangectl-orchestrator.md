# KICKOFF FOR NEXT TEAM LEAD ORCHESTRATOR

You are the team lead for rangectl. You do NOT write code — you spawn coding agents via the Agent tool. After doing this kickoff, check back in with the user.

## First Steps
1. Read `agents/docs/TEAM-LEAD-AGENT-GUIDE.md` — your workflow
2. Read this file top-to-bottom — it IS your memory of all prior work
3. Read `CLAUDE.md` — project conventions
4. Read `agents/docs/development-rules.md` — TDD rules
5. Read `agents/docs/ec2-usage.md` — EC2 management
6. Read `agents/network-architecture.md` — the v2 namespace architecture (validated spike, with implementation status table at the top)

## Current State Summary
- **Phases 0-12 COMPLETE**: all committed and pushed to GitHub as v0.3.0
- **Gate 1**: 222/222 unit tests pass
- **Gate 2**: Topo 1-7 all passing on namespace-isolated backend, plus ns-specific tests (freeze/thaw, internet policy, resource limits, multi-range isolation)
- **EC2 instance**: STOPPED. Instance ID: `i-0cd9c4f8ad3406291`. Start it when needed for Gate 2.
- **GitHub**: `mdobrien/rangectl`, tag `v0.3.0`, HEAD at `65f8a7f`
- **Team name**: `rangectl`
- **Build retrospective**: `scratch/issues/20260601-1-build-retrospective.md`

## EC2 RULES
- **Start EC2 before Gate 2 work**: `scratch/scripts/ec2.sh start`
- **AWS auth may need refresh**: `aws sso login` if ec2.sh fails with auth errors
- **Leave EC2 running during active dev cycles** — stop/start takes 5-10 min + quota issues
- **Leave EC2 running** during active dev — user stops it when done for the day
- **SSH key**: `~/.ssh/aws.pem`, user: `ubuntu`
- **Push code**: use rsync, not `ec2.sh push` (`.git` on EC2 is root-owned):
  `rsync -az --exclude='.git' --exclude='__pycache__' --exclude='.venv' -e "ssh -i ~/.ssh/aws.pem" ./rangectl ./tests ubuntu@$IP:/home/ubuntu/rangectl/`
- **Run tests**: `sudo python3 -m pytest ...` (system python3, not .venv)
- **Clean orphans between test runs**: `sudo pkill -f 'libvirtd --config /ranges' || true; sudo pkill -f qemu-system || true; sleep 2; for ns in $(ip netns list 2>/dev/null | grep rangectl | awk '{print $1}'); do sudo ip netns del $ns; done; sudo rm -rf /ranges/*`
- **Run integration tests ONE AT A TIME** — parallel runs overload the instance and leak state

## AGENT SPAWNING RULES
- **Spawn coding agents as team members** (Agent tool with `team_name: "rangectl"` and `run_in_background: true`)
- **Scope agent tasks to one context window** (~200k tokens max). If a phase is too large, split into multiple sequential agents.
- **Kickoff messages must be self-contained** — agents start with fresh context
- **Tell agents explicitly**: commit code + update issue file BEFORE stopping
- **Verify after agents complete**: check `git log`, run `pytest tests/unit`, read the issue file. Don't trust summaries.
- **Stuck agents**: spawn a fresh triage agent to read actual code/git state. Don't send more messages to the stuck agent.
- **Push back on shallow fixes**: demand root cause analysis. A symptom fix compounds into cascading failures downstream.

## What Needs to Happen Next

### Execution Plan: 4 Parallel Tracks

**Run Track A first (sequential internally). Then run Tracks B, C, D in parallel.**

The c5.metal EC2 instance (96 vCPU, 192GB RAM) can handle 3-4 agents running Gate 2 simultaneously — each range deploys in its own namespace with isolated networking.

```
Track A (sequential — each builds on the last):
  Phase 13 (Persistent Ranges)
    → Phase 15 (SDK Polish — Range class, OS drivers)
      → Phase 14 (CLI)

Then in parallel:
Track B:  Phase 16 (Management Namespace) → Phase 17 (Performance Benchmarking)
Track C:  Phase 19 (Link Properties) → Phase 20 (Hub/Switch) → Phase 21 (Pcap/Mirror)
Track D:  Phase 22 (Per-Range Services / DNS)

After all tracks complete:
  Phase 18 (Security Hardening — QEMU unprivileged, full regression)
```

### Track A: SDK & CLI (sequential — FIRST PRIORITY)
Makes rangectl usable beyond the person who built it. Must be sequential — CLI wraps the SDK, SDK needs persistence.

| Phase | Issue | What |
|-------|-------|------|
| **13** | `20260529-10-phase13-persistent-ranges.md` | `Range.connect()`, `Range.list()`, state persistence |
| **15** | `20260529-12-phase15-sdk-polish.md` | Range lifecycle class, OS drivers (Linux/VyOS/Container/Windows skeleton), `.run()`/`.put()`, hide Engine/Backend from users, refactor integration tests |
| **14** | `20260529-11-phase14-cli.md` | `rangectl list/status/exec/virsh/logs/net/freeze/destroy/...`, wraps Phase 15 SDK |

### Track B: Architecture & Performance (sequential, parallel with Track A)
Management namespace protects the host. Benchmark after the architecture is final.

| Phase | Issue | What |
|-------|-------|------|
| **16** | (in master plan) | Persistent management namespace — three-tier model, host network never modified |
| **17** | (in master plan) | Performance benchmarking — establish baselines, identify bottlenecks, fix top 2-3 |

### Track C: Network Simulation (sequential, parallel with Track A)
All additive features using native Linux primitives inside the netns.

| Phase | Issue | What |
|-------|-------|------|
| **19** | (in master plan) | Link properties — `tc netem` latency/bandwidth/loss/jitter |
| **20** | (in master plan) | Hub & Switch node types — L2 devices as bridges |
| **21** | (in master plan) | Port mirroring, SPAN, packet capture — `tcpdump`, `tc mirred` |

### Track D: Per-Range Services (parallel with Track A)

| Phase | Issue | What |
|-------|-------|------|
| **22** | (in master plan) | Extensible `RangeService` base class, DNS first (dnsmasq, `<node>.<range>` resolution) |

### After All Tracks: Security Hardening

| Phase | Issue | What |
|-------|-------|------|
| **18** | (in master plan) | QEMU as `libvirt-qemu` (unprivileged), full Topo 1-7 regression |

### Someday / Not Now
| Phase | What |
|-------|------|
| **23** | Windows support (UEFI, cloudbase-init, WinRM) |
| — | Mobility + propagation models (native tc netem, no EMANE) |
| — | EMANE integration (bridge driver replacement for RF fidelity) |
| — | OVS bridge driver |
| — | Multi-host distributed ranges |
| — | HIL (physical NIC passthrough into netns) |

## Key Design Docs for New Phases

Each phase issue has detailed specs. The orchestrator should **Explore the codebase** before creating kickoff messages — APIs may have changed from what the issues describe.

| Doc | What |
|-----|------|
| `scratch/issues/20260529-10-phase13-persistent-ranges.md` | Persistent ranges — 3 use cases, Range.connect(), implementation |
| `scratch/issues/20260529-11-phase14-cli.md` | CLI — full command reference, SSH key handling, scope boundary |
| `scratch/issues/20260529-12-phase15-sdk-polish.md` | SDK polish — Range lifecycle class, OS drivers, full SDK reference |
| `scratch/issues/20260527-1-vm-testbed-platform-design.md` | Master plan — all phases 0-23 |
| `agents/network-architecture.md` | Architecture — target design with implementation status table |
| `docs/rangectl-overview.md` | Overview doc — current features, architecture, SDK reference |
| `scratch/issues/20260601-1-build-retrospective.md` | Build retrospective — what worked, what broke, lessons learned |

## Key Decisions Made (judgment calls from Phases 0-12)
- Bridge names use hashed format (`rlmgt-{hash6}`) in legacy mode — clean names (`mgmt-br`, `data-0`) in namespace mode
- VyOS NIC naming: runtime rename e<N+2>→ethN via serial console + hw-id persistence in config.boot
- VyOS cloud-init not used — serial console pexpect bootstrap instead (VyOS cloud-init schema incompatible)
- Overlays/seeds stored in `/var/lib/libvirt/images/rangectl/` for AppArmor compatibility
- Images stored in `/var/lib/libvirt/images/` (symlinked from `~/.rangectl/images/`)
- Container snapshot/restore: raises NotImplementedError (deferred)
- Link.down()/up() on container endpoints: does not re-wire veth (v1 limitation)
- Default SSH password `rangectl` on all Ubuntu VMs (dev access). Framework uses ed25519 keypair.
- SDK redesign: Range lifecycle class (define_nodes → define_network → install_software → configure_os → verify → READY). Topology IS the lab — no separate Range return value.
- OS driver abstraction: `put()` and `exec()` required, everything else optional. Extensible via `OSType.register()`.
- CLI is day-2 operations only (wraps SDK). YAML deploy is secondary path.
- Test scenarios are loosely coupled from range definition — connect by range ID.

## Root-Cause Fixes Found During Gate 2
1. `backend.restore()` — `virsh snapshot-revert` leaves VM paused/shut-off. Fix: force resume + wait_for_ssh after revert. (Topo 4)
2. `Link.up()` — `create_bridge` makes empty bridge, orphaned TAPs don't reattach. Fix: `attach_interface` re-enslaves TAPs from recorded `_endpoints`. (Topo 5)
3. Mgmt bridge isolation — `ip_forward=1` (needed for MASQUERADE) lets host route between `rlmgt+` bridges. Fix: `iptables -I FORWARD 1 -i rlmgt+ -o rlmgt+ -j DROP`. (Topo 6) — replaced by netns structural isolation in Phase 10.
4. Cgroup placement — `unshare --fork` spawns libvirtd before engine can write PID to cgroup. Self-placement from inside namespace fails (`/sys` shadowed). Fix: `supervisor._place_in_cgroup()` polls for libvirtd child PID from host namespace. (Phase 12)
5. destroy_range — killed only unshare wrapper, not libvirtd (the child). Leaked libvirtd + QEMU processes caused cascading SSH timeouts. Fix: kill process tree via `_child_pids()`, drain cgroup before rmdir. (Phase 12)

---

# rangectl — Orchestrator Tracking
**Created**: 2026-05-27
**Status**: Phase 13+15 COMPLETE, Phase 14 IN PROGRESS.

## Related Issues
- **Plan**: `20260527-1-vm-testbed-platform-design.md` — THE PLAN (Phases 0-23)
- **Architecture**: `agents/network-architecture.md` — v3 design (with implementation status)
- **Requirements**: `20260527-2-requirements-and-design-decisions.md`
- **API Reference**: `20260527-3-sdk-api-reference.md`
- **Testing Strategy**: `20260527-4-testing-strategy.md`
- **Phase 8-10**: `20260529-7-phase8-10-namespace-isolation-gate1.md`
- **Phase 11**: `20260529-8-phase11-engine-integration.md`
- **Phase 12**: `20260529-9-phase12-sdk-internet-regression.md`
- **Phase 13**: `20260529-10-phase13-persistent-ranges.md`
- **Phase 14**: `20260529-11-phase14-cli.md`
- **Phase 15**: `20260529-12-phase15-sdk-polish.md`
- **Retrospective**: `20260601-1-build-retrospective.md`

## Critical Docs (re-read after compaction)
1. `CLAUDE.md`
2. `agents/docs/TEAM-LEAD-AGENT-GUIDE.md`
3. `agents/docs/development-rules.md`
4. `agents/docs/ec2-usage.md`
5. `agents/network-architecture.md` — v3 design + implementation status
6. `scratch/issues/20260527-1-vm-testbed-platform-design.md` — THE PLAN
7. This file — current state

## Agent Team
**Team name**: `rangectl`

## Phase Status
| Phase | Title | Issue | Status | Gate 1 | Gate 2 | Notes |
|-------|-------|-------|--------|--------|--------|-------|
| 0 | EC2 Setup | `20260527-6-phase0-ec2-bootstrap.md` | COMPLETE | N/A | Smoke pass | c5.metal |
| 1-2 | Backend + Networking | `20260527-7-phase1-2-backend-networking.md` | COMPLETE | 31/31 | Topo 1 ✅ | |
| 3 | State Machine + DAG | `20260527-8-phase3-state-machine-dag.md` | COMPLETE | 64/64 | Topo 2 ✅ | |
| 4-5 | Images + Dependencies | `20260527-9-phase4-5-images-dependencies.md` | COMPLETE | 91/91 | Topo 3 ✅ | |
| 6 | SDK Surface | `20260527-10-phase6-sdk-surface.md` | COMPLETE | 117/117 | Topo 4-6 ✅ | |
| 7 | Docker Containers | `20260529-4-phase7-docker-container-nodes.md` | COMPLETE | 138/138 | Topo 7 ✅ | |
| 8-10 | Namespace Isolation | `20260529-7-phase8-10-namespace-isolation-gate1.md` | COMPLETE | 186/186 | Gate 1 only | |
| 11 | Engine Integration | `20260529-8-phase11-engine-integration.md` | COMPLETE | 197/197 | 4/4 ns tests ✅ | |
| 12 | SDK + Internet + Regression | `20260529-9-phase12-sdk-internet-regression.md` | COMPLETE | 222/222 | 9/9 ns regression ✅ | |
| 13 | Persistent Ranges | `20260529-10-phase13-persistent-ranges.md` | COMPLETE | 243/243 | 2/2 ✅ | Track A |
| 14 | CLI | `20260529-11-phase14-cli.md` | IN PROGRESS | | | Track A (after 15) |
| 15 | SDK Polish | `20260529-12-phase15-sdk-polish.md` | COMPLETE | 260/260 | CLI-only ✅ | Track A (after 13) |
| 16 | Management Namespace | `20260603-1-phase16-management-namespace.md` | NOT STARTED | | | Track B |
| 17 | Performance Benchmarking | `20260601-2-deploy-performance-analysis.md` | COMPLETE | N/A | EC2 ✅ | Covered by perf-analyst work |
| 18 | Security Hardening | `20260603-2-phase18-security-hardening.md` | NOT STARTED | | | After all tracks |
| 19 | Link Properties | `20260603-3-phase19-link-properties.md` | NOT STARTED | | | Track C |
| 20 | Hub & Switch | `20260603-4-phase20-hub-switch.md` | NOT STARTED | | | Track C |
| 21 | Pcap & Mirror | `20260603-5-phase21-pcap-mirror.md` | NOT STARTED | | | Track C |
| 22 | Per-Range Services (DNS) | `20260603-6-phase22-per-range-services.md` | NOT STARTED | | | Track D |
| 23 | Windows | (master plan) | NOT STARTED | | | Someday |

## Progress Log

### Phases 0-7 — Complete (v0.1.0, tag `v0.1.0`)
See detailed log below. 138/138 unit tests, Topo 1-7 all passing.

### Phases 8-10 — Complete (commit `010bb37`)
- Namespace isolation modules: supervisor.py, netns.py, cgroup.py
- Backend rewrite: libvirt_socket + netns_name params, socket-aware virsh, netns-aware bridges
- Gate 1 only (186/186 unit tests), integration deferred to Phase 11

### Phase 11 — Complete (commit `593d020`)
- Engine integration: use_namespaces, _setup_namespace, _teardown_namespace
- ContainerBackend gains netns_name
- Gate 2: 4/4 ns integration tests (2-node, VyOS routed, mixed VM/container, multi-range)

### Phase 12 — Complete (commits `4d7064f`, `a6458bd`, `57fe34f`, `6df8860`)
- SDK: Range(internet=, resources=), freeze/thaw, enable/disable_internet
- internet.py: per-range iptables chains (RANGE-<name>), MASQUERADE
- Freeze cgroup placement fix: _place_in_cgroup() polls for libvirtd child from host
- destroy_range fix: kill process tree, drain cgroup before rmdir
- cleanup_on_fail in Engine.deploy
- Gate 1: 222/222, Gate 2: 9/9 ns regression tests

### Phase 13 — Complete (commit `45bbecc`)
- Agent: `phase13-coder`
- Gate 1: 243/243 (21 new in test_persistent.py)
- Gate 2: 2/2 on EC2 (persistent reconnect lifecycle + stale detection)
- New API: Range.connect(), Range.list(), Range.cleanup(), RangeNotRunning
- LibvirtBackend.reconnect_vm() — repopulate SSH bookkeeping cross-process
- StateDB: list_nodes(), save_node() now persists vm_id
- Range gains `persistent` flag — reconnected ranges don't auto-destroy on __exit__
- Follow-up: graceful VM shutdown ~84s/VM on EC2 (perf, not correctness)

### Phase 15 — Complete (commit `aa08d22`)
- Agent: `phase15-coder`
- Gate 1: 260/260 (17 new in test_sdk_polish.py)
- Gate 2: CLI-specific integration test on EC2 (full regression deferred)
- Range lifecycle class: define_nodes/define_network/install_software/configure_os/verify
- verify() required — deploy raises if not overridden
- OS drivers: LinuxDriver, VyOSDriver, ContainerDriver, WindowsDriver (skeleton)
- LiveNode: .run(), .put(), .route(), .sysctl(), .packages(), .service(), .start/.stop/.restart
- make_driver() factory + OSType.register() for extensibility
- Range.__repr__, LiveNode.__repr__
- expect_reach() verify helper

### v0.3.0 tagged at `65f8a7f` — pushed to GitHub

### Detailed Phase 0-7 Log

#### Phase 0 — Complete (commit a4b0560)
- Gate: Smoke test passed — VM booted, SSH'd, destroyed cleanly
- Fix: Images stored in `/var/lib/libvirt/images/` (AppArmor whitelisted)
- Instance: c5.metal, AWS quota increased to 96 vCPUs

#### Phase 1-2 — Complete (commit f1b4904)
- Gate 1: 31/31 unit tests
- Key: StateDB, networking helpers, resource validation, DAG/wave computation

#### Phase 3 — Complete (commit e0e7a82)
- Gate 1: 64/64 unit tests
- Key: State machine, Engine.deploy/destroy, link wiring, structured logging

#### Phase 4-5 — Complete (commit b9be02f)
- Gate 1: 91/91 unit tests
- Key: ImageRegistry add/remove, dependency injection, LiveNode exec/upload

#### Phase 6 — Complete (commit b1f8871)
- Gate 1: 117/117 unit tests
- Key: Topology.deploy wired to Engine, YAML export/import, snapshot/restore, link toggle, template

#### Gate 2 Topo 1 — Complete (commit 3061922)
- LibvirtBackend, cloud-init seed ISO, paramiko SSH
- Bridge names hashed for IFNAMSIZ
- Overlays/seeds in `/var/lib/libvirt/images/rangectl/`

#### Gate 2 Topo 2+3 — Complete (commit 100e19d)
- VyOS support: OSType.VYOS, serial-console pexpect bootstrap, hw-id persistence
- VyOS NIC fix: runtime rename e<N+2>→ethN + hw-id in config.boot
- VM internet: conftest `vm_internet_nat` fixture (iptables MASQUERADE + DNS in cloud-init)
- Topo 3: nginx installed via apt-get, cross-subnet curl returns HTTP 200

#### Gate 2 Topo 4 — Complete (commit bfc3180)
- Diamond DAG: router → web+db → monitor (4 Ubuntu nodes, 3 waves)
- Snapshot/restore: marker file test verifies revert
- Root-cause fix: `backend.restore()` force-resumes VM + waits for SSH after snapshot-revert

#### Gate 2 Topo 5 — Complete (commit 87f45c0)
- Link toggle: down → ping fails, up → ping restored
- Root-cause fix: `Link.up()` re-enslaves TAPs via `attach_interface` after bridge recreation

#### Gate 2 Topo 6 — Complete (commit 36c7949)
- Multi-topology: red-team (VyOS+2 Ubuntu) + blue-team (2 Ubuntu) simultaneously
- Root-cause fix: `_ensure_mgmt_isolation()` adds `iptables -I FORWARD 1 -i rlmgt+ -o rlmgt+ -j DROP`
- Staggered destroy verified (destroy red, blue unaffected)

#### Phase 7 — Complete (commit d41dc99)
- ContainerBackend: docker CLI + veth/nsenter wiring, `--network=none --cap-add NET_ADMIN/NET_RAW`
- Node gains `container=` kwarg, Engine dispatches per-node via `_backend_for(node)`
- Topo 7: nginx container + Ubuntu VM on shared bridge, ping + curl + exec both paths
- Gate 1: 138/138 (21 new container tests)
