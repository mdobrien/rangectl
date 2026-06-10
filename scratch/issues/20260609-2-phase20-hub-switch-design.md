# Design: Phase 20 Hub & Switch — Options & Recommendation
**Created**: 2026-06-09
**Status**: Complete (design approved 2026-06-09)

## Related Issues
- **Parent**: `20260603-4-phase20-hub-switch.md` — Phase 20 spec (links back here)
- **Child**: `20260609-4-phase25-vlan-support.md` — Phase 25: native 802.1Q via bridge vlan_filtering (user picked VLAN option B, 2026-06-09)
- **Phase 19**: `20260603-3-phase19-link-properties.md` — impairment targets TAPs; interplay in D6
- **Phase 21**: `20260603-5-phase21-pcap-mirror.md` — hub is the "see all traffic" alternative to mirroring
- **Plan**: `20260527-1-vm-testbed-platform-design.md` — Phase 20

## Goal
Hubs and switches as instant, boot-free L2 devices in a range. This doc enumerates implementation
options with pros/cons (learning-oriented), grounded in the actual codebase.

---

## Background: what a Linux bridge actually is (the learning bit)

A Linux bridge is a **kernel-space L2 switch**. Every link rangectl creates today already makes one
(`data-0`, `data-1`, …) — Phase 20 mostly *exposes* what already exists, with controllable behavior.

How a frame moves through a bridge:
1. Frame arrives on a bridge **port** (a TAP, veth, or any enslaved interface).
2. Bridge records `src MAC → arrival port` in its **FDB** (forwarding database). That's "MAC learning".
3. Destination lookup in the FDB:
   - **known unicast** → forward out exactly that one port (this is what makes it a switch)
   - **unknown unicast / broadcast / multicast** → **flood** out every port except the arrival one
4. FDB entries expire (default ageing ~300s) and are re-learned.

So:
- **Switch** = bridge with defaults. After learning, unicast is private to the two ports involved.
- **Hub** = bridge with learning suppressed → every frame is "unknown" → everything floods to all
  ports. That's why an IDS plugged into a hub sees all traffic.

Two caveats worth knowing:
- Even a *switch* floods broadcast/ARP/multicast — an IDS on a switch sees ARP chatter, just not
  learned unicast. Tests must assert on unicast specifically.
- Flooding to a port ≠ the receiving VM accepts the frame. A NIC drops frames not addressed to its
  MAC unless in **promiscuous mode**. `tcpdump` enables promisc automatically; a passive IDS service
  that doesn't must set it. Integration tests should use tcpdump (and note this).

---

## D1: What implements the device?

| Option | How | Pros | Cons |
|---|---|---|---|
| **A. Linux bridge** ✅ | switch = defaults; hub = learning off (see D2) | Zero boot, zero RAM, native, engine already creates bridges everywhere; per-port `bridge link set` control | No real control plane (no VLANs/STP config/LACP); "ports" are a software fiction |
| B. OVS bridge | `ovs-vsctl` per device | Real SPAN/VLAN/OpenFlow | New host dependency; explicitly deferred in arch doc §15.1; overkill for hub/switch semantics |
| C. Full VM (Linux/VyOS bridging) | a VM whose interfaces are bridged internally | Most realistic: real STP, VLANs, CLI a student can log into | 30-60s boot, ~1GB RAM per device, defeats "instant L2 device"; users can already build this manually with `node()` today |
| D. Container running a bridge | bridge inside container netns + veth plumbing | Lighter than VM | Docker netns vs range netns plumbing is gnarly; no benefit over A — the kernel bridge is the same code either way |

**Recommendation: A.** C is not lost — "switch as a VM" is just a normal node a user defines today;
Phase 20 is for the cheap structural case. B stays deferred with OVS as a future bridge driver.

## D2: Hub mechanics — how to suppress learning

| Option | Command | Pros | Cons |
|---|---|---|---|
| **A. Per-port learning off + flood on** ✅ | `bridge link set dev <port> learning off flood on` (each port, incl. ports added later) | Issue spec's plan; true hub semantics (FDB never populated); per-port = mixed-mode possible later | Must re-apply on every port attach — engine owns all attach paths, so this is one helper call |
| B. Bridge-wide ageing 0 | `ip link set <br> type bridge ageing_time 0` | One setting at create; nothing per-port | Subtly different semantics (entries learned then instantly stale); kernel-version edge cases; harder to explain |
| C. tc-mirror everything to every port | — | none | Reinvents flooding badly; Phase 21 territory |

**Recommendation: A** — matches the spec, semantics are exact, and the per-port hook lives in the
same code path that attaches ports anyway.

## D3: Object model — are hub/switch *nodes*?

Today: `Node` carries image/container, vcpu, memory, `os_type` (TEXT in StateDB), interfaces appear
lazily via `node.eth1` `__getattr__`. Every `link()` creates a bridge named `data-<i>`.

