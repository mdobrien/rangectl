# Phase 3: State Machine + Deploy/Destroy Engine (Gate 1 Only)
**Created**: 2026-05-27
**Status**: Complete (Gate 1)
**Phase**: 3

## Related Issues
- **Plan**: `20260527-1-vm-testbed-platform-design.md` — Phase 3
- **Orchestrator**: `20260527-5-rangectl-orchestrator.md`
- **Phase 1-2**: `20260527-7-phase1-2-backend-networking.md` — foundation this builds on

## Goal
Implement the deploy/destroy engine flow, node state machine transitions, readiness probe checking, deferred link wiring, and structured logging — all testable with MockBackend. Gate 2 deferred (EC2 blocked).

## What Exists (from Phase 1-2, commit f1b4904)
- `Engine.validate_resources(topo)` — implemented, raises ResourceError
- `Engine.compute_waves(topo)` → list[list[Node]] — implemented, Kahn toposort
- `Engine.deploy()` — stub, calls validate/allocate/compute but raises NotImplementedError
- `Engine._deploy_wave()` — stub, spawns threads but _deploy_node raises NotImplementedError
- `Engine._deploy_node()`, `_wire_link()`, `_inject_dependencies()`, `destroy()` — all stubs
- `MockBackend` — full implementation in tests/unit/conftest.py (records calls, canned responses)
- `StateDB` — all CRUD methods implemented (topology, node, logs, images, subnets)
- `networking.py` — allocate_mgmt_ip, mgmt_host_ip, bridge_name, mgmt_bridge_name
- `NodeState` enum: DEFINED → PROVISIONING → READY → LINKED → RUNNING → DESTROYING → DESTROYED → FAILED

## Scope — What to Implement

### 1. Node state machine (rangectl/types.py or engine.py)
Define valid transitions:
- DEFINED → PROVISIONING (deploy starts)
- PROVISIONING → READY (readiness probe passes)
- READY → LINKED (links wired)
- LINKED → RUNNING (dependency injection complete)
- RUNNING → DESTROYING (destroy starts)
- DESTROYING → DESTROYED (cleanup complete)
- Any → FAILED (on error)

Add a helper: `transition_node_state(current: NodeState, target: NodeState)` that raises on invalid transitions.

### 2. Engine.deploy() — full implementation (engine.py)
Remove the NotImplementedError. The flow:
1. `validate_resources(topology)` — already works
2. `allocate_mgmt_subnet` + `save_topology` to DB
3. `create_bridge(mgmt_bridge_name)`
4. `compute_waves(topology)` — already works
5. For each wave, call `_deploy_wave()` (already threads nodes)
6. Wire topology links via `_wire_link()`
7. Inject dependencies via `_inject_dependencies()` (skip for Phase 3 — just mark as RUNNING)
8. Build and return `Range` with `LiveNode` handles

