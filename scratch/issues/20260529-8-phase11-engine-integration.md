# Phase 11: Engine Integration — Namespace-Aware Deploy/Destroy
**Created**: 2026-05-29
**Status**: Complete

## Related Issues
- **Plan**: `20260527-1-vm-testbed-platform-design.md` — Phase 11 detail
- **Phase 8-10**: `20260529-7-phase8-10-namespace-isolation-gate1.md` — modules built
- **Architecture**: `agents/network-architecture.md` — v2 design
- **Orchestrator**: `20260527-5-rangectl-orchestrator.md`

## Goal
Wire the engine to use supervisor + netns + cgroup for range provisioning. Engine.deploy() creates namespaces before deploying nodes, Engine.destroy() tears down namespaces after cleanup. This is the phase where everything comes together — unit tests AND integration tests (Gate 2).

## What Changes in `rangectl/engine.py` (~20% rewrite)

### deploy() flow becomes:
```
1. supervisor.create_range(name, mgmt_subnet)  → RangeInfo
   - Creates PID+net+mount namespaces
   - Starts libvirtd inside them
   - Wires veth mgmt network to host
2. Create LibvirtBackend(libvirt_socket=range_info.libvirt_socket, netns_name=range_info.netns_name)
3. Deploy nodes using ns-aware backend (existing DAG/wave logic)
4. All bridge operations land inside the netns automatically
```

### destroy() flow becomes:
```
1. Destroy nodes via ns-aware backend (existing logic)
2. supervisor.destroy_range(name) → kills PID ns, cleans up everything
```

### Key integration points:

**Engine constructor change:**
```python
class Engine:
    def __init__(self, backend: Backend, db: StateDB,
                 container_backend: Backend | None = None,
                 use_namespaces: bool = False,
                 resources: Resources | None = None):
```
When `use_namespaces=True`, engine creates supervisor/netns infrastructure and passes socket/netns to backend.

**Per-range backend creation:**
The engine must create a NEW LibvirtBackend per range with the range's libvirt_socket and netns_name. The backend passed to the constructor is used as a template (ssh_user, ssh_ready_timeout) or for legacy mode.

**Mgmt subnet allocation:**
Unchanged — engine already allocates /24 subnets. Just routed via veth instead of host bridge.

**Bridge naming:**
Use `ns_bridge_name(index)` and `ns_mgmt_bridge_name()` from networking.py when in namespace mode. Clean names: `mgmt-br`, `data-0`, `data-1`.

**Container nodes in namespace mode:**
ContainerBackend already uses veth pairs. In namespace mode, containers need their veth wired into the range's netns instead of the host namespace. The engine should pass the netns_name to container wiring.

**Cgroup integration (optional for this phase):**
If resources are provided, create cgroup before supervisor.create_range() and write supervisor PID into it. Can defer full cgroup testing to Phase 12.

## What Carries Over Unchanged
- DAG resolution, wave deploy, dependency injection
- State machine transitions
- Cloud-init seed ISO generation
- SSH plumbing (goes via veth, path is transparent)
- VyOS serial console bootstrap
- Snapshot/restore (virsh commands just use per-range socket)

## Modules Available (built in Phase 8-10)

### supervisor.py
- `create_range(name, mgmt_subnet, range_dir="/ranges") -> RangeInfo`
- `destroy_range(name, range_dir="/ranges") -> None`
- `RangeInfo(name, pid, netns_name, libvirt_socket, mgmt_subnet, veth_host, veth_ns)`
- netns_name format: `rangectl-{name}`

### netns.py
- `create_mgmt_network(netns_name, mgmt_subnet, range_name) -> MgmtNetwork`
- `destroy_mgmt_network(mgmt) -> None`
- `create_data_bridge(netns_name, bridge_name) -> None`
- `exec_in_netns(netns_name, cmd) -> CompletedProcess`
- `MgmtNetwork(bridge_name="mgmt-br", veth_host, veth_ns, host_ip, subnet)`

### cgroup.py
- `create_cgroup(range_name, resources) -> str` (cgroup path)
- `destroy_cgroup(range_name) -> None`
- `freeze(range_name)` / `thaw(range_name)`
- `write_pid(cgroup_path, pid)`
- `Resources(memory, cpus, pids, cpuset)`

### libvirt_backend.py
- `LibvirtBackend(ssh_user, ssh_ready_timeout, libvirt_socket=None, netns_name=None)`
- When socket/netns set: virsh uses `-c qemu+unix:///system?socket=<sock>`, bridge ops use `ip netns exec`
- When None: legacy host-level behavior (backward compat)

### networking.py
- `ns_bridge_name(index)` → `"data-{index}"`
- `ns_mgmt_bridge_name()` → `"mgmt-br"`

## Unit Tests (Gate 1)

### `tests/unit/test_engine.py` updates
- Engine with `use_namespaces=True` calls `supervisor.create_range` during deploy
- Engine creates per-range LibvirtBackend with socket/netns params
- Engine uses clean bridge names in namespace mode
- Engine calls `supervisor.destroy_range` during destroy
- Backward compat: `use_namespaces=False` behaves exactly as before
- All existing 186 unit tests still pass