| Option | Shape | Pros | Cons |
|---|---|---|---|
| **A. Node with new os_type values** ✅ | `Range.switch(name)` / `Range.hub(name)` return Node-subclass; `os_type="switch"/"hub"`; `vm_id=NULL` | First-class in StateDB/CLI/status with near-zero schema change (os_type is TEXT); matches issue spec; `depends_on=[switch]` works free | A few `is_l2` branches in engine/CLI (skip boot, skip SSH, skip power query) |
| B. New NodeType axis alongside OSType | separate enum + column | "Cleaner" taxonomy | Schema migration + duplicate dispatch for zero current benefit — premature |
| C. Not a node: a "segment" primitive | `self.segment("lan1")`; `link(vm.eth1, segment)` | Honest model — a switch IS just a named bridge, and 2-endpoint links already are bridges | New SDK concept users must learn; doesn't show in `status`; hub-to-switch uplinks get awkward; diverges from issue spec |
| D. Port-count fidelity: `ports=8` preallocated | `sw.port0…port7`, error on overflow | Pedagogically "real" | Pure validation code; lazy `sw.port3` via the existing `__getattr__` pattern costs nothing |

**Recommendation: A + lazy ports** (accept `ports=N` kwarg, enforce as a cap, but create port
specs lazily like `node.eth1` does). Option C is the intellectually pure model but C's insight
ships *inside* A: the Switch node's "body" is simply the bridge.

## D4: How links wire when an endpoint is L2

Today `_wire_link` creates bridge `data-<i>` and attaches both endpoints' TAPs to it.

- **VM ↔ switch/hub**: do NOT create a new `data-<i>` bridge — attach the VM's TAP **directly to the
  L2 node's own bridge**. The switch's bridge *is* the segment. (Creating a bridge-per-link and then
  joining it to the switch bridge would need a veth per link for zero benefit.)
- **switch ↔ switch / switch ↔ hub**: two distinct bridges need a **veth pair**, one end enslaved to
  each bridge (you cannot enslave a bridge to a bridge). New small mechanism in netns.py; veth ends
  are named per the existing hash scheme. On the hub side, the veth port gets `learning off flood on`
  like any other hub port.
- Loop hazard: see D7.

## D5: State machine

| Option | Pros | Cons |
|---|---|---|
| New `ACTIVE` state, path `DEFINED→ACTIVE` | Mirrors issue spec wording | New state + transition-table changes + every consumer (CLI, DB, waves) learns a new state — for a node that just skips work |
| **Reuse existing states, no-op the boot** ✅ | Containers already prove the pattern (instant READY, no cloud-init); zero changes to types.py transitions; DAG/wave code untouched | `RUNNING` reads slightly odd for a bridge (acceptable) |

**Recommendation: reuse.** L2 nodes go PROVISIONING (create bridge + hub flags) → READY (bridge
exists — that IS the health check) → LINKED → RUNNING, all in microseconds during the infra step,
before any VM boots. `ready_when`/SSH/health-probe paths are skipped (`is_l2` check), as is backend
power status in CLI.

## D6: Phase 19 impairment interplay

`Link.impair()` resolves endpoints as `(vm_id, mac) → TAP`. L2 endpoints break that assumption:

- **VM ↔ L2 link**: only one endpoint has a TAP. Impair targets the VM TAP — works today, but
  "symmetric" semantics collapse to single-TAP (delay applies once per RTT instead of twice).
  Document it; `outbound=` toward the L2 node is meaningless and should raise a clear error.
- **L2 ↔ L2 link**: no TAPs at all, but the **veth ends from D4 are tc-able** — netem on a veth works
  identically. Fix: generalize `Link._endpoints` entries from `(vm_id, mac)` to a resolved device
  name (TAP or veth). MockBackend `_find_tap_for_mac` grows a sibling for veth endpoints.

This is the main hidden cost of Phase 20 — a small refactor of Phase 19's endpoint model, with unit
tests for both shapes.

**Clarification (user question 2026-06-09): MACs are assigned, not discovered.** rangectl generates
every interface MAC itself and writes it into the libvirt domain XML / container veth at create
time — no guest cooperation, so guest OS (Linux/Windows/VyOS/container) is irrelevant; the TAP and
its MAC are host-side objects. The only runtime-resolved fact is the **libvirt TAP name** (`vnetN`,
allocated by libvirt in boot order, can change across restart/snapshot-revert) — which is why
resolution must stay **lazy at use time** (`virsh domiflist` by our own MAC), exactly as Phase 19
does today. Container veths and L2↔L2 veths are named by the engine itself — fully deterministic,
zero discovery. So the generalization *reduces* runtime dependence: per-endpoint resolver = lazy
MAC→TAP for libvirt VMs, static engine-chosen names for everything else. Do NOT cache resolved TAP
names in the DB (staleness across power events); cache the (vm_id, mac) key and resolve on demand.

