# Phase 1-2: Backend Interface + Networking (Gate 1 Only)
**Created**: 2026-05-27
**Status**: Complete (Gate 1)
**Phase**: 1-2

## Related Issues
- **Plan**: `20260527-1-vm-testbed-platform-design.md` — Phase 1 + Phase 2
- **Orchestrator**: `20260527-5-rangectl-orchestrator.md`
- **Requirements**: `20260527-2-requirements-and-design-decisions.md`
- **Testing Strategy**: `20260527-4-testing-strategy.md`

## Goal
Implement the foundational backend interface, MockBackend for unit testing, StateDB operations, resource validation, and networking primitives (IP allocation, subnet math, bridge naming). Gate 2 (integration tests on EC2) deferred — EC2 bare-metal instance blocked on AWS quota increase.

## Scope — What to Implement

### 1. HostResources dataclass (backend.py)
`HostResources` is currently a plain class — convert to `@dataclass`.

### 2. MockBackend (tests/unit/conftest.py)
Implement per the testing strategy doc. Records all calls, returns canned responses. Implements the `Backend` protocol. Include as a pytest fixture.

### 3. StateDB methods (state.py)
All methods currently raise `NotImplementedError`. Implement:
- `allocate_mgmt_subnet(topology_name)` — find next available /24 from 192.168.100.0/24 pool, insert into mgmt_subnets table, return subnet string
- `free_mgmt_subnet(topology_name)` — delete from mgmt_subnets
- `save_topology(name, status, mgmt_subnet, mgmt_bridge)` — INSERT into topologies
- `get_topology(name)` — SELECT, return dict or None
- `list_topologies()` — SELECT all, return list of dicts
- `delete_topology(name)` — DELETE cascade
- `save_node(...)` — INSERT into nodes
- `update_node_state(topology_name, name, state)` — UPDATE nodes
- `log_event(topology_name, node_name, level, message)` — INSERT into logs
- `get_logs(topology_name, node_name, level)` — SELECT with filters
- `add_image(name, path, inject, os_type, size_mb, built_from)` — INSERT into images
- `remove_image(name)` — DELETE from images
- `get_image(name)` — SELECT, return dict or None
- `list_images()` — SELECT all
- `image_exists(name)` — SELECT EXISTS

### 4. Resource validation (engine.py)
Implement `validate_resources()`:
- Sum vCPU and memory across all nodes in topology
- Compare against `backend.host_resources()`
- Raise `ResourceError` (new exception in types.py) if insufficient

### 5. IP allocation and subnet math (new: rangectl/networking.py)
- `allocate_mgmt_ip(subnet: str, index: int) -> str` — given "192.168.100.0/24" and index 0, return "192.168.100.1" (index+1, host at .254)
- `mgmt_host_ip(subnet: str) -> str` — return .254 for the subnet
- `bridge_name(topology_name: str, index: int) -> str` — return `{topology_name}-br{index}`
- `mgmt_bridge_name(topology_name: str) -> str` — return `rangectl-mgmt-{topology_name}`

### 6. DAG / wave computation (engine.py)
Implement `compute_waves()`:
- Build adjacency from `node.depends_on`
- Topological sort
- Group into waves: wave N+1 contains nodes whose deps are all in waves <= N
- Raise `CycleError` (new exception in types.py) if cycle detected

## Unit Tests to Write

### tests/unit/conftest.py
- `MockBackend` fixture
- `StateDB(db_path=":memory:")` fixture
- `ExecResult` fixture

### tests/unit/test_state.py
- `test_allocate_mgmt_subnet_first` — first allocation returns "192.168.100.0/24"
- `test_allocate_mgmt_subnet_sequential` — second returns "192.168.101.0/24"
- `test_free_mgmt_subnet` — free and reallocate
- `test_save_and_get_topology` — round-trip
- `test_list_topologies` — multiple topologies
- `test_delete_topology` — deleted topology not in list
- `test_save_and_update_node` — save node, update state, verify
- `test_log_event_and_get_logs` — insert logs, query with filters
- `test_image_crud` — add, get, list, exists, remove

### tests/unit/test_engine.py
- `test_validate_resources_sufficient` — no error when resources available
- `test_validate_resources_insufficient_vcpu` — raises ResourceError
- `test_validate_resources_insufficient_memory` — raises ResourceError

### tests/unit/test_dag.py
- `test_no_dependencies` — all nodes in wave 1
- `test_linear_chain` — A→B→C = 3 waves
- `test_diamond` — A→(B,C)→D = 3 waves
- `test_parallel_no_deps` — all nodes in wave 1
- `test_cycle_detection` — raises CycleError

