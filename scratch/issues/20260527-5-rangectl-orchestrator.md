# KICKOFF FOR NEXT TEAM LEAD ORCHESTRATOR

You are the team lead for rangectl. You do NOT write code — you spawn coding agents via the Agent tool. After doing this kickoff, check back in with the user.

## First Steps
1. Read `agents/docs/TEAM-LEAD-AGENT-GUIDE.md` — your workflow
2. Read this file top-to-bottom — it IS your memory of all prior work
3. Read `CLAUDE.md` — project conventions
4. Read `agents/docs/development-rules.md` — TDD rules
5. Read `agents/docs/ec2-usage.md` — EC2 management
6. Read `agents/network-architecture.md` — the v2 namespace architecture (validated spike)

## Current State Summary
- **Phases 0-7 COMPLETE**: all committed and pushed to GitHub as v0.1.0
- **Gate 1**: 138/138 unit tests pass (117 original + 21 container tests)
- **Gate 2**: Topo 1-7 all passing individually (no combined full sweep since Topo 4)
- **Phase 7 (Docker containers)**: shipped — mixed VM+container topologies work
- **EC2 instance**: RUNNING. Docker installed. **LEAVE IT RUNNING through Phase 11** — c5.metal stop/start cycle takes 5-10 min and hits vCPU quota errors. Only clean up leftover VMs/bridges between runs.
- **GitHub**: `mdobrien/rangectl`, tag `v0.1.0` (pre-Phase 7), HEAD at `c678e3a` (Phase 7)
- **Team name**: `rangectl`

## EC2 CRITICAL RULE
**DO NOT stop the EC2 instance between phases.** Tell every coding agent: "Do NOT run `ec2.sh stop`. Leave EC2 running. Only clean up VMs and bridges after tests." The stop/start cycle is slow and causes vCPU quota race conditions. Stop EC2 ONLY after Phase 12 (full regression) is complete.

## AGENT SPAWNING RULE
**Always spawn coding agents as team members** (using `Agent` tool with `team_name` and `run_in_background: true`). They launch in their own terminal windows. Do NOT use subagents that return results inline — the user wants to see and interact with agents directly. Use `SendMessage` to communicate with them.

## What Needs to Happen Next

### Phases 8-12: Namespace Isolation (v2 Architecture)
The master plan (`20260527-1-vm-testbed-platform-design.md`) has full details for each phase. Architecture design in `agents/network-architecture.md`.

| Phase | What | Key Deliverable |
|-------|------|----------------|
| **8** | Supervisor + Network Namespace | `supervisor.py`, `netns.py` — libvirtd in PID+net+mount ns, veth mgmt access |
| **9** | Cgroups | `cgroup.py` — resource limits (memory/cpu/pids), freeze/thaw |
| **10** | Backend Rewrite | `libvirt_backend.py` rewrite — per-range socket, delete bridge hashing |
| **11** | Engine Integration | Engine calls supervisor, ns-aware deploy/destroy |
| **12** | SDK Surface + Internet Policy + Full Regression | `Range(internet=)`, freeze/thaw API, full Topo 1-7 regression on ns backend |

### Gate 2 Testing Strategy (Phases 8-11)
Four representative tests validate each phase incrementally. Full Topo 1-7 regression deferred to Phase 12.

| Test | What it covers |
|------|---------------|
| 2-node (Topo 1 pattern) | Basic VM lifecycle, cloud-init, mgmt SSH via veth |
| VyOS routed (Topo 2 pattern) | Multi-OS, serial console bootstrap, cross-subnet routing |
| Mixed VM+container (Topo 7 pattern) | Container veth wiring, docker exec, mixed backend dispatch |
| Multi-range (2 ranges simultaneous) | Two ranges coexist, separate netns/libvirtd/mgmt, independent destroy |

### Backlog (do later, not now)
- `scratch/issues/20260529-6-sdk-polish-audit.md` — dependency execution audit, ImageBuilder.build(), SDK doc update

