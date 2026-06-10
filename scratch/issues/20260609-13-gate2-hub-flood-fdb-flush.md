# Bug: Gate 2 hub does not flood — `bridge fdb flush` unsupported on iproute2 5.15
**Created**: 2026-06-09
**Status**: Complete

## Related Issues
- **Parent**: `20260603-4-phase20-hub-switch.md` — Phase 20 (links back here)
- **Design**: `20260609-2-phase20-hub-switch-design.md` — D2 hub mechanics

## Symptom
First Gate 2 run of `tests/integration/test_hub_switch.py` on EC2 (Ubuntu 22.04,
iproute2 5.15): 2 failed / 2 passed.

1. `test_hub_floods_unicast_to_ids` — IDS on the hub saw **0** ICMP packets
   between a and b (expected ≥5; hub must flood).
2. `test_switch_forwarding_and_isolation` — ALL network semantics passed
   (mesh ping, switch isolation, impair, outbound-raises); only the final
   `rangectl status` subprocess check failed with exit 2.

## Root Cause (evidence-confirmed on the box)
1. **fdb flush silently failing.** `LibvirtBackend.set_port_flags` ran
   `bridge fdb flush dev <port>` (check=False) after `learning off flood on`.
   On the EC2 box:
   ```
   $ ip -V                       -> iproute2-5.15.0
   $ sudo bridge fdb flush dev tva
   Command "flush" is unknown, try "bridge fdb help".   (exit 255)
   ```
   `bridge fdb flush` only exists in iproute2 ≥ 6.1. So FDB entries learned
   during VM boot (libvirt enslaves TAPs at start; cloud-init netplan ARPs)
   survived, and the hub kept forwarding a→b as **learned unicast** — switch
   behavior — for the ~300s ageing window, longer than the test.
   Consistency check: `test_switch_hub_uplink_carries_traffic` PASSED because
   its veth uplink is created in Step 8 (after boot), so the far VM's MAC was
   never learned by the hub — its replies flooded and the IDS saw them.
2. **CLI check read the wrong DB.** The test deployed against the tmp-path
   `db` fixture, but the `rangectl status` subprocess reads the default
   `~/.rangectl/rangectl.db` → `RangeNotRunning` (exit 2). Same plumbing as
   `tests/integration/test_cli.py`, which deliberately uses the default DB.

## Fix
1. `set_port_flags(learning=False)`: replace `bridge fdb flush` with a
   portable per-entry delete — `bridge fdb show brport <port>`, then
   `bridge fdb del <mac> dev <port> master` for every non-`permanent` entry
   (works on every iproute2; permanent entries are the port's own MAC and are
   skipped). Unit-tested with patched `_run` + canned fdb output.
2. The integration test's CLI check now deploys against the default StateDB
   (pre-loaded with images, mirroring `test_cli.py`) so the CLI subprocess
   sees the range.

## Validation
- Unit: `tests/unit/test_libvirt_backend.py::test_set_port_flags_*` (red→green),
  full suite green.
- Gate 2 rerun on EC2: see parent issue progress log.

## Resolution
Root cause fixed at the source (portable FDB clear in the backend, not a
test-side sleep/workaround). Debug artifacts: ad-hoc bridge/veth probe on EC2
(testbr0/tva, removed after use); no scratch scripts left behind.
