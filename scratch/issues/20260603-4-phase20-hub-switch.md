# Phase 20: Hub & Switch Node Types
**Created**: 2026-06-03
**Status**: In Progress (Gate 1 green; Gate 2 pending EC2)
**Depends on**: Phase 19 (Link Properties)

## Related Issues
- **Design**: `20260609-2-phase20-hub-switch-design.md` — options/considerations + recommendation (bridge mechanics, object model, impairment interplay, loop handling)
- **Plan**: `20260527-1-vm-testbed-platform-design.md` — Phase 20
- **Track C**: Phase 19 (Link Properties) → Phase 20 (this) → Phase 21 (Pcap/Mirror)

## Goal
L2 network devices as first-class node types. No VM, no container — just a bridge with specific forwarding behavior inside the range netns. Instant to create, zero boot time, no health check.

## SDK Surface

```python
class NetworkLab(Range):
    name = "net-lab"

    def define_nodes(self):
        self.router = self.node("router", image="vyos-1.4", os_type=OSType.VYOS)
        self.server = self.node("server", image="ubuntu-22.04")
        self.client = self.node("client", image="ubuntu-22.04")
        self.ids = self.node("ids", image="ubuntu-22.04")

        # L2 devices — no image, no boot
        self.sw = self.switch("core-switch", ports=8)
        self.hub = self.hub("monitor-hub", ports=4)

    def define_network(self):
        # Wire VMs to switch
        self.link(self.router.eth1["10.0.1.1/24"], self.sw.port0)
        self.link(self.server.eth1["10.0.1.2/24"], self.sw.port1)
        self.link(self.client.eth1["10.0.1.3/24"], self.sw.port2)

        # Switch uplink to hub (IDS sees all traffic)
        self.link(self.sw.port3, self.hub.port0)
        self.link(self.ids.eth1, self.hub.port1)

    def verify(self):
        self.expect_reach(self.server, "10.0.1.1")
        self.expect_reach(self.client, "10.0.1.1")
```

### Key API additions
- `Range.switch(name, ports=N)` — returns a Switch node (Linux bridge, MAC learning on)
- `Range.hub(name, ports=N)` — returns a Hub node (Linux bridge, MAC learning off, flood all)
- `switch.portN` / `hub.portN` — port interfaces for linking
- Switch/Hub are never "booted" — created as bridges during infra setup, before VMs

### CLI
```bash
rangectl status <range>          # shows switch/hub nodes as type=switch/hub, state=ACTIVE
rangectl net <range>             # shows switch/hub bridges in topology view
```

## Implementation
- **Switch**: standard Linux bridge (`ip link add <name> type bridge`) — same as existing data bridges but user-named and port-aware
- **Hub**: Linux bridge with MAC learning disabled per port (`bridge link set dev <port> learning off flood on`) — all frames flooded to all ports
- No state machine (DEFINED → ACTIVE, skip BOOTING/READY), no health check, no SSH
- Created during infra setup (Step 4 in engine), before VM boot
- Ports are bridge interfaces — VMs attach TAPs to them via existing `attach_interface`

### New/changed modules
- `rangectl/topology.py` — `Range.switch()`, `Range.hub()`, `Switch`/`Hub` node subclass or node type
- `rangectl/engine.py` — create switch/hub bridges during infra setup, skip boot for L2 nodes
- `rangectl/types.py` — `NodeType.SWITCH`, `NodeType.HUB` (or use existing `OSType` with new variants)

## Integration Tests (SDK-based)
- Deploy range with switch + 3 VMs → all VMs can ping each other through switch
- Deploy range with hub + 2 VMs + IDS → IDS sees traffic between VMs (tcpdump on IDS interface)
- Switch does NOT flood (unicast after MAC learning) — verify IDS on switch does NOT see other VMs' unicast after learning
- Hub DOES flood — verify IDS sees all traffic

## Success Criteria
- [x] `Range.switch(name, ports)` creates a MAC-learning bridge
- [x] `Range.hub(name, ports)` creates a flood-all bridge
- [x] Switch/hub created instantly (no boot, no health check)
- [x] `switch.portN` / `hub.portN` work in `link()` calls
- [ ] VMs can communicate through switch (Gate 2)
- [ ] Hub floods all traffic to all ports (Gate 2)
- [ ] Switch only forwards learned unicast (Gate 2)
- [x] Switch/Hub show in `rangectl status` and `rangectl net`
- [x] Link properties (Phase 19) work on switch/hub ports (unit; Gate 2 pending)
- [x] Unit tests: bridge creation, MAC learning flag, port assignment
- [x] Integration tests WRITTEN (`tests/integration/test_hub_switch.py`); not yet run (EC2 owned by phase16c)

## Progress Log
- 2026-06-09 (phase20-coder, branch `phase20-hub-switch`): Implemented full design
  D1-D8 from `20260609-2-phase20-hub-switch-design.md`. **Gate 1: 405/405 unit
  tests, zero skips** (was 358; +47 in `tests/unit/test_hub_switch.py`).
  - `topology.py`: `L2Node` (lazy `portN` via `__getattr__`, `ports=` cap,
    `bridge_name` sw-/hub-, 15-char IFNAMSIZ check), `Topology.switch/hub`,
    `Range.switch/hub`, `Node.is_l2`. `LinkEndpoint` dataclass (D6): lazy
    MAC→TAP for VMs, static dev for veth ends, `resolve()`; `Link` impair/
    clear/down/up/_reapply rewired to endpoints; `outbound=` toward L2 raises;
    L2↔L2 down/up deletes/recreates the veth pair; hub flags re-applied on
    `up()`. `Range.connect()` rebuilds L2 nodes + endpoint shapes from DB.
    `from_yaml` round-trips L2 nodes.
  - `engine.py`: `_l2_veth_names`, `_check_l2_cycles` (union-find, aborts
    pre-allocation naming the looped nodes, D7), `_provision_l2_node` (state
    sprint PROVISIONING→READY→LINKED→RUNNING during Step 4, before VM boot),
    `_bridge_for_link` (VM↔L2 → L2 bridge, no data-<i>; L2↔L2 → None/veth),
    `_wire_link` builds LinkEndpoints + applies hub `learning off flood on`
    per attach; L2 nodes excluded from boot/mgmt-IP/LiveNode/dep-injection.
  - `types.py`: `OSType.SWITCH/HUB` (no new NodeStates — D5 reuse).
  - Backends: `create_veth_pair`/`delete_device`/`set_port_flags` on Protocol,
    LibvirtBackend (netns-aware), MockBackend (records + `veths`/`port_flags`).
  - `state.py`: `StateDB.list_bridges`. `cli.py`: status skips power query for
    L2 rows (image/IP render `-`); `net` lists L2 devices with enslaved ports.
    `agents/docs/cli-reference.md` updated.
  - **Deviations/notes**: (1) nodes.image column is NOT NULL in existing DBs, so
    L2 rows store `""` (rendered `-`) instead of NULL — avoids a SQLite table
    rebuild migration; intent of D8 (no image) preserved. (2)
    `set_port_flags(learning=False)` also runs `bridge fdb flush dev <port>`
    (best-effort) — libvirt enslaves TAPs at VM start, before Step 8 applies hub
    flags, so the hub would otherwise hold learned entries for up to the ageing
    time and behave like a switch in the IDS test. Verify on EC2.
  - Gate 2 tests written (5 tests: switch mesh+isolation+impair+CLI, hub
    floods to IDS, switch↔hub uplink + veth impair, loop abort). NOT run.