## Key Decisions Made (judgment calls)
- Bridge names use hashed format (`rlmgt-{hash6}`) due to Linux 15-char IFNAMSIZ limit — **will be deleted in Phase 10** (netns scoping removes the need)
- VyOS NIC naming: runtime rename e<N+2>→ethN via serial console + hw-id persistence in config.boot
- VyOS cloud-init not used — serial console pexpect bootstrap instead (VyOS cloud-init schema incompatible)
- Overlays/seeds stored in `/var/lib/libvirt/images/rangectl/` for AppArmor compatibility
- Images stored in `/var/lib/libvirt/images/` (symlinked from `~/.rangectl/images/`)
- Container snapshot/restore: raises NotImplementedError (deferred to v2)
- Link.down()/up() on container endpoints: does not re-wire veth (acceptable for v1)
- EC2 left running between phases to avoid stop/start delays

## Root-Cause Fixes Found During Gate 2
Document these patterns — they inform the namespace rewrite:
1. `backend.restore()` — `virsh snapshot-revert` leaves VM paused/shut-off. Fix: force resume + wait_for_ssh after revert. (Topo 4)
2. `Link.up()` — `create_bridge` makes empty bridge, orphaned TAPs don't reattach. Fix: `attach_interface` re-enslaves TAPs from recorded `_endpoints`. (Topo 5)
3. Mgmt bridge isolation — `ip_forward=1` (needed for MASQUERADE) lets host route between `rlmgt+` bridges. Fix: `iptables -I FORWARD 1 -i rlmgt+ -o rlmgt+ -j DROP`. (Topo 6) — **goes away in Phase 10** when netns provides structural isolation.

---

# rangectl — Orchestrator Tracking
**Created**: 2026-05-27
**Status**: Phases 0-7 COMPLETE. Next: Phase 8 (namespace isolation).

## Related Issues
- **Plan**: `20260527-1-vm-testbed-platform-design.md` — THE PLAN (Phases 0-13)
- **Architecture**: `agents/network-architecture.md` — v2 namespace design (validated spike)
- **Requirements**: `20260527-2-requirements-and-design-decisions.md`
- **API Reference**: `20260527-3-sdk-api-reference.md`
- **Testing Strategy**: `20260527-4-testing-strategy.md`
- **Topo 2-3 Detail**: `20260527-12-topo2-topo3-integration.md`
- **Topo 4**: `20260528-2-topo4-diamond-snapshot.md`
- **Topo 5**: `20260529-1-topo5-link-toggle.md`
- **Topo 6**: `20260529-2-topo6-multi-topology-isolation.md`
- **Topo 6 bug**: `20260529-3-mgmt-bridge-isolation-bug.md`
- **Phase 7**: `20260529-4-phase7-docker-container-nodes.md`
- **SDK Polish (backlog)**: `20260529-6-sdk-polish-audit.md`

## Critical Docs (re-read after compaction)
1. `CLAUDE.md`
2. `agents/docs/TEAM-LEAD-AGENT-GUIDE.md`
3. `agents/docs/development-rules.md`
4. `agents/docs/ec2-usage.md`
5. `agents/network-architecture.md` — v2 namespace design
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
| 8 | Supervisor + Netns | | NOT STARTED | | | Next up |
| 9 | Cgroups | | NOT STARTED | | | |
| 10 | Backend Rewrite | | NOT STARTED | | | |
| 11 | Engine Integration | | NOT STARTED | | | |
| 12 | SDK + Internet + Regression | | NOT STARTED | | | Full Topo 1-7 sweep here |

## Gate 2 Integration Test Status
| Topo | Description | Status | Commit | Notes |
|------|-------------|--------|--------|-------|
| 1 | 2 Ubuntu VMs | ✅ PASS (68s) | 3061922 | |
| 2 | VyOS router + 2 Ubuntu | ✅ PASS (108s) | 100e19d | |
| 3 | Services + DependencySet | ✅ PASS (242s) | 100e19d | apt-get nginx + cross-subnet curl |
| 4 | Diamond DAG + snapshot | ✅ PASS (155s) | bfc3180 | restore() resumes paused/shut-off VMs |
| 5 | Link toggle | ✅ PASS (94s) | 87f45c0 | TAP re-enslave on Link.up() |
| 6 | Multi-topology isolation | ✅ PASS (155s) | 36c7949 | iptables FORWARD DROP between rlmgt+ |
| 7 | Mixed VM + Docker | ✅ PASS (50s) | d41dc99 | nginx container + Ubuntu VM |

