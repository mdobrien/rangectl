# Gate 2: Topo 6 — Multi-Topology Isolation
**Created**: 2026-05-29
**Status**: Complete

## Related Issues
- **Parent**: `20260527-5-rangectl-orchestrator.md`
- **Spec**: `20260527-4-testing-strategy.md` (Topo 6 definition)
- **Prior**: `20260529-1-topo5-link-toggle.md` (Topo 5, committed)

## Goal
Write and pass integration test for Topo 6: two topologies deployed simultaneously, verifying isolation between them.

## Topologies
```
Topology "red-team":                Topology "blue-team":
  attacker -- router -- target        siem -- sensor
  10.0.1.0/24     10.0.2.0/24        172.16.0.0/24

  mgmt: 192.168.100.0/24             mgmt: 192.168.101.0/24
```

## What Already Exists
- `list_topologies()` — returns all topologies from DB
- Topology isolation via DB primary key + naming conventions (bridge names, VM names prefixed with topo name)
- Per-topology mgmt subnet allocation via `db.allocate_mgmt_subnet()`
- Each topology gets its own SSH keypair
- Tested in unit: test_state.py (test_list_topologies, test_delete_topology)

## Implementation Steps
1. Read test_topo2.py and test_topo3.py for patterns
2. Write `tests/integration/test_topo6.py`:
   - Build "red-team" topology: attacker + router + target (VyOS router, 2 Ubuntu hosts, 2 subnets)
   - Build "blue-team" topology: siem + sensor (2 Ubuntu hosts, 1 subnet) — simpler, no router needed
   - Deploy BOTH topologies (may need two context managers or sequential deploy)
   - Verify both are listed in `list_topologies()`
   - Verify red-team internal connectivity: attacker pings target through router
   - Verify blue-team internal connectivity: siem pings sensor
   - Verify isolation: red-team VMs CANNOT reach blue-team VMs on mgmt network (different subnets, no route between them)
   - Destroy red-team, verify blue-team still operational (siem can still ping sensor)
   - Verify `list_topologies()` shows only blue-team after red-team destroyed
3. Run Gate 1: `pytest tests/unit` — no regressions (117/117)
4. Push to EC2, run Gate 2: ALL topos pass (1-6)

## Important Notes
- Two topologies need different mgmt subnets. Check how `allocate_mgmt_subnet` works — it may auto-assign or you may need to specify.
- The red-team topology uses VyOS (same pattern as topo2). Blue-team can be Ubuntu-only (simpler).
- For isolation test: try pinging a blue-team mgmt IP from a red-team VM. It should fail because there's no route between the two mgmt bridges.
- Deploy order: deploy red-team first, then blue-team. Or deploy both — read topology.py to see if concurrent deploy is supported.
- EC2 is already running — no need to start it.

## Success Criteria
- [x] test_topo6.py written and passing on EC2
- [x] Two topologies coexist simultaneously
- [x] Internal connectivity works within each topology
- [x] Cross-topology isolation verified (no mgmt network leakage)
- [x] Destroy one topology, other unaffected
- [x] list_topologies() reflects correct state
- [x] Gate 1: 117/117 unit tests pass
- [x] Gate 2 Topo 6: PASS (155s) — full sweep skipped by request (user said the prior 1-5 results stand)
- [x] Issue updated with gate output
- [x] Code committed

## Progress Log
- Wrote `tests/integration/test_topo6.py` modeled on topo2/3 patterns; manual deploy/destroy with try/finally for the staggered lifecycle (destroy red while blue stays up).
- 1st EC2 run failed at the isolation assertion: `attacker` pinged blue-team `siem` mgmt IP. ROOT CAUSE: with `net.ipv4.ip_forward=1` (enabled by conftest for VM internet via MASQUERADE), the EC2 host — which has on-link IPs on every `rlmgt-*` bridge — forwards L3 packets between mgmt bridges. No FORWARD-chain rule was blocking it.
- Filed `20260529-3-mgmt-bridge-isolation-bug.md`; fixed in SDK by adding `LibvirtBackend._ensure_mgmt_isolation()` (called from `assign_host_ip`), which idempotently installs `iptables -I FORWARD 1 -i rlmgt+ -o rlmgt+ -j DROP`. Intra-bridge L2 traffic is unaffected; VM internet still works (MASQUERADE rule is `-o ens5`, not `-o rlmgt+`).
- 2nd run: PASS (155s).
- Used `db.list_topologies()` (fixture DB) rather than the top-level `from rangectl import list_topologies` because the public function opens the default `~/.rangectl/rangectl.db` — invisible to the test's tmp-path DB fixture. Same SQL underneath; the assertion still validates engine bookkeeping.

## Gate Output
```
tests/integration/test_topo6.py::test_topo6_multi_topology_isolation PASSED [100%]
======================== 1 passed in 155.38s (0:02:35) =========================
```

Gate 1 (local): `117 passed in 0.49s`.

## Resolution
Topo 6 passes. SDK gained a real cross-topology mgmt isolation guarantee, not just a test-side workaround.