### tests/unit/test_networking.py
- `test_allocate_mgmt_ip` — index 0 → .1, index 1 → .2
- `test_mgmt_host_ip` — returns .254
- `test_bridge_name` — correct format
- `test_mgmt_bridge_name` — correct format

### tests/unit/test_types.py
- `test_interface_spec_getitem` — `InterfaceSpec[...]` returns new spec with IP/CIDR
- `test_exec_result` — dataclass fields

## Success Criteria
- [x] MockBackend implemented in conftest.py
- [x] All StateDB methods implemented (no NotImplementedError)
- [x] Resource validation implemented
- [x] IP allocation / subnet math implemented
- [x] DAG / wave computation implemented
- [x] All unit tests pass: `pytest tests/unit -v`
- [x] Zero test failures, zero skips
- [x] Committed to git

## Notes
- Removed FK from `mgmt_subnets.topology_name` — Engine allocates the subnet
  before saving the topology (chicken/egg with the FK).
- `StateDB(db_path=":memory:")` short-circuits the parent.mkdir / WAL pragma
  so SQLite stays in-memory for unit tests.
- HostResources promoted to `@dataclass`.
- New exceptions: `ResourceError`, `CycleError` in `rangectl.types`.

## Gate 1 Output
```
============================= test session starts ==============================
platform darwin -- Python 3.9.6, pytest-8.4.1, pluggy-1.6.0
collected 31 items

tests/unit/test_dag.py::test_no_dependencies PASSED                      [  3%]
tests/unit/test_dag.py::test_linear_chain PASSED                         [  6%]
tests/unit/test_dag.py::test_diamond PASSED                              [  9%]
tests/unit/test_dag.py::test_parallel_no_deps PASSED                     [ 12%]
tests/unit/test_dag.py::test_cycle_detection PASSED                      [ 16%]
tests/unit/test_dag.py::test_self_loop_detection PASSED                  [ 19%]
tests/unit/test_engine.py::test_validate_resources_sufficient PASSED     [ 22%]
tests/unit/test_engine.py::test_validate_resources_insufficient_vcpu PASSED [ 25%]
tests/unit/test_engine.py::test_validate_resources_insufficient_memory PASSED [ 29%]
tests/unit/test_engine.py::test_validate_resources_exactly_enough PASSED [ 32%]
tests/unit/test_networking.py::test_allocate_mgmt_ip_index_zero PASSED   [ 35%]
tests/unit/test_networking.py::test_allocate_mgmt_ip_sequential PASSED   [ 38%]
tests/unit/test_networking.py::test_allocate_mgmt_ip_different_subnet PASSED [ 41%]
tests/unit/test_networking.py::test_allocate_mgmt_ip_does_not_collide_with_host PASSED [ 45%]
tests/unit/test_networking.py::test_mgmt_host_ip PASSED                  [ 48%]
tests/unit/test_networking.py::test_bridge_name PASSED                   [ 51%]
tests/unit/test_networking.py::test_mgmt_bridge_name PASSED              [ 54%]
tests/unit/test_state.py::test_allocate_mgmt_subnet_first PASSED         [ 58%]
tests/unit/test_state.py::test_allocate_mgmt_subnet_sequential PASSED    [ 61%]
tests/unit/test_state.py::test_free_mgmt_subnet_releases_for_reuse PASSED [ 64%]
tests/unit/test_state.py::test_save_and_get_topology PASSED              [ 67%]
tests/unit/test_state.py::test_get_topology_missing PASSED               [ 70%]
tests/unit/test_state.py::test_list_topologies PASSED                    [ 74%]
tests/unit/test_state.py::test_delete_topology PASSED                    [ 77%]
tests/unit/test_state.py::test_save_and_update_node PASSED               [ 80%]
tests/unit/test_state.py::test_log_event_and_get_logs PASSED             [ 83%]
tests/unit/test_state.py::test_image_crud PASSED                         [ 87%]
tests/unit/test_types.py::test_interface_spec_getitem_parses_ip_and_cidr PASSED [ 90%]
tests/unit/test_types.py::test_interface_spec_getitem_returns_new_instance PASSED [ 93%]
tests/unit/test_types.py::test_exec_result_dataclass_fields PASSED       [ 96%]
tests/unit/test_types.py::test_host_resources_is_dataclass PASSED        [100%]

============================== 31 passed in 0.07s ==============================
```

## Gate 2
Deferred — EC2 bare-metal blocked on AWS vCPU quota increase.

## Resolution
