# Phase 25: Native VLAN Support (802.1Q switches)
**Created**: 2026-06-09
**Status**: Complete
**Depends on**: Phase 20 (Hub & Switch)

## Related Issues
- **Parent design**: `20260609-2-phase20-hub-switch-design.md` — VLAN option B chosen by user 2026-06-09 (links back here)
- **Phase 20 spec**: `20260603-4-phase20-hub-switch.md` — the switch node this extends
- **Plan**: `20260527-1-vm-testbed-platform-design.md` — added as Phase 25

## Goal
Make the Phase 20 switch a real 802.1Q switch using native Linux **bridge VLAN filtering** — access
ports, trunk ports, tagged frames actually on the wire. Enables VLAN segmentation labs and
router-on-a-stick topologies where the infrastructure enforces VLAN boundaries instead of trusting
guests.

Mechanism primer: with `vlan_filtering 1`, a Linux bridge keeps a VLAN table per port and a
per-VLAN FDB. An **access** port has a PVID (untagged ingress frames get that VID; egress frames
are untagged). A **trunk** port carries multiple VIDs with tags intact. Frames only forward between
ports sharing the VID — enforcement happens in the kernel, not by guest goodwill.

## SDK Surface

```python
class VlanLab(Range):
    name = "vlan-lab"

    def define_nodes(self):
        self.router = self.node("router", image="vyos-1.4", os_type=OSType.VYOS)
        self.web = self.node("web", image="ubuntu-22.04")
        self.db = self.node("db", image="ubuntu-22.04")
        self.sw = self.switch("core", vlan_aware=True)

    def define_network(self):
        self.link(self.web.eth1["10.0.10.2/24"], self.sw.port0.access(10))
        self.link(self.db.eth1["10.0.20.2/24"],  self.sw.port1.access(20))
        self.link(self.router.eth1,              self.sw.port2.trunk(10, 20))
        # router does 802.1Q subinterfaces (eth1.10 / eth1.20) — router-on-a-stick

    def verify(self):
        self.expect_reach(self.web, "10.0.10.1")   # via router subif
```

Rules:
- `vlan_aware=True` required to use `.access()/.trunk()`; plain switches behave as Phase 20.
- A port is access XOR trunk; unconfigured ports on a vlan-aware switch default to access(1)
  (kernel default PVID) — documented, and `rangectl net` shows it.
- `trunk(..., native=N)` optional: untagged frames on a trunk map to VID N.
- Hubs are never vlan-aware (`hub(..., vlan_aware=True)` → error) — real hubs predate VLANs and
  flood-all + VLAN filtering is contradictory.

## Implementation
- Switch bridge created with `ip link set <br> type bridge vlan_filtering 1` when `vlan_aware`.
- At port attach (TAP or veth — same attach hook Phase 20 adds):
  - access: `bridge vlan add dev <port> vid N pvid untagged` (+ remove default vid 1)
  - trunk: `bridge vlan add dev <port> vid N` per VID (+ native handling)
  - the bridge's own "self" port needs no VIDs (no L3 on switch — Phase 20 D8)
- Persist port VLAN config in StateDB (extend links/bridges rows or node metadata) so
  `Range.connect()` and CLI rebuild it.
- `rangectl net <range>`: render `bridge vlan show` per vlan-aware switch.
- Impairment (Phase 19) unaffected: tc operates on the TAP/veth below the VLAN layer.

### New/changed modules
- `rangectl/topology.py` — `switch(vlan_aware=)`, `PortSpec.access()/trunk()`, validation
- `rangectl/netns.py` or backend — `bridge vlan` command helpers
- `rangectl/engine.py` — apply VLAN config at attach time
- `rangectl/state.py` — persist port VLAN config
- `rangectl/cli.py` — net view

## Integration Tests (EC2)
- Access isolation: web (vlan10) cannot reach db (vlan20) directly — ping fails
- Router-on-a-stick: VyOS trunk subinterfaces route between VLANs — cross-VLAN ping via router OK
- Tags on the wire: tcpdump -e on the trunk TAP shows 802.1Q headers with correct VIDs
- Untagged/native handling on trunk
- Non-vlan-aware switch on same range still floods/forwards normally (regression)

