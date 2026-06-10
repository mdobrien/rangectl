# Phase 21: Port Mirroring, SPAN & Packet Capture
**Created**: 2026-06-03
**Status**: Complete

## Progress Log
- 2026-06-09 (phase21-coder): EC2 primitive verify done BEFORE building (Phase 20/25 lesson):
  - iproute2/tc OK, tcpdump 4.99.1, nsenter present.
  - `clsact` add OK; 2nd add → "Exclusivity flag on" error ⇒ idempotent install = del-then-add.
  - `matchall action mirred egress mirror` OK on both hooks; `tc filter show dev X ingress` shows "mirred (Egress Mirror to device ...)".
  - root netem + clsact coexist on one device (separate slots).
  - Kernel-reap proven at primitive level: `nsenter -t <pidns-init> -p -m -- sleep` forks the child INTO the pid-ns; child reaped when init exits (`REAP_OK`); nsenter parent propagates the signal. `nsenter -p` target must be libvirtd itself (= child of range.json's unshare wrapper pid).
- 2026-06-09 (phase21-coder): Gate 1 green — 516/516 (464 base + 52 new), zero skips.
  New: rangectl/capture.py, rangectl/mirror.py, StateDB captures/mirrors tables (D5-B intent/index),
  Range.capture/stop_capture/captures/mirror/unmirror/mirrors, find_link_endpoint (LinkEndpoint reuse),
  Link._mirrors + up() re-apply, connect() intent rebuild, LibvirtBackend.spawn_capture/tc_filter_show,
  CLI capture/capture-stop/captures/mirror/unmirror/mirrors, MockBackend spawn/tc-filter recording.
  Note: `-Z root` on tcpdump (privilege drop would break -w into root-owned captures dir).
  Mirror-to-IDS verified at the IDS's TAP via the capture API (in-guest tcpdump isn't in the cloud image;
  the TAP egress IS what gets delivered to the sensor).
- 2026-06-09 (phase21-coder): Gate 2 runs 1+2 FAILED at BPF-filter check: 4/5 echo-requests.
  Debugged per strict rules with `scratch/scripts/debug_pcap_seq.py` (3 rounds, prints captured
  ICMP seqs): **seq 5 missing every round** — the TAIL packet, not the head.
  ROOT CAUSE: libpcap batches ring-buffer blocks with a ~1s timeout; a packet arriving <1s
  before stop() is still in the unread block when SIGTERM breaks the capture loop, and tcpdump
  exits without draining it. `-U` alone is insufficient (it only flushes packets already READ).
  FIX (at source): `--immediate-mode -U` in build_capture_cmd — per-packet delivery from the
  kernel + per-packet savefile flush makes stop() deterministic: everything on the wire before
  stop() is in the pcap. Verified: 5/5 in all rounds + full Gate 2 green after fix.
  (Trade-off: more syscalls per packet; fine for testbed observability.)
**Depends on**: Phase 20 (Hub & Switch)

## Related Issues
- **Design**: `20260609-14-phase21-pcap-mirror-design.md` — full design review post-Phase 16/20:
  PID-ns capture spawning, LinkEndpoint reuse, capture-on-L2-node replaces capture_bridge,
  persistence aligned with H6
- **Plan**: `20260527-1-vm-testbed-platform-design.md` — Phase 21
- **Track C**: Phase 19 → Phase 20 → Phase 21 (this)

## Goal
Observe traffic on any interface or bridge inside a range. Two capabilities: capture (write pcap files) and mirror (copy traffic to another port). All via native Linux tools inside the range netns.

## SDK Surface

### Packet capture
```python
lab = MyLab()
lab.deploy()

# Capture on a node interface
cap = lab.capture("router", "eth1")
cap = lab.capture("router", "eth1",
                  filter="tcp port 80",
                  output="/tmp/http.pcap")

# Let traffic flow...
lab["client"].run("curl http://10.0.1.2")

# Stop and retrieve
cap.stop()
pcap_path = cap.file   # path to pcap on host

# Capture on a bridge (sees all segment traffic)
cap = lab.capture_bridge("data-0")
```

### Port mirroring
```python
# Mirror all traffic on router.eth1 to IDS sensor
lab.mirror("router", "eth1", to="ids-sensor", port="eth0")

# Directional
lab.mirror("router", "eth1", to="ids-sensor", port="eth0",
           direction="ingress")

# Remove
lab.unmirror("router", "eth1")
```

### Context manager for captures
```python
with lab.capture("router", "eth1") as cap:
    lab["client"].run("curl http://10.0.1.2")
# auto-stopped, pcap available at cap.file
```

### CLI
```bash
rangectl capture <range> <node> <iface> [--filter "tcp port 80"] [--output /tmp/cap.pcap]
rangectl capture-stop <range> <capture-id>
rangectl mirror <range> <src-node> <src-iface> <dst-node> <dst-iface> [--direction ingress|egress|both]
rangectl unmirror <range> <src-node> <src-iface>
```

