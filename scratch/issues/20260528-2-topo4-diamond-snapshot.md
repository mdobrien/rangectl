# Gate 2: Topo 4 — Diamond DAG + Snapshot/Restore
**Created**: 2026-05-28
**Status**: Complete

## Related Issues
- **Parent**: `20260527-5-rangectl-orchestrator.md`
- **Spec**: `20260527-4-testing-strategy.md` (Topo 4 definition)
- **Prior**: `20260527-12-topo2-topo3-integration.md` (Topo 2+3, patterns)

## Goal
Write and pass integration test for Topo 4: diamond DAG (4 Ubuntu nodes) with snapshot/restore verification on real VMs.

## Topology
```
              ┌── web ──┐
router ──────┤          ├── monitor
              └── db  ──┘
```
- router: no dependencies (wave 1)
- web + db: depend on router (wave 2)
- monitor: depends on web + db (wave 3)
- 3 topology links, 4 mgmt IPs

## What Already Exists
- `Range.snapshot(name)` / `Range.restore(name)` — implemented, unit-tested
- `LiveNode.snapshot(name)` / `LiveNode.restore(name)` — implemented, DB-tracked
- `backend.snapshot(vm_id, name)` → `virsh snapshot-create-as`
- `backend.restore(vm_id, snapshot_id)` → `virsh snapshot-revert`
- Diamond DAG resolution — unit-tested in test_dag.py
- Integration test patterns established in test_topo2.py / test_topo3.py

## Implementation Steps
1. Read test_topo2.py and test_topo3.py for patterns
2. Write `tests/integration/test_topo4.py`:
   - Build diamond topology (router → web+db → monitor)
   - Deploy via context manager
   - Verify all 4 nodes get mgmt IPs and are SSH-reachable
   - Verify deploy order: router first, then web+db, then monitor
   - Create a marker file on monitor: `echo "before" > /tmp/marker`
   - `rng.snapshot("baseline")`
   - Modify marker: `echo "after" > /tmp/marker`
   - Verify marker reads "after"
   - `rng.restore("baseline")`
   - Verify marker reads "before" (restore worked)
3. Run Gate 1: `pytest tests/unit` — no regressions (117/117)
4. Push to EC2, run Gate 2: all topos pass

## Success Criteria
- [x] test_topo4.py written and passing on EC2
- [x] Diamond DAG deploys in correct wave order
- [x] Snapshot creates successfully on all 4 nodes
- [x] Restore reverts state (marker file test)
- [x] Gate 1: 117/117 unit tests pass
- [x] Gate 2: Topo 1-4 all pass
- [x] Issue updated with gate output
- [x] Code committed

## Root-Cause Fix in libvirt_backend.restore()
`virsh snapshot-revert` can leave the domain in `paused` or `shut off` depending on
how the snapshot was created. The bare `snapshot-revert` call left SSH unreachable
after restore. Fix: after revert, check `_dom_state()` and force the VM back to
running (`virsh resume` for paused, `virsh start` for shut off), then `_wait_for_ssh`
before returning so callers can immediately `exec()` without burning their own retry
budget on the network coming back.

## Resolution

### Gate 1 (local)
```
117 passed in 0.57s
```

### Gate 2 (EC2 c5.metal)
```
tests/integration/test_topo4.py::test_topo4_diamond_snapshot_restore PASSED [100%]
1 passed in 154.75s (0:02:34)

# full suite:
tests/integration/test_topo1.py::test_topo1_boots_and_pings PASSED       [ 25%]
tests/integration/test_topo2.py::test_topo2_routed_ping PASSED           [ 50%]
tests/integration/test_topo3.py::test_topo3_service_through_router PASSED [ 75%]
tests/integration/test_topo4.py::test_topo4_diamond_snapshot_restore PASSED [100%]
4 passed in 395.77s (0:06:35)
```

### Files Changed
- `tests/integration/test_topo4.py` — diamond DAG test, snapshot/restore marker
  verification (router → web + db → monitor, all Ubuntu 22.04)
- `rangectl/libvirt_backend.py` — `restore()` now post-revert force-resumes/starts the
  domain and waits for SSH
