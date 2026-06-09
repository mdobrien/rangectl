# Phase 19: Link Properties (WAN Simulation)
**Created**: 2026-06-03
**Status**: Complete

## Related Issues
- **Plan**: `20260527-1-vm-testbed-platform-design.md` — Phase 19
- **Track C**: Phase 19 → Phase 20 (Hub/Switch) → Phase 21 (Pcap/Mirror)

## Goal
Runtime link impairment via `tc netem` inside the range netns. Every link can be degraded on the fly — latency, bandwidth, loss, jitter, reordering, corruption, duplication. All native Linux, no external deps.

## SDK Surface

### At definition time (optional defaults)
```python
class WanLab(Range):
    name = "wan-lab"

    def define_nodes(self):
        self.hq = self.node("hq", image="ubuntu-22.04")
        self.branch = self.node("branch", image="ubuntu-22.04")

    def define_network(self):
        self.link(self.hq.eth1["10.0.1.1/24"],
                  self.branch.eth1["10.0.1.2/24"],
                  latency="50ms", bandwidth="10mbit", loss="2%")

    def verify(self):
        self.expect_reach(self.branch, "10.0.1.1")
```

### At runtime (modify live links)
```python
lab = WanLab()
lab.deploy()

link = lab.link("hq", "branch")
link.impair(latency="100ms", bandwidth="1mbit", loss="5%", jitter="20ms")
link.impair(reorder="25%", corrupt="1%")
link.clear()  # remove all impairments, restore clean link

# Default is symmetric (both TAPs impaired)
link.impair(latency="200ms")             # both hq→branch AND branch→hq

# Asymmetric — degrade only one direction
link.impair(latency="200ms", outbound="hq")  # only hq→branch is degraded
link.impair(latency="50ms", outbound="branch")  # only branch→hq
```

### CLI
```bash
rangectl link <range> <node_a> <node_b> impair --latency 100ms --loss 5%
rangectl link <range> <node_a> <node_b> clear
rangectl link <range> <node_a> <node_b> status   # show current impairments
```

### Parameters
| Param | tc netem | Example |
|-------|---------|---------|
| `latency` | `delay` | `"50ms"`, `"100ms"` |
| `jitter` | `delay X Yms` | `"10ms"` (variation around latency) |
| `bandwidth` | `rate` (tbf parent) | `"10mbit"`, `"1gbit"` |
| `loss` | `loss` | `"5%"`, `"0.1%"` |
| `reorder` | `reorder` | `"25%"` |
| `corrupt` | `corrupt` | `"1%"` |
| `duplicate` | `duplicate` | `"1%"` |

## Implementation
- `link.impair()` → `tc qdisc add/change dev <iface> root netem delay 50ms loss 2% ...` inside range netns
- Applied per-interface on the bridge side (TAP device)
- `link.clear()` → `tc qdisc del dev <iface> root` 
- Bandwidth requires a `tbf` qdisc as parent: `tc qdisc add dev <iface> root handle 1: tbf rate 10mbit burst 32kbit latency 50ms` then `tc qdisc add dev <iface> parent 1:1 handle 10: netem delay 50ms`
- Integrates with `link.down()/up()` — impairments re-applied after `link.up()`
- Store current impairment state on the Link object for re-application and status queries

### New/changed modules
- `rangectl/topology.py` — `Link.impair()`, `Link.clear()`, `Link.impairments` property
- `rangectl/link_properties.py` (new) — tc netem command builders
- `rangectl/cli.py` — `rangectl link` subcommand

## Integration Tests (SDK-based)
```python
class ImpairmentLab(Range):
    name = "impair-lab"
    def define_nodes(self):
        self.a = self.node("a", image="ubuntu-22.04")
        self.b = self.node("b", image="ubuntu-22.04")
    def define_network(self):
        self.link(self.a.eth1["10.0.1.1/24"], self.b.eth1["10.0.1.2/24"])
    def verify(self):
        self.expect_reach(self.b, "10.0.1.1")

lab = ImpairmentLab()
lab.deploy()

# Baseline ping
result = lab["a"].run("ping -c 5 10.0.1.2")
# expect ~0.x ms RTT

# Apply latency
lab.link("a", "b").impair(latency="100ms")
result = lab["a"].run("ping -c 5 10.0.1.2")
# expect ~100ms RTT

# Clear
lab.link("a", "b").clear()
result = lab["a"].run("ping -c 5 10.0.1.2")
# expect ~0.x ms RTT again
```

