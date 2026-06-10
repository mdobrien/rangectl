# Bug: VyOS bootstrap skips address-less NICs (no rename / hw-id pin)
**Created**: 2026-06-09
**Status**: Not Started

## Related Issues
- **Found during**: `20260609-4-phase25-vlan-support.md` — Phase 25 router-on-a-stick lab (links back here)
- **Tracking**: `20260527-5-rangectl-orchestrator.md` — listed in backlog (links back here)
- **Background**: VyOS NIC naming decision in `20260527-5` "Key Decisions" — runtime rename e<N+2>→ethN via serial console + hw-id persistence

## Goal
Make address-less VyOS NICs first-class: `link(router.eth1, sw.port2.trunk(10, 20))`
(bare trunk parent — the natural way to declare a trunk; addresses live on the vifs)
should yield a guest device named `eth1`, pinned via hw-id, with no IP.

## Problem
`LibvirtBackend._bootstrap_vyos_via_console` builds its rename + configure command
lists with `if not eth or not ip: continue` (libvirt_backend.py ~line 262). For a NIC
declared without an IP:
1. No kernel rename — the device stays `e<i+2>` (VyOS initramfs udev name), which the
   VyOS CLI rejects (`set interfaces ethernet e3 ...` → "Invalid Ethernet interface name").
   Users cannot configure vifs on it by the documented name.
2. No `hw-id` pin — even a manual rename would not survive a reboot.

Host-side VLAN behavior (filtering/tagging/isolation) is unaffected — this is purely
guest interface naming, VyOS only. Ubuntu guests don't care.

**Workaround in use**: Phase 25's RAS integration lab parks `10.0.99.1/24` on the trunk
parent (`tests/integration/test_vlan.py`, RasLab). Inert (untagged frames are dropped at
a native-less trunk), but non-obvious — and the Phase 25 spec's SDK example uses bare
`router.eth1`, which silently doesn't work as written.

## Fix Sketch (~5 lines)
In `_bootstrap_vyos_via_console`, decouple the two concerns in the iface loop:
- rename `e<i+2>`→`eth<i>` + `set interfaces ethernet eth<i> hw-id <mac>`: do for EVERY
  iface with `eth_name` + `mac` (IP or not)
- `set interfaces ethernet eth<i> address <ip>/<cidr>` (+ gateway route): only when `ip`

## Steps
- [ ] Unit test: bootstrap cmd generation includes rename + hw-id for a no-IP NIC, no address cmd
- [ ] Fix the loop in `_bootstrap_vyos_via_console`
- [ ] Integration: RAS lab variant with bare `router.eth1` (drop the parking IP) on EC2
- [ ] Update Phase 25 issue note once fixed

## Progress Log
- 2026-06-09: Filed from Phase 25 Gate 2 work (phase25-coder).

## Resolution
[pending]
