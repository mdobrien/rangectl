# Phase 12: SDK Surface + Internet Policy + Full Regression
**Created**: 2026-05-29
**Status**: In Progress

## Progress Log

**2026-05-29** — Implementation complete, Gate 1 green (214 unit tests local + EC2).

Created/modified:
- `rangectl/internet.py` (NEW) — per-range NAT chain `RANGE-<name>`, `enable_internet`/`disable_internet`/`detect_outbound_iface`. MASQUERADE out host uplink, FORWARD allow on veth choke point. Idempotent; teardown removes only the range's own rules.
- `rangectl/topology.py` — `Range(internet=, resources=)`; `Range.freeze/thaw` (→ cgroup), `Range.enable_internet/disable_internet` (→ internet mod, requires ns mode). `Topology.deploy(use_namespaces=, resources=, internet=)`.
- `rangectl/engine.py` — `Engine(internet=)`; `_setup_namespace` enables internet when `full`; `_teardown_namespace` disables it (before destroy_range, while veth/subnet still known); deploy() wires `_mgmt_subnet`/`_veth_host` onto the returned Range for runtime toggle.
- `rangectl/__init__.py` — export `Resources`.
- `tests/unit/test_internet.py` (NEW, 9), `test_range.py` (+5 SDK tests), `test_engine_ns.py` (+4 internet wiring tests).
- `tests/integration/test_ns_regression.py` (NEW, 9) — Topo 3-6 on ns backend + freeze/thaw + internet none/full/toggle + resource limits.

Gate 1: `214 passed` (was 197). Gate 2: running on EC2.

**Gate 2 debugging notes:**
- EC2 env was missing `pytest`/`paramiko` in system python3 (the repo `.venv` is a stale macOS binary). Installed both into system python3. First failures were purely `ModuleNotFoundError: paramiko` on SSH exec — cgroup limit assertions (memory.max=8GiB, cpu.max=400000) PASSED before that point, confirming resource limits work.
- Cleaned orphaned libvirtd/qemu zombies + stale rangectl netns left by an earlier Phase 11 crash.
- Test-logic fix: every fresh-DB test allocates the first mgmt subnet (192.168.100.0/24), which conftest's session `vm_internet_nat` MASQUERADEs unconditionally. That blanket rule masks the per-range policy, so the 3 internet tests now wrap in `_without_blanket_nat()` (remove + restore) — leaving ONLY the `RANGE-<name>` chain as the NAT path, properly exercising the feature.

## Related Issues
- **Plan**: `20260527-1-vm-testbed-platform-design.md` — Phase 12 detail
- **Phase 8-10**: `20260529-7-phase8-10-namespace-isolation-gate1.md`
- **Phase 11**: `20260529-8-phase11-engine-integration.md`
- **Architecture**: `agents/network-architecture.md`
- **Orchestrator**: `20260527-5-rangectl-orchestrator.md`

## Goal
Three deliverables:
1. **SDK surface**: Expose namespace features through the Range class — `Range(internet=, resources=)`, `range.freeze()`, `range.thaw()`
2. **Internet policy**: Per-range iptables chains for internet access control (`none` / `full`)
3. **Full regression**: All Topo 1-7 passing on the namespace-isolated backend

## 1. SDK Surface Changes

### `rangectl/topology.py` — Range and Topology updates

`Topology.deploy()` must pass `use_namespaces=True` and `resources` to the Engine when namespace mode is requested.

Add to Range class (or wherever the deployed range is represented):
```python
range.freeze()            # calls cgroup.freeze(range_name)
range.thaw()              # calls cgroup.thaw(range_name)
range.enable_internet()   # adds MASQUERADE + FORWARD rules for range's veth
range.disable_internet()  # removes those rules
```

The Range constructor should accept:
```python
Range("my-lab",
    topology=topo,
    mgmt_network="10.255.1.0/24",
    resources=Resources(memory="32G", cpus=8),
    internet="full")
```

### Key: `internet` parameter
- `internet="none"` (default): VMs can reach each other and host. No outbound internet.
- `internet="full"`: MASQUERADE all range traffic out through host's internet connection.
- Per-range iptables chain: `RANGE-<name>`. Teardown flushes only its chain.
- Runtime toggle via `range.enable_internet()` / `range.disable_internet()`

### Implementation of internet policy

The veth pair is the choke point. All traffic to/from a range flows through it.

For `internet="full"`:
```bash
# Create per-range chain
iptables -N RANGE-<name>
iptables -A RANGE-<name> -o <host-outbound-iface> -j MASQUERADE -t nat
iptables -A FORWARD -i <veth_host> -j ACCEPT
iptables -A FORWARD -o <veth_host> -m state --state RELATED,ESTABLISHED -j ACCEPT
```

