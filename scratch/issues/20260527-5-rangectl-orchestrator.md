# rangectl — Orchestrator Tracking
**Created**: 2026-05-27
**Status**: In Progress

## Related Issues
- **Plan**: `20260527-1-vm-testbed-platform-design.md`
- **Requirements**: `20260527-2-requirements-and-design-decisions.md`
- **API Reference**: `20260527-3-sdk-api-reference.md`
- **Testing Strategy**: `20260527-4-testing-strategy.md`

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
| Phase | Title | Issue | Task | Status | Gate 1 | Gate 2 | Notes |
|-------|-------|-------|------|--------|--------|--------|-------|
| 0 | EC2 Setup | `20260527-6-phase0-ec2-bootstrap.md` | #1 | BLOCKED | N/A | bootstrap validates | Blocked: AWS vCPU quota (16) too low for bare-metal. Increase requested 2026-05-27. Script ready. |
| 1-2 | Backend + Networking | `20260527-7-phase1-2-backend-networking.md` | #2 | Gate 1 DONE, Gate 2 deferred | 31/31 pass | Topo 1, Topo 2 | Gate 2 deferred until EC2 ready |
| 3 | State Machine + DAG | `20260527-8-phase3-state-machine-dag.md` | #3 | Gate 1 DONE, Gate 2 deferred | 64/64 pass | Topo 2, Topo 4 | Gate 2 deferred |
| 4-5 | Images + Dependencies | `20260527-9-phase4-5-images-dependencies.md` | #4 | In Progress (Gate 1 only) | unit | Topo 3 | Gate 2 deferred |
| 6 | SDK Surface | | #5 | Blocked by #4 | unit | Topo 4, Topo 5, Topo 6 | |

## Progress Log

### Phase 0 — BLOCKED (no commit yet)
- Agent: `phase0-coder` — script written, tested, fails fast on c5.4xlarge (no KVM)
- Script: `scratch/scripts/ec2-bootstrap.sh` — ready, idempotent, VyOS downloads ISO only
- Blocker: AWS vCPU quota is 16, bare-metal needs 48+. Quota increase requested 2026-05-27.
- .ec2-config updated to c5.metal
- Old c5.4xlarge instance terminated
- Side fix: chmod o+x /home/ubuntu for libvirt-qemu image access
- Side fix: fail-fast KVM check added to script
- Decision: VyOS qcow2 build deferred to Phase 1-2, ISO only for now

### Phase 1-2 — Gate 1 Complete (commit f1b4904)
- Agent: `phase1-2-coder` — shut down
- Gate 1: 31/31 pytest tests/unit (state, engine, dag, networking, types)
- Gate 2: DEFERRED — EC2 bare-metal blocked
- Files created: `rangectl/networking.py`, `tests/unit/conftest.py`, `tests/unit/test_state.py`, `tests/unit/test_engine.py`, `tests/unit/test_dag.py`, `tests/unit/test_networking.py`, `tests/unit/test_types.py`, `tests/__init__.py`, `tests/unit/__init__.py`
- Files modified: `rangectl/state.py`, `rangectl/engine.py`, `rangectl/backend.py`, `rangectl/types.py`
- Key APIs built:
  - `StateDB.allocate_mgmt_subnet(topo) → str` (sequential /24s from 192.168.100.0)
  - `StateDB.save_topology/get_topology/list_topologies/delete_topology`
  - `StateDB.save_node/update_node_state`
  - `StateDB.log_event/get_logs`
  - `StateDB.add_image/remove_image/get_image/list_images/image_exists`
  - `Engine.validate_resources(topo)` — raises ResourceError
  - `Engine.compute_waves(topo) → list[list[Node]]` — Kahn toposort, raises CycleError
  - `networking.allocate_mgmt_ip(subnet, index) → str`
  - `networking.mgmt_host_ip(subnet) → str`
  - `networking.bridge_name(topo, idx) → str`
  - `networking.mgmt_bridge_name(topo) → str`
- Note: FK removed from mgmt_subnets (subnet allocated before topology row exists)

### Phase 3 — Gate 1 Complete (commit e0e7a82)
- Agent: `phase3-coder` — shut down
- Gate 1: 64/64 pytest tests/unit (33 new: 17 state_machine, 11 deploy, 5 readiness)
- Gate 2: DEFERRED
- Files created: `tests/unit/test_state_machine.py`, `tests/unit/test_deploy.py`, `tests/unit/test_readiness.py`
- Files modified: `rangectl/engine.py`, `rangectl/types.py`, `rangectl/state.py`
- Key APIs built:
  - `transition_node_state(current, target) → NodeState` — raises InvalidTransitionError
  - `VALID_TRANSITIONS` dict — defines state machine
  - `Engine.deploy(topo) → Range` — full flow with MockBackend
  - `Engine.destroy(topo)` — full teardown
  - `Engine._deploy_node()` — overlay, VM create/start, mgmt IP, state transitions
  - `Engine._wire_link()` — bridge create, interface attach
  - `Engine._inject_dependencies()` — stub (LINKED→RUNNING)
- Notes: StateDB made thread-safe (check_same_thread=False + RLock) for parallel wave deploys
- Notes: Nodes without links go READY→LINKED→RUNNING in _inject_dependencies
- Notes: Deterministic MAC from sha1(topo/node/suffix), overlay at ~/.rangectl/overlays/{topo}/{node}.qcow2