## Success Criteria
- [x] `link.impair()` applies tc netem rules inside range netns
- [x] `link.clear()` removes all impairments
- [x] Latency measurable via ping (100ms impairment → ~202ms RTT, both taps)
- [x] Bandwidth limiting works (tbf parent qdisc — confirmed `tbf 1: root rate 10Mbit`)
- [x] Loss/jitter/reorder/corrupt/duplicate all apply (100% loss ⇒ ping fails; rest unit-tested)
- [x] Symmetric by default (both TAPs impaired when no `outbound=` specified)
- [x] Asymmetric via `outbound=` param (one TAP carries netem, verified at qdisc level)
- [x] Definition-time defaults applied at deploy (80ms ⇒ ~160ms RTT)
- [x] Impairments survive `link.up()` (re-applied after link restoration ⇒ ~201ms post-up)
- [x] `Link.impairments` property returns current state
- [x] CLI: `rangectl link impair/clear/status`
- [x] Unit tests: tc command generation (mocked) — 18 in test_link_properties.py
- [x] Integration tests: measurable latency change via ping — Gate 2 2/2 on EC2

## Resolution (2026-06-07)

Implemented on branch `phase19-link-properties`.

### Changes
- `rangectl/link_properties.py` (new) — pure tc builders `build_netem_cmds` /
  `build_clear_cmds`. netns-prefixed argv lists; bandwidth uses tbf root +
  netem child; reorder auto-injects a base delay (netem requires it).
- `rangectl/topology.py` — `Link.impair()`, `Link.clear()`, `Link.impairments`
  property, per-side `_impairments` state, `up()` re-applies after restore.
  `Topology.link()` / `Range.link()` accept impairment kwargs as
  definition-time defaults. `Range.connect()` rebuilds links from the DB so
  impair/clear work cross-process (CLI).
- `rangectl/engine.py` — applies `_default_impairments` after wiring each link.
- `rangectl/libvirt_backend.py` — `run_tc()` (tolerant tc runner).
- `rangectl/state.py` — `list_links()`.
- `rangectl/cli.py` — `rangectl link <range> <a> <b> impair|clear|status`.
- `tests/unit/conftest.py` — MockBackend `_find_tap_for_mac` / `run_tc` + tc
  inspection helpers.

### Direction semantics (root-cause note)
netem on a host-side TAP shapes egress of THAT tap = delivery INTO that VM.
`outbound="a"` targets node a's own TAP (per the spec's wording). A ping RTT
CANNOT distinguish direction: every round trip crosses each TAP exactly once,
so a single shaped TAP adds its delay to RTT regardless of ping direction
(symmetric 100ms×2 taps ⇒ ~199ms RTT confirms this). Asymmetry is therefore
verified at the qdisc level (exactly one TAP carries netem), not via ping
direction.

### Gate output
- Gate 1 (unit): `326 passed` (incl. 18 new in test_link_properties.py).
  Re-confirmed locally 2026-06-09.
- Gate 2 (integration, EC2 44.210.81.28, KVM) — re-run 2026-06-09, `2 passed
  in 105.11s`:

```
tests/integration/test_link_properties.py::test_link_impairment_via_ping
  baseline avg RTT = 0.252 ms
  impaired avg RTT = 202.630 ms        (100ms symmetric, both taps -> ~2x)
  cleared avg RTT  = 0.369 ms
  asym single-tap avg RTT = 201.2 ms   (outbound="a", only a's TAP carries netem)
  post-up avg RTT  = 200.857 ms        (impairment survived link down/up)
  qdisc on vnet2: qdisc tbf 1: root ... rate 10Mbit ... (tbf root + netem child)
PASSED
tests/integration/test_link_properties.py::test_definition_time_default_impairment
  default-impaired avg RTT = 160.463 ms  (80ms default live at deploy, both taps)
PASSED
======================== 2 passed in 105.11s (0:01:45) =========================
```

  Note: a single tolerated WARNING `tc qdisc del dev vnet3 root (2)` fired
  during the asymmetric clear() — clearing the side that never had a qdisc.
  `run_tc()` is intentionally tolerant of this; both tests passed clean. No
  source changes were needed for Gate 2.

### Success criteria — all met
- [x] impair applies tc netem on TAPs in range netns
- [x] clear removes impairments
- [x] symmetric default / asymmetric outbound=
- [x] latency measurable via ping; bandwidth tbf qdisc; loss/jitter/reorder/corrupt/duplicate
- [x] impairments survive up(); Link.impairments property
- [x] definition-time defaults at deploy
- [x] CLI impair/clear/status
- [x] unit + integration tests