For teardown:
```bash
iptables -D FORWARD -i <veth_host> -j ACCEPT
iptables -D FORWARD -o <veth_host> -m state --state RELATED,ESTABLISHED -j ACCEPT
iptables -t nat -D POSTROUTING -s <mgmt_subnet> -j MASQUERADE
iptables -F RANGE-<name>
iptables -X RANGE-<name>
```

VMs also need DNS + default route configured. Cloud-init already handles this for Ubuntu (gateway at .254, DNS from cloud-init metadata). VyOS gets it via serial console config.

## 2. Full Regression (Topo 1-7 on namespace backend)

All existing test topologies must pass on the new namespace-isolated architecture. This validates that the v2 rewrite is a drop-in replacement.

### What to test

Adapt existing integration tests (or write new ones) to run through the namespace-isolated engine. Each test should:
1. Create topology with `use_namespaces=True`
2. Deploy
3. Run assertions (same as original tests)
4. Destroy
5. Verify clean teardown (no orphan processes, bridges, namespaces)

| Topo | Description | Key assertions |
|------|-------------|---------------|
| 1 | 2 Ubuntu VMs | SSH, ping, clean destroy |
| 2 | VyOS router + 2 Ubuntu | Serial console bootstrap, cross-subnet routing |
| 3 | Services + DependencySet | apt-get nginx (needs internet=full), cross-subnet curl |
| 4 | Diamond DAG + snapshot | 4-node deploy in waves, snapshot/restore marker file |
| 5 | Link toggle | Link.down() → ping fails, Link.up() → ping restored |
| 6 | Multi-topology isolation | 2 topologies simultaneously, no cross-range leakage, staggered destroy |
| 7 | Mixed VM + container | nginx container + Ubuntu VM, ping + curl + exec |

**Note**: Topo 1, 2, 7 were already tested in Phase 11's `test_ns_integration.py`. You can either extend those tests or verify they cover the same ground. Focus effort on Topo 3-6 which haven't been tested on the ns backend yet.

### Plus new namespace-specific tests:
- **Freeze/thaw**: Deploy range, freeze → verify CPU drops (VMs unresponsive to ping), thaw → VMs resume
- **Internet policy**: `internet="full"` allows `apt-get update` from VM, `internet="none"` blocks it
- **Resource limits**: Deploy with `Resources(memory="8G")`, verify cgroup exists with correct limits

## 3. Available APIs (from Phases 8-11)

### Engine (modified in Phase 11)
```python
Engine(backend, db, container_backend=None, use_namespaces=False, resources=None)
engine.deploy(topology, cleanup_on_fail=True) -> Range
engine.destroy(topology)
```
- `_setup_namespace(topo_name, mgmt_subnet)` — creates cgroup + supervisor + per-range backends
- `_teardown_namespace(topo_name)` — destroys supervisor + cgroup

### supervisor.py
- `create_range(name, mgmt_subnet, range_dir) -> RangeInfo`
- `destroy_range(name, range_dir)`

### cgroup.py
- `create_cgroup(range_name, resources) -> str`
- `destroy_cgroup(range_name)`
- `freeze(range_name)` / `thaw(range_name)`
- `Resources(memory, cpus, pids, cpuset)`

### netns.py
- `create_mgmt_network(netns_name, mgmt_subnet, range_name) -> MgmtNetwork`
- Internet rules go here or in a new module

### LibvirtBackend
- `LibvirtBackend(ssh_user, ssh_ready_timeout, libvirt_socket=None, netns_name=None)`

### ContainerBackend
- `ContainerBackend(netns_name=None)`

## Unit Tests (Gate 1)

- Range constructor with `internet`, `resources` params
- `range.freeze()` / `range.thaw()` call cgroup module
- `range.enable_internet()` / `range.disable_internet()` call iptables
- Internet policy iptables chain creation/teardown
- All 197 existing tests still pass

## Integration Tests (Gate 2)

**EC2 is RUNNING. Do NOT run `ec2.sh stop`.**

```bash
scratch/scripts/ec2.sh push . /home/ubuntu/rangectl
scratch/scripts/ec2.sh ssh "cd rangectl && sudo pytest tests/integration -x -v"
```

Run ALL integration tests — both existing `test_ns_integration.py` and new regression tests.

## Success Criteria
- [ ] SDK: Range accepts `internet=`, `resources=` params
- [ ] SDK: `range.freeze()` / `range.thaw()` work
- [ ] SDK: `range.enable_internet()` / `range.disable_internet()` work
- [ ] Internet policy: per-range iptables chains, MASQUERADE for `internet="full"`
- [ ] Regression: Topo 1-7 all pass on namespace backend
- [ ] New: Freeze/thaw test passes
- [ ] New: Internet policy test passes
- [ ] All unit tests pass (197 existing + new)
- [ ] All integration tests pass on EC2
