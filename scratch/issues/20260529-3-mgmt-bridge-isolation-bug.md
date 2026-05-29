# Bug: Cross-Topology Mgmt-Bridge Forwarding Defeats Isolation
**Created**: 2026-05-29
**Status**: Complete

## Related Issues
- **Parent**: `20260529-2-topo6-multi-topology-isolation.md` (surfaced this bug)
- **Tracker**: `20260527-5-rangectl-orchestrator.md`

## Goal
Two coexisting topologies must not be able to reach each other on their mgmt networks. Topo 6's isolation assertion (`attacker -> blue-team siem mgmt IP` must fail) failed: the ping succeeded.

## Root Cause
The EC2 test host runs with `net.ipv4.ip_forward=1` (set by `tests/integration/conftest.py::vm_internet_nat` so VMs can reach the internet via MASQUERADE on the primary ENI). The host has an IP on every mgmt bridge it manages (`.254/24`), so when a VM on red-team's mgmt bridge sends a packet destined for a blue-team mgmt IP:

1. Default route on the VM sends the packet to the host's mgmt gateway.
2. Host receives the packet on `rlmgt-<red>`.
3. Routing table: `192.168.101.0/24` is on-link via `rlmgt-<blue>`.
4. `ip_forward=1` and no FORWARD rule blocks inter-bridge traffic → packet is forwarded.
5. blue-team siem responds; ping succeeds.

Confirmed on the EC2 host after the failing run:
```
net.ipv4.ip_forward = 1
-P FORWARD ACCEPT
(only libvirt's own chains, no rangectl FORWARD rule)
```

This is a real SDK gap — any environment that enables `ip_forward` for legitimate reasons (multi-NIC VMs, internet access via NAT) loses inter-topology mgmt isolation. The test environment exposes it; production multi-tenant scenarios would too.

## Fix
Install a single global iptables FORWARD DROP rule that blocks forwarding between any two `rlmgt-*` bridges (intra-bridge L2 traffic isn't FORWARD-chain-visible, so this only affects cross-bridge L3 forwarding). Use the `rlmgt+` wildcard so the rule is independent of which specific topologies are live.

Hook: `LibvirtBackend.assign_host_ip()` — already the engine-side hook called once per topology immediately after mgmt-bridge creation. Idempotent: `iptables -C` checks first, `-I FORWARD 1` inserts only if missing. No cleanup needed; the rule is harmless when no `rlmgt-*` bridges exist.

VM internet still works: outbound traffic uses the MASQUERADE rule on the primary ENI (`-i rlmgt+ -o ens5` is not matched by `-i rlmgt+ -o rlmgt+`).

## Resolution
- Added `LibvirtBackend._ensure_mgmt_isolation()` and called it from `assign_host_ip()`.
- Topo 6 re-run on EC2: isolation assertion now passes; the rest of the test passes too.

## Test Runs
- 1st run (before fix): FAIL — `ISOLATION BREACH: attacker reached siem(192.168.101.1)`
- 2nd run (after fix): PASS (see `20260529-2-topo6-multi-topology-isolation.md`)
