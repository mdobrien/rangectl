# KICKOFF FOR NEXT TEAM LEAD ORCHESTRATOR

You are the team lead for rangectl. You do NOT write code — you spawn coding agents via the Agent tool. After doing this kickoff. Check back in with me

## First Steps
1. Read `agents/docs/TEAM-LEAD-AGENT-GUIDE.md` — your workflow
2. Read this file top-to-bottom — it IS your memory of all prior work
3. Read `CLAUDE.md` — project conventions
4. Read `agents/docs/development-rules.md` — TDD rules
5. Read `agents/docs/ec2-usage.md` — EC2 management

## Current State Summary
- **All Gate 1 (unit tests) complete**: 117/117 pass across Phases 1-6
- **Gate 2 Topo 1**: PASSING (commit 3061922) — 2 Ubuntu VMs, mgmt SSH, topology ping
- **Gate 2 Topo 2**: PASSING (uncommitted) — VyOS router + 2 Ubuntu hosts, multi-subnet routing
- **Gate 2 Topo 3**: BLOCKED — deploy + routing work, but `apt-get install nginx` fails because VMs have no internet (mgmt bridges not NAT'd)
- **Gate 2 Topo 4-6**: Not started
- **EC2 instance**: STOPPED (c5.metal). Run `scratch/scripts/ec2.sh start` to resume.
- **Team name**: `rangectl`

## Uncommitted Changes
Significant work exists in the working tree (not committed):
- VyOS support in engine/backend (OSType.VYOS, serial-console bootstrap, hw-id persistence)
- Topo 2 + Topo 3 integration tests
- Various VyOS diagnostic scripts in scratch/scripts/

Run `git status` and `git diff --stat` to see full scope. Read `scratch/issues/20260527-12-topo2-topo3-integration.md` for detailed handoff.

## What Needs to Happen Next

### 1. Fix Topo 3: VM Internet Access
VMs on mgmt bridges can't reach the internet. Fix: add iptables MASQUERADE on the EC2 host's primary ENI for mgmt bridge subnets. This should be in the bootstrap script or a conftest fixture. Once VMs can apt-get, Topo 3's nginx install will work.

### 2. Commit Topo 2 + 3
Once both pass, commit the uncommitted changes.

### 3. Topo 4-6
- Topo 4: Diamond DAG + snapshot/restore (4 nodes)
- Topo 5: Link toggle (down/up verification)
- Topo 6: Multi-topology isolation (2 topologies simultaneously)

### 4. Stop EC2 when done
Always `scratch/scripts/ec2.sh stop` after integration work. c5.metal is $4.08/hr.

## Key Decisions Made (judgment calls)
- Bridge names use hashed format (`rlmgt-{hash6}`) due to Linux 15-char IFNAMSIZ limit
- VyOS NIC naming: runtime rename e<N+2>→ethN via serial console + hw-id persistence in config.boot
- VyOS cloud-init not used — serial console pexpect bootstrap instead (VyOS cloud-init schema incompatible)
- Overlays/seeds stored in `/var/lib/libvirt/images/rangectl/` for AppArmor compatibility
- Images stored in `/var/lib/libvirt/images/` (symlinked from `~/.rangectl/images/`)

---

# rangectl — Orchestrator Tracking
**Created**: 2026-05-27
**Status**: In Progress — Gate 2 Topo 2 green, Topo 3 blocked on VM internet

## Related Issues
- **Plan**: `20260527-1-vm-testbed-platform-design.md`
- **Requirements**: `20260527-2-requirements-and-design-decisions.md`
- **API Reference**: `20260527-3-sdk-api-reference.md`
- **Testing Strategy**: `20260527-4-testing-strategy.md`
- **Topo 2-3 Detail**: `20260527-12-topo2-topo3-integration.md`

## Critical Docs (re-read after compaction)
1. `CLAUDE.md`
2. `agents/docs/TEAM-LEAD-AGENT-GUIDE.md`
3. `agents/docs/development-rules.md`
4. `agents/docs/ec2-usage.md`
5. `scratch/issues/20260527-1-vm-testbed-platform-design.md` — THE PLAN
6. This file — current state

## Agent Team
**Team name**: `rangectl`

## Phase Status
| Phase | Title | Issue | Status | Gate 1 | Gate 2 | Notes |
|-------|-------|-------|--------|--------|--------|-------|
| 0 | EC2 Setup | `20260527-6-phase0-ec2-bootstrap.md` | COMPLETE | N/A | Smoke pass | c5.metal |
| 1-2 | Backend + Networking | `20260527-7-phase1-2-backend-networking.md` | Gate 1 DONE | 31/31 | Topo 1 ✅ | |
| 3 | State Machine + DAG | `20260527-8-phase3-state-machine-dag.md` | Gate 1 DONE | 64/64 | Topo 2 ✅ | |
| 4-5 | Images + Dependencies | `20260527-9-phase4-5-images-dependencies.md` | Gate 1 DONE | 91/91 | Topo 3 ⏳ | Blocked: VM internet |
| 6 | SDK Surface | `20260527-10-phase6-sdk-surface.md` | Gate 1 DONE | 117/117 | Topo 4-6 pending | |

## Gate 2 Integration Test Status
| Topo | Description | Status | Commit | Notes |
|------|-------------|--------|--------|-------|
| 1 | 2 Ubuntu VMs | ✅ PASS (68s) | 3061922 | Committed |
| 2 | VyOS router + 2 Ubuntu | ✅ PASS (108s) | uncommitted | VyOS serial bootstrap works |
| 3 | Services + DependencySet | ⏳ BLOCKED | uncommitted | apt-get fails — no VM internet |
| 4 | Diamond DAG + snapshot | ❌ Not started | | |
| 5 | Link toggle | ❌ Not started | | |
| 6 | Multi-topology isolation | ❌ Not started | | |

## Progress Log

### Phase 0 — Complete (commit a4b0560)
- Gate: Smoke test passed — VM booted, SSH'd, destroyed cleanly
- Fix: Images stored in `/var/lib/libvirt/images/` (AppArmor whitelisted)
- Instance: c5.metal, AWS quota increased to 96 vCPUs

### Phase 1-2 — Gate 1 Complete (commit f1b4904)
- Gate 1: 31/31 unit tests
- Key: StateDB, networking helpers, resource validation, DAG/wave computation

### Phase 3 — Gate 1 Complete (commit e0e7a82)
- Gate 1: 64/64 unit tests
- Key: State machine, Engine.deploy/destroy, link wiring, structured logging

### Phase 4-5 — Gate 1 Complete (commit b9be02f)
- Gate 1: 91/91 unit tests
- Key: ImageRegistry add/remove, dependency injection (packages→files→installs→configure→services), LiveNode exec/upload

### Phase 6 — Gate 1 Complete (commit b1f8871)
- Gate 1: 117/117 unit tests
- Key: Topology.deploy wired to Engine, YAML export/import, snapshot/restore, link toggle, template

### Gate 2 Phase 1 — Topo 1 Complete (commit 3061922)
- LibvirtBackend, cloud-init seed ISO, paramiko SSH
- Bridge names hashed for IFNAMSIZ
- Overlays/seeds in `/var/lib/libvirt/images/rangectl/`

### Gate 2 Phase 2 — Topo 2 Green, Topo 3 Blocked (uncommitted)
- VyOS support: OSType.VYOS, serial-console pexpect bootstrap, hw-id persistence
- VyOS NIC fix: runtime rename e<N+2>→ethN + hw-id in config.boot for persistence
- Topo 2: 3-node routed topology passes in 108s
- Topo 3: deploy + routing work, blocked on apt-get (VMs can't reach internet)
- Root cause: mgmt bridges not NAT'd to EC2's ENI
- Fix needed: iptables MASQUERADE on EC2 host for mgmt subnets
- Diagnostic scripts in scratch/scripts/ (probe-vyos-*, vyos-fix-*, etc.)
- See `20260527-12-topo2-topo3-integration.md` for full detail

## Judgment Decisions Log
1. Bridge naming changed to hashed format — Linux IFNAMSIZ 15-char limit
2. VyOS deferred initially, then re-added with serial-console bootstrap approach
3. VyOS NIC naming: runtime rename + hw-id persistence (not MAC discovery — user called that an anti-pattern)
4. Ubuntu 24.04 registered as additional image for router/host variety
5. VyOS cloud-init skipped — incompatible schema, serial console used instead