### 3. Engine._deploy_node() — full implementation
1. Set state DEFINED → PROVISIONING (save to DB)
2. Resolve image path from DB (just lookup, don't copy yet)
3. Create COW overlay: `backend.create_overlay(image_path, overlay_path)`
4. Build VMSpec from Node
5. Create VM: `backend.create_vm(spec)` — save vm_id to DB
6. Start VM: `backend.start(vm_id)`
7. Assign mgmt IP: `networking.allocate_mgmt_ip(subnet, node_index)`
8. Attach mgmt interface: `backend.attach_interface(vm_id, mgmt_bridge, mac)`
9. Run readiness check (mock for now — just transition state)
10. Set state PROVISIONING → READY

### 4. Engine._wire_link()
1. Create bridge: `backend.create_bridge(bridge_name(topo, link_index))`
2. Save bridge to DB
3. Attach node_a interface: `backend.attach_interface(vm_a, bridge, mac_a)`
4. Attach node_b interface: `backend.attach_interface(vm_b, bridge, mac_b)`
5. Set both nodes READY → LINKED

### 5. Engine._inject_dependencies() — Phase 3 stub
For Phase 3, just transition LINKED → RUNNING. Full dependency injection comes in Phase 4-5.

### 6. Engine.destroy() — full implementation
1. For each node: set RUNNING → DESTROYING, call `backend.stop(vm_id)`, call `backend.destroy(vm_id)`, set → DESTROYED
2. Delete topology bridges: `backend.delete_bridge()`
3. Delete mgmt bridge: `backend.delete_bridge()`
4. Free mgmt subnet: `db.free_mgmt_subnet()`
5. Delete topology from DB: `db.delete_topology()`

### 7. Structured logging (state.py)
`Engine` should call `db.log_event()` for key state transitions, deploy steps, and errors. Already implemented in StateDB — just wire it up in engine methods.

### 8. Range construction
`Engine.deploy()` must build `Range` with populated `_nodes` dict containing `LiveNode` instances (name, mgmt_ip, topology_name).

## Unit Tests to Write

### tests/unit/test_state_machine.py
- `test_valid_transitions` — all valid pairs succeed
- `test_invalid_transition` — e.g., DEFINED → RUNNING raises
- `test_any_to_failed` — any state can go to FAILED

### tests/unit/test_deploy.py (engine deploy/destroy with MockBackend)
- `test_deploy_single_node` — creates overlay, VM, starts, returns Range with LiveNode
- `test_deploy_records_backend_calls` — verify MockBackend.calls contains create_overlay, create_vm, start, create_bridge (mgmt), attach_interface in order
- `test_deploy_saves_topology_to_db` — db.get_topology returns the topology after deploy
- `test_deploy_saves_nodes_to_db` — db has node records with correct state
- `test_deploy_two_nodes_with_link` — both nodes deployed, link bridge created, interfaces attached
- `test_deploy_wave_ordering` — node with depends_on deploys after dependency (check call order)
- `test_deploy_assigns_mgmt_ips` — LiveNode.mgmt_ip is set correctly
- `test_destroy_cleans_up` — destroy calls stop, destroy on all VMs, deletes bridges, frees subnet
- `test_destroy_removes_from_db` — topology no longer in db after destroy
- `test_deploy_insufficient_resources` — raises ResourceError, nothing created
- `test_range_context_manager` — `with` block calls destroy on exit

### tests/unit/test_readiness.py
- `test_readiness_probe_factory_functions` — port_open, ping, process_running, command_succeeds return correct ReadinessProbe

## Important Notes
- Use MockBackend for all deploy/destroy tests — no real VMs
- Use StateDB(":memory:") for all DB operations
- Node indexes for mgmt IP allocation: use order within topology._nodes
- MAC addresses can be generated deterministically (e.g., from node name hash)
- _inject_dependencies is a stub in Phase 3 — just transitions state
- All 31 existing tests must still pass (regression)

## Success Criteria
- [x] State machine transitions implemented and tested
- [x] Engine.deploy() fully works with MockBackend
- [x] Engine.destroy() fully works with MockBackend
- [x] Range with LiveNodes returned from deploy
- [x] Link wiring creates bridges and attaches interfaces
- [x] Wave ordering verified in tests
- [x] Structured logging via db.log_event()
- [x] All unit tests pass (new + existing 31)
- [x] Zero failures, zero skips
- [x] Committed to git

## Gate 1 Output
```
============================== 64 passed in 0.14s ==============================
```

Breakdown:
- 31 pre-existing tests (test_dag, test_engine.validate_resources, test_networking, test_state, test_types) — no regressions
- 11 new test_deploy.py — single/two-node deploy, DB persistence, wave ordering, mgmt IPs, destroy, ResourceError path, Range context manager
- 17 new test_state_machine.py — valid transitions (parametrized), invalid (skip/backwards/from-terminal), all non-terminal → FAILED, terminal-state outgoing set check
- 5 new test_readiness.py — port_open / ping / process_running / command_succeeds factories

## Gate 2
Deferred — EC2 bare-metal blocked on AWS vCPU quota increase.

## Resolution
Implemented:
- `rangectl/types.py`: `VALID_TRANSITIONS`, `InvalidTransitionError`, `transition_node_state()`
- `rangectl/engine.py`: full `deploy()` / `destroy()` / `_deploy_node()` / `_wire_link()` / `_inject_dependencies()` (stub flips LINKED→RUNNING)
- `rangectl/state.py`: `check_same_thread=False` + RLock to make StateDB safe for wave-parallel deploys
- Per-deploy engine bookkeeping (`_vm_ids`, `_mgmt_ips`, `_link_bridges`) keyed by `(topology, node)` so destroy() can find what to tear down
- Deterministic MAC generation from `sha1(topo/node/suffix)` with `52:54:00:` OUI
- Overlay path scheme: `~/.rangectl/overlays/{topo}/{node}.qcow2`

Notes / design decisions:
- Single-node topologies with no links never enter LINKED via `_wire_link`, so `_inject_dependencies` bridges READY→LINKED→RUNNING. When links exist, `_wire_link` already moves both endpoints to LINKED, then injection finishes the move to RUNNING.
- `_deploy_wave` re-raises the first worker exception after joining all threads, so insufficient-resource failures propagate cleanly.
- Logged events at: deploy start (mgmt subnet/bridge), per-node provisioning + ready, per-node running, destroy start, deploy complete.