## D7: Loops & STP (switch↔switch↔switch…)

Linux bridges ship with **STP off**. If a user wires a cycle of switches, broadcast frames circulate
forever — a broadcast storm. It's contained inside the range netns (cgroup caps the CPU damage,
host unaffected — the isolation layers doing their job), but the range's network melts.

| Option | Pros | Cons |
|---|---|---|
| **A. Detect cycles in the L2 subgraph at deploy, abort with clear error** ✅ | Cheap (graph walk over links between L2 nodes); deterministic; matches the "abort loudly" precedent from Phase 16 D3b | Forbids intentional redundant-path labs |
| B. Enable kernel STP on switches (`stp_state 1`) | Real switches run STP; redundant topologies converge | 30-50s convergence delays deploy verification; surprising blocked-port behavior in tests; hubs would still storm |
| C. Do nothing, document | Zero code | Storm is a miserable debugging experience for exactly the learners this targets |

**Recommendation: A now**, with B (`stp=True` opt-in per switch) listed as a future follow-up if a
redundancy lab is ever wanted.

## D8: Persistence & CLI

- StateDB `nodes` row: `os_type="switch"/"hub"`, `vm_id=NULL`, `image=NULL`, mgmt_ip NULL (L2 nodes
  get **no mgmt interface** — nothing to SSH to; keeps the mgmt bridge clean).
- The L2 node's bridge is recorded in the `bridges` table (`bridge_type="switch"/"hub"`), named like
  other ns-mode bridges: `sw-<name>`/`hub-<name>` (netns-scoped, so clean names are safe).
- `rangectl status`: render state without backend power query; OS column shows `switch`/`hub`.
- `rangectl net`: list L2 nodes with their bridge and enslaved ports (`bridge link show` in netns).
- `Range.connect()` rebuild: L2 nodes reconstruct from DB like links do (Phase 19 precedent).

---

## Recommended shape (summary)

1. `Range.switch(name, ports=None)` / `Range.hub(name, ports=None)` → Node subclass, `os_type`
   `"switch"`/`"hub"`, lazy `portN` interfaces (capped if `ports=` given).
2. Switch = plain bridge; hub = same bridge + `learning off flood on` applied to every port at
   attach time (D2-A).
3. Links: VM↔L2 attaches the TAP straight onto the L2 bridge (no `data-<i>`); L2↔L2 uses a veth
   pair (D4).
4. No new states — L2 nodes sprint through the existing machine during infra setup (D5).
5. Generalize Phase 19 `_endpoints` to device names so impairment works on veth ends; `outbound=`
   toward an L2 node errors (D6).
6. Cycle detection over the L2 subgraph at deploy → abort with named loop (D7); `stp=True` deferred.
7. No mgmt interface on L2 nodes; CLI renders them without power/SSH queries (D8).

Integration test sketch (one range): 3 VMs on a switch (all ping), IDS-on-switch does NOT see
learned unicast between two others (tcpdump filtered to their IPs), same IDS moved to a hub DOES,
switch↔hub uplink carries traffic, impair on a VM↔switch link measurably delays ping, cycle abort
fires on a deliberately-looped definition.

## Decisions (user-confirmed 2026-06-09)
- **D7: cycles abort at deploy** (no auto-STP; `stp=True` opt-in deferred).
- **D8: no mgmt IP on L2 nodes**; `sw-<name>`/`hub-<name>` bridge naming confirmed.

## Unresolved
- None.

## Progress Log
- 2026-06-09: Explored object model (types.py states/OSType, topology.py Node/InterfaceSpec/Link,
  engine wiring steps, StateDB schema, MockBackend, Phase 19 endpoint targeting). Wrote options.
  Awaiting user review.
- 2026-06-09: User confirmed D7 (abort on cycles), D8 (no mgmt IP, sw-/hub- naming). Design final.
  Review interplay noted (`20260609-3-architecture-code-review-findings.md`): fold Protocol sync
  (M1) + per-side link backend fix (H8) into the D6 endpoint generalization; design the impairment
  persistence column (H6) to cover veth endpoints; on `Range.connect()` rebuild, re-apply hub
  per-port flags when `Link.up()` recreates a bridge (bridge_type from DB at re-attach).

## Resolution
Design approved: kernel bridge (D1-A), per-port learning-off hub (D2-A), Node subclass with
os_type switch/hub + lazy ports (D3-A), direct TAP attach / veth uplinks (D4), state reuse (D5),
endpoint generalization with review-fix foldins (D6), cycle abort (D7), no mgmt IP + sw-/hub-
naming (D8). Implementation proceeds in parent `20260603-4-phase20-hub-switch.md`.