## Success Criteria
- [x] `vlan_aware=True` switch with access/trunk port API
- [x] Kernel-enforced isolation between VLANs (ping fails without router)
- [x] Router-on-a-stick lab passes end-to-end
- [x] 802.1Q tags visible on trunk (tcpdump -e)
- [x] hub + vlan_aware rejected with clear error
- [x] Config survives Range.connect(); CLI net shows VLAN table
- [x] Unit tests (command generation, validation); integration on EC2

## Progress Log
- 2026-06-09: Created from Phase 20 design discussion (VLAN option B). Scheduled after Phase 20.
- 2026-06-09: Implementation (phase25-coder):
  - `types.py`: `PortSpec(InterfaceSpec)` with `.access(vid)` / `.trunk(*vids, native=)`,
    VID validation 1-4094, access XOR trunk, requires `vlan_aware` switch.
  - `topology.py`: `switch(vlan_aware=)` (Topology + Range); `hub(vlan_aware=True)` →
    ValueError; lazy `portN` now creates PortSpec; `LinkEndpoint.vlan` +
    `.bridge_vlan_aware`; `Link.up()` re-enables vlan_filtering + re-applies port VLANs;
    `Range.connect()` rebuilds vlan-aware switches (from bridges.vlan_aware) and
    endpoint configs (from links.vlan_a/vlan_b JSON); YAML export/from_yaml round-trips
    `vlan_aware` + per-port configs.
  - `backend.py`/`libvirt_backend.py`: `set_vlan_filtering(bridge)` (`ip link set ...
    type bridge vlan_filtering 1`), `set_port_vlans(port, mode=, vids=, native=)` —
    per-entry `bridge vlan add/del` only (iproute2 5.15-safe), default VID 1 removed
    unless explicitly configured, access → `vid N pvid untagged`, trunk → `vid N` per
    VID + native as `pvid untagged`.
  - `engine.py`: `_provision_l2_node` enables filtering + persists bridges.vlan_aware;
    `_wire_link` persists links.vlan_a/vlan_b JSON and programs TAP/veth ports at the
    same hook as hub flags.
  - `state.py`: schema + ALTER TABLE migration (bridges.vlan_aware, links.vlan_a/b).
  - `cli.py`: `net` renders per-port VLAN table for vlan-aware switches + live
    `bridge vlan show`. cli-reference.md updated.
  - Tests: `tests/unit/test_vlan.py` (36 tests: SDK validation, engine programming,
    persistence, connect rebuild, up() re-apply, backend command generation incl.
    netns + access(1) keep-default edge, CLI render, DB migration);
    `tests/integration/test_vlan.py` (isolation lab same-subnet/diff-VLAN with
    same-VLAN control; VyOS router-on-a-stick + tags-on-wire via host tcpdump in netns).
  - Gate 1: 464/464 passed, zero skips (428 base + 36 new).
  - Note: VyOS bootstrap skips address-less NICs (no rename/hw-id), so the RAS lab
    puts a parking IP on the trunk parent (10.0.99.1/24, dropped untagged — no native).
- 2026-06-09: Gate 2 on EC2 (44.210.81.28, iproute2 5.15, host tcpdump in netns):
  ```
  $ sudo python3 -m pytest tests/integration/test_vlan.py -x -q
  ..                                                                       [100%]
  2 passed in 138.04s (0:02:18)
  ```
  - (a) access isolation: same-subnet web(10)→db(20) ping FAILS both directions;
    same-VLAN web(10)→peer(10) control ping OK — kernel-enforced.
  - (b) router-on-a-stick: VyOS eth1 vif 10/20 via SSH script-template; web→10.0.10.1
    OK, web→10.0.20.2 via router OK, reverse OK.
  - (c) tags on wire: tcpdump -e on trunk TAP inside netns shows 802.1Q with
    vlan 10 AND vlan 20 (both legs of the routed flow share the trunk).
  - (d) regression: non-vlan-aware switch/hub unaffected:
    ```
    $ sudo python3 -m pytest tests/integration/test_hub_switch.py -x -q
    ....                                                                     [100%]
    4 passed in 198.48s (0:03:18)
    ```
  - EC2 left clean: all ranges destroyed, no qemu, only rangectl-mgmt netns; box running.

## Resolution
Complete. Gate 1: 464/464 (428 base + 36 new), zero skips. Gate 2: test_vlan.py 2/2 +
test_hub_switch.py 4/4 regression on EC2. Untagged/native trunk handling covered at
the unit level (command generation incl. native pvid untagged + default-VID-1
keep/remove edge); live kernel VLAN table asserted in both integration tests.