## Implementation

### Capture
- `tcpdump -i <iface> -w <file> <filter>` as background process inside range netns (`ip netns exec`)
- `Capture` handle tracks PID, netns, output file
- `cap.stop()` sends SIGTERM to tcpdump
- Capture files stored in `/ranges/<name>/captures/`
- Range destroy kills running captures + cleans capture dir

### Mirror
- **DECIDED (2026-06-09): use `clsact`, NOT the legacy `ingress` qdisc.** `clsact` provides
  both ingress and egress filter hooks (the `ingress` qdisc is receive-side only), and it
  occupies a separate slot from the root qdisc — so Phase 19 netem/tbf impairment and
  mirroring coexist on the same TAP. `ingress` and `clsact` are mutually exclusive on a
  device; starting with `clsact` avoids a migration later.
- `tc qdisc add dev <src> clsact`
- `tc filter add dev <src> ingress matchall action mirred egress mirror dev <dst>` (incoming)
- `tc filter add dev <src> egress matchall action mirred egress mirror dev <dst>` (outgoing)
- Works on TAP devices inside the netns
- `unmirror` removes the clsact qdisc (drops all its filters)
- Integrates with link.down()/up() — mirrors re-applied after link restoration
- Unit test required: impair (root netem) + mirror (clsact) on the SAME TAP — no conflict

### New/changed modules
- `rangectl/capture.py` (new) — Capture class, start/stop, file management
- `rangectl/mirror.py` (new) — mirror/unmirror tc commands
- `rangectl/topology.py` — `Range.capture()`, `Range.capture_bridge()`, `Range.mirror()`, `Range.unmirror()`
- `rangectl/cli.py` — capture/mirror subcommands

## Integration Tests (SDK-based)
- Capture on a link → generate traffic → stop → verify pcap has packets (read with `tcpdump -r`)
- Capture with BPF filter → verify only filtered traffic captured
- Mirror traffic to IDS node → verify IDS sees mirrored packets
- Directional mirror (ingress only) → verify egress not mirrored
- Context manager auto-stop works

## Success Criteria
- [x] `lab.capture()` starts tcpdump in range netns, returns Capture handle (net+PID+mnt ns per design D2-B)
- [x] `cap.stop()` stops capture, pcap file accessible (SIGTERM → 5s → SIGKILL + possibly_truncated)
- [x] `cap.file` returns path to pcap
- [x] BPF filters work (Gate 2: exact echo-request count)
- [x] `lab.capture_bridge()` captures on bridge interface — REPLACED per design D3 by `lab.capture(l2node)` + `bridge=` escape hatch (both tested)
- [x] Context manager auto-stops capture (unit-tested)
- [x] `lab.mirror()` applies tc mirred rules (clsact + matchall)
- [x] `lab.unmirror()` removes mirror
- [x] Directional mirroring (ingress/egress/both) — Gate 2 ingress-only count assertion
- [x] Mirrors survive link.up() (re-applied) — Gate 2 verified
- [x] Range destroy cleans up captures and mirrors — STRUCTURAL per design D2-B: kernel reaps the capture with the PID ns (Gate 2 test e proves zero orphan, zero cleanup code); mirrors die with their devices
- [x] CLI: capture/capture-stop/captures/mirror/unmirror/mirrors
- [x] Unit tests: command generation (mocked) — 52 new, Gate 1 516/516
- [x] Integration tests: pcap has packets, mirror visible on sensor — Gate 2 2/2 + Phase 19 regression 2/2

## Resolution
Implemented exactly per `20260609-14-phase21-pcap-mirror-design.md`.
- New: `rangectl/capture.py` (nsenter -n -p -m tcpdump, Capture handle, packet_count),
  `rangectl/mirror.py` (pure builders, del+add idempotent install), StateDB `captures`/`mirrors`
  tables (D5-B intent/index; live state answers status), `find_link_endpoint` (LinkEndpoint reuse),
  `Range.capture/stop_capture/captures/mirror/unmirror/mirrors`, `Link._mirrors` + up() re-apply,
  `Range.connect()` intent rebuild, `LibvirtBackend.spawn_capture/tc_filter_show`, 6 CLI commands,
  `tests/integration/test_pcap_mirror.py` (a-e), cli-reference.md updated.
- Gate 1: 516/516 zero skips. Gate 2 (EC2): test_capture_and_mirror + test_capture_dies_with_range
  green; test_link_properties regression green.
- One root-caused fix during Gate 2: tcpdump needs `--immediate-mode -U` or the kernel-ring tail
  (last ~1s of packets) is dropped at SIGTERM (see Progress Log; debug script
  `scratch/scripts/debug_pcap_seq.py`).