## Integration Tests (Gate 2)

**EC2 instance is RUNNING. Do NOT run `ec2.sh stop`.**

### Test strategy: 4 representative tests

Push code to EC2: `scratch/scripts/ec2.sh push . /home/ubuntu/rangectl`
Run tests: `scratch/scripts/ec2.sh ssh "cd rangectl && sudo pytest tests/integration -x -v"`

#### Test 1: 2-node (Topo 1 pattern)
- Deploy 2 Ubuntu VMs in namespace-isolated range
- Verify: virsh via per-range socket lists 2 VMs
- Verify: SSH from host via veth mgmt path works
- Verify: VMs can ping each other
- Destroy: clean, no orphan processes/bridges

#### Test 2: VyOS routed (Topo 2 pattern)
- Deploy VyOS router + 2 Ubuntu VMs
- Verify: VyOS serial console bootstrap works through per-range libvirtd
- Verify: cross-subnet routing via VyOS works
- Verify: SSH to all VMs via veth mgmt

#### Test 3: Mixed VM+container (Topo 7 pattern)
- Deploy nginx container + Ubuntu VM on shared bridge inside namespace
- Verify: ping between container and VM
- Verify: curl nginx from VM
- Verify: docker exec works

#### Test 4: Multi-range (2 simultaneous ranges)
- Deploy 2 separate ranges with different mgmt subnets
- Verify: each has its own netns, libvirtd, libvirt socket
- Verify: no L2 cross-range leakage (structural isolation via netns)
- Destroy range A: range B unaffected
- Destroy range B: clean

### Important: Clean up between tests
Each test must destroy all ranges, VMs, bridges after completion. Do NOT leave state between tests.

## Success Criteria
- [x] Engine.deploy() creates namespace infrastructure before deploying nodes
- [x] Engine.destroy() tears down namespace infrastructure after cleanup
- [x] Per-range LibvirtBackend with socket/netns params
- [x] Clean bridge names in namespace mode
- [x] All existing unit tests still pass (197 = 186 + 11 new)
- [x] Gate 2: 2-node test passes on EC2
- [x] Gate 2: VyOS routed test passes on EC2
- [x] Gate 2: Mixed VM+container test passes on EC2
- [x] Gate 2: Multi-range isolation test passes on EC2

## Resolution

### What was built
- `Engine.__init__` gained `use_namespaces` + `resources` params. When
  `use_namespaces=True`, deploy() calls `_setup_namespace()` (cgroup →
  `supervisor.create_range` → per-range LibvirtBackend bound to the range's
  socket+netns → `cgroup.write_pid`) before any VM/bridge op, and destroy()
  calls `_teardown_namespace()` (`supervisor.destroy_range` + cgroup cleanup).
- Backend/bridge-name resolution is namespace-aware via `_vm_backend()`,
  `_backend_for(topology_name, node)`, `_mgmt_bridge_name()`,
  `_link_bridge_name()`. Clean names (`mgmt-br`, `data-N`) in ns mode; legacy
  hashed names otherwise. The supervisor owns mgmt-br + host gateway IP in ns
  mode, so the engine skips creating them.
- `ContainerBackend` gained an optional `netns_name`: host-side veth is moved
  into the range netns and enslaved to the bridge there (per-range container
  wiring). Engine builds a per-range ContainerBackend in ns mode.
- Backward compat: `use_namespaces=False` is the default and untouched path.

### Root-cause bug found + fixed (Test 4)
`bridges.name TEXT NOT NULL UNIQUE` was globally unique — fine for legacy
hashed names, but ns-mode bridge names (`data-0`) repeat across ranges. The
second range's `INSERT INTO bridges` hit `UNIQUE constraint failed`. Fixed by
making it `UNIQUE(topology_name, name)` (matches the `nodes` table; strictly
looser, no legacy regression). Also hardened the integration cleanup fixture to
sweep orphaned `mgh*/mgp*` host veths left by a half-built range.

### Files created
- `tests/unit/test_engine_ns.py` — 10 ns-mode engine unit tests
- `tests/integration/test_ns_integration.py` — 4 Gate 2 tests

### Files modified
- `rangectl/engine.py` — namespace-aware deploy/destroy (the bulk)
- `rangectl/container_backend.py` — optional `netns_name` veth wiring
- `rangectl/state.py` — `bridges` UNIQUE(topology_name, name)
- `tests/unit/test_container.py` — +1 container netns wiring test

### Gate output
Gate 1 (local + EC2): `197 passed` (186 existing + 11 new).

Gate 2 (EC2, `sudo .venv-ec2/bin/python -m pytest tests/integration/test_ns_integration.py`):
- `test_ns_two_node` — PASSED (218s)
- `test_ns_vyos_routed` — PASSED (328s)
- `test_ns_mixed_vm_container` — PASSED (133s)
- `test_ns_multi_range_isolation` — PASSED (435s)

Host left clean after each run (no leftover netns / veths / domains).