## Progress Log

### Phase 0 — Complete (commit a4b0560)
- Gate: Smoke test passed — VM booted, SSH'd, destroyed cleanly
- Fix: Images stored in `/var/lib/libvirt/images/` (AppArmor whitelisted)
- Instance: c5.metal, AWS quota increased to 96 vCPUs

### Phase 1-2 — Complete (commit f1b4904)
- Gate 1: 31/31 unit tests
- Key: StateDB, networking helpers, resource validation, DAG/wave computation

### Phase 3 — Complete (commit e0e7a82)
- Gate 1: 64/64 unit tests
- Key: State machine, Engine.deploy/destroy, link wiring, structured logging

### Phase 4-5 — Complete (commit b9be02f)
- Gate 1: 91/91 unit tests
- Key: ImageRegistry add/remove, dependency injection, LiveNode exec/upload

### Phase 6 — Complete (commit b1f8871)
- Gate 1: 117/117 unit tests
- Key: Topology.deploy wired to Engine, YAML export/import, snapshot/restore, link toggle, template

### Gate 2 Phase 1 — Topo 1 Complete (commit 3061922)
- LibvirtBackend, cloud-init seed ISO, paramiko SSH
- Bridge names hashed for IFNAMSIZ
- Overlays/seeds in `/var/lib/libvirt/images/rangectl/`

### Gate 2 Phase 2 — Topo 2+3 Complete (commit 100e19d)
- VyOS support: OSType.VYOS, serial-console pexpect bootstrap, hw-id persistence
- VyOS NIC fix: runtime rename e<N+2>→ethN + hw-id in config.boot
- VM internet: conftest `vm_internet_nat` fixture (iptables MASQUERADE + DNS in cloud-init)
- Topo 3: nginx installed via apt-get, cross-subnet curl returns HTTP 200

### Gate 2 Phase 3 — Topo 4 Complete (commit bfc3180)
- Diamond DAG: router → web+db → monitor (4 Ubuntu nodes, 3 waves)
- Snapshot/restore: marker file test verifies revert
- Root-cause fix: `backend.restore()` force-resumes VM + waits for SSH after snapshot-revert

### Gate 2 Phase 4 — Topo 5 Complete (commit 87f45c0)
- Link toggle: down → ping fails, up → ping restored
- Root-cause fix: `Link.up()` re-enslaves TAPs via `attach_interface` after bridge recreation
- New method: `LibvirtBackend.attach_interface` finds TAP by MAC via `virsh domiflist`

### Gate 2 Phase 5 — Topo 6 Complete (commit 36c7949)
- Multi-topology: red-team (VyOS+2 Ubuntu) + blue-team (2 Ubuntu) simultaneously
- Root-cause fix: `_ensure_mgmt_isolation()` adds `iptables -I FORWARD 1 -i rlmgt+ -o rlmgt+ -j DROP`
- Staggered destroy verified (destroy red, blue unaffected)

### Phase 7 — Complete (commit d41dc99)
- ContainerBackend: docker CLI + veth/nsenter wiring, `--network=none --cap-add NET_ADMIN/NET_RAW`
- Node gains `container=` kwarg, Engine dispatches per-node via `_backend_for(node)`
- Topo 7: nginx container + Ubuntu VM on shared bridge, ping + curl + exec both paths
- Gate 1: 138/138 (21 new container tests)
- Container snapshot/restore: NotImplementedError (deferred)
- Link.down()/up() on container endpoints: does not re-wire veth (v1 limitation)

## Judgment Decisions Log
1. Bridge naming: hashed format for IFNAMSIZ — goes away in Phase 10 (netns scoping)
2. VyOS: serial-console bootstrap, not cloud-init (incompatible schema)
3. VyOS NIC naming: runtime rename + hw-id persistence (not MAC discovery — anti-pattern)
4. Overlays/seeds in `/var/lib/libvirt/images/rangectl/` for AppArmor
5. Images in `/var/lib/libvirt/images/` (shared, read-only)
6. Container snapshot deferred to v2
7. EC2 left running between phases (stop/start is slow + quota issues)
8. Mgmt bridge isolation via iptables FORWARD DROP — replaced by netns in Phase 10
