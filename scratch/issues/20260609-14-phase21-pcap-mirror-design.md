# Design: Phase 21 Pcap & Mirror — Options & Recommendation
**Created**: 2026-06-09
**Status**: Complete (finalized by team-lead under user-delegated autonomy, 2026-06-09)

## Related Issues
- **Parent**: `20260603-5-phase21-pcap-mirror.md` — Phase 21 spec (clsact decision already recorded there 2026-06-09; links back here)
- **Phase 20**: `20260603-4-phase20-hub-switch.md` — LinkEndpoint resolver + L2 bridges this design reuses
- **Phase 22 design**: `20260609-12-phase22-services-design.md` — D3 PID-ns spawn insight reused here for tcpdump
- **H6 design**: `20260609-9-h6-impairments-not-persisted-design.md` — same persistence question for mirrors
- **Tutorials**: `docs/tutorials/qdisc.md`, `tc.md`, `netem.md` — background for every mechanism here
- **Plan**: `20260527-1-vm-testbed-platform-design.md` — Phase 21

## Goal
Two capabilities: **capture** (write pcap files from any interface/segment) and **mirror** (copy a
port's traffic to a sensor node, live). This review re-grounds the spec against what Phases 16/20
changed and resolves the open design dimensions.

What changed since the spec was written:
- **Phase 20 gave us `LinkEndpoint`** (`topology.py:379`) — lazy MAC→TAP for VMs, static device
  names for veths, `resolve(backend)`. Capture/mirror MUST reuse this instead of growing a third
  device-resolution path.
- **Switches are first-class bridges** (`sw-<name>`) — `capture_bridge("data-0")` from the spec
  becomes the much more natural `lab.capture("core-switch")` (capture the whole segment at the
  switch). Hubs already solve "IDS sees everything" structurally; mirror is the *runtime, selective*
  complement.
- **Phase 16 PID-ns insight** (from the Phase 22 design): processes spawned inside the range's PID
  namespace are reaped by the kernel when the range dies — no orphan tracking needed.

---

## D1: Capture mechanism

| Option | How | Pros | Cons |
|---|---|---|---|
| **A. tcpdump child process** ✅ | `tcpdump -i <dev> -w <file> <bpf>` | Kernel-speed (AF_PACKET + in-kernel BPF), standard pcap, native filters, already on the EC2 image | A process to manage — but see D2, the PID-ns trick deletes most of that cost |
| B. Python in-process (scapy/AF_PACKET) | sniff in the SDK process | no child process | Python drops packets at any real rate; scapy is a heavy dep; the SDK process lives on the HOST — it would need nsenter gymnastics per packet. Instructive-bad: puts a slow interpreter on the data path that the kernel handles for free |
| C. dumpcap/tshark | wireshark's capture engine | ring buffers built in | wireshark dependency for nothing tcpdump can't do here |

## D2: Where the capture process runs (the lifecycle question)

| Option | How | Pros | Cons |
|---|---|---|---|
| A. Host PID ns + `ip netns exec` (spec's plan) | engine spawns, records PID | simple to write | Orphan class of bugs: survives range destroy unless tracked+killed; PID file staleness; exactly what Phase 12 fought |
| **B. Inside the range's net+PID namespaces** ✅ | `nsenter -t <libvirtd-pid> -n -p -- tcpdump ...` (supervisor already records libvirtd's PID) | **Kernel reaps tcpdump when the range dies** — destroy-time cleanup is structural, not bookkeeping; same guarantee that cleans QEMU | Stop/SIGTERM needs the host-visible PID — read the spawned child's host PID at launch (we spawn it, we know it); pcap writes to `/ranges/<name>/captures/` which is host-visible (mount ns only binds libvirt dirs) |
| C. In-guest tcpdump via SSH | run inside the VM | sees exactly what the guest sees | needs tcpdump in every image (VyOS/containers vary), perturbs the guest, file retrieval hop, breaks for crashed/booting nodes — the times you most want packets |

`cap.stop()` flushes (SIGTERM → tcpdump flushes buffers and exits cleanly); `Capture.file` is the
host path. Range destroy needs zero capture-specific code under B — document that explicitly so
nobody adds redundant cleanup later.

## D3: What you can capture on (surface)

- `lab.capture(node, iface, filter=, output=)` — resolves the host-side device via the link's
  `LinkEndpoint` (VM TAP or veth). NOT the guest device name — document that eth1's TAP shows
  pre-guest egress / post-guest ingress.
- `lab.capture(l2node)` — capture on the switch/hub **bridge device** itself (sees all forwarded
  frames on the segment). Replaces the spec's `capture_bridge(bridge_name)` — users know node
  names, not internal bridge names. Keep a `bridge=` escape hatch for data-<i> link bridges.
- Context manager per spec. Multiple concurrent captures fine (independent processes).

## D4: Mirror mechanism — settled, with one addition

clsact + `matchall action mirred egress mirror` (already decided, recorded in the spec 2026-06-09;
rationale in `docs/tutorials/tc.md`). Directions: ingress/egress/both via clsact's two hooks.
Remaining choices:

| Sub-question | Options | Call |
|---|---|---|
| Mirror destination | (a) sensor node's existing data TAP ✅ — frames appear at the IDS like normal traffic; (b) dedicated mirror interface auto-added to the sensor — cleaner separation but requires hotplug NIC machinery we don't have | (a); document that mirrored frames mix with the sensor's own traffic (use BPF on the sensor to separate) |
| Mirror + netem on same TAP | clsact coexists with root qdisc — REQUIRED unit+integration test (impair and mirror simultaneously) | already in plan |
| Mirror across L2 nodes | mirror a switch *port* (TAP/veth enslaved to sw-) — same mechanism, ports are just devices | free; test it |
| 5.15 compat | clsact: kernel 4.5+/iproute2 4.5+ — fine on EC2's 5.15. But per the Phase 20 lesson (`20260609-13`), Gate 2 must verify each exact command on the box before building on it | verify-first step in kickoff |

## D5: Persistence & cross-process visibility (the H6 question again)

Captures and mirrors are runtime state created possibly by one process and inspected/stopped by
another (CLI). Same dilemma as H6 (impairments):

| Option | Pros | Cons |
|---|---|---|
| A. StateDB rows as truth | survives process exit | drifts from kernel/process reality (the H6 bug class) |
| **B. Kernel/process state as truth, DB as index** ✅ | `rangectl captures <range>` lists from `/ranges/<name>/captures/` + live process check; `mirror status` reads `tc filter show` — can't lie | needs DB rows only for *intent* (re-apply mirrors after link.up()) |
| C. No persistence (in-memory only, spec's implicit plan) | least code | CLI capture-stop/mirror-status cross-process impossible — repeats H6 exactly |

Align with whatever H6's review decides — the recommendation here (B: write-through intent +
live-state reads) matches the H6 design doc's recommended option and the Phase 16 "kernel is the
source of truth" principle. Mirrors re-apply after `link.up()` from stored intent, like impairments.

## D6: Scope check — what NOT to build
- No rotation/size caps on pcaps in v1 (`-C`/`-W` exist if a test needs them; document the flag
  pass-through `extra_args=`). A runaway capture fills `/ranges` — note it, don't engineer for it.
- No remote-sensor streaming (mirred to a VXLAN/ERSPAN tunnel) — multi-host someday.
- No pcap parsing/analysis API — `cap.file` + user's tooling. One helper allowed:
  `cap.packet_count()` via `tcpdump -r` because every integration test needs it anyway.

---

## Recommended shape (summary)
1. `rangectl/capture.py`: spawn tcpdump **inside the range's net+PID ns** via nsenter on the
   recorded libvirtd PID (D2-B); pcaps in `/ranges/<name>/captures/<id>.pcap`; `Capture` handle =
   host PID + file; context manager; `stop()` SIGTERM+wait; kernel handles destroy-time cleanup.
2. `rangectl/mirror.py`: clsact + matchall + mirred to the sensor's resolved endpoint device;
   directions via the two hooks; coexists with netem (tested); works on switch ports.
3. Device resolution everywhere via Phase 20's `LinkEndpoint.resolve()` — no new lookup paths.
4. `lab.capture(node_or_l2node, iface=None, ...)`; mirrors per spec's `lab.mirror(...)` signature.
5. Persistence per D5-B: DB rows record intent (mirror re-apply on up(), capture index), kernel and
   process state answer all status queries. Mirror this with the H6 implementation when it lands.
6. CLI: `rangectl capture/capture-stop/captures`, `rangectl mirror/unmirror` + status. Update
   cli-reference.md.
7. Gate 2: verify clsact/mirred command-by-command on the EC2 box FIRST (iproute2 5.15 lesson),
   then: pcap-has-packets, BPF filter, mirror-to-IDS visible, ingress-only directional, impair+mirror
   same TAP, switch-port mirror, capture survives nothing (dies with range — assert no orphan after
   destroy).

## Resolved decisions (team-lead, user-delegated autonomy 2026-06-09)
- **D5**: adopt B now (kernel/process state is truth; DB rows store intent only — mirror re-apply
  on `link.up()`, capture index). Rationale: matches the H6 design's recommended option AND the
  Phase 16 verify-don't-trust principle; if the user's H6 review later picks differently, the
  refactor surface is one status-reading function per feature. Decision logged, reversible cheaply.
- **D3**: confirmed — `lab.capture(l2node)` replaces `capture_bridge()`; `bridge=` kwarg kept as
  the escape hatch for internal `data-<i>` link bridges.
- **One correction to D2-B made during finalization**: tcpdump must be spawned entering the range's
  net+PID+**mount** namespaces (`nsenter -n -p -m`). PID-ns entry alone leaves the process with the
  host mount view, which is fine for /ranges writes, but entering -p without -m can leave a stale
  /proc view for tcpdump's own operation; -m matches how the supervisor launches libvirtd and is
  the tested combination. File path `/ranges/<name>/captures/` is identical in both views (the
  range mount ns only binds libvirt dirs), so no behavior change — just the safer namespace set.
- **Drop privileges note added**: tcpdump's packet-buffer flush on SIGTERM is reliable only if it
  is not killed with SIGKILL first — `stop()` sends SIGTERM, waits up to 5s, then SIGKILL +
  flags the capture as possibly-truncated in the returned handle.

## Progress Log
- 2026-06-09: Reviewed spec against post-16/20 codebase; reused LinkEndpoint + PID-ns reaping;
  aligned persistence with H6.
- 2026-06-09: User delegated final review. Resolved D5/D3, corrected the nsenter namespace set,
  specified stop() signal semantics. Design final — implementation may begin after Phase 25 lands.

## Resolution
Design complete. Implemented 2026-06-09 by `phase21-coder` exactly as specified — see
`20260603-5-phase21-pcap-mirror.md` Resolution. Gate 1 516/516, Gate 2 green (incl. kernel-reap
proof e and impair+mirror coexistence d). Two implementation-level findings worth recording:
- **stop()-flush correction**: SIGTERM alone does NOT flush packets still in the kernel ring
  block (libpcap batches ~1s); tcpdump is spawned with `--immediate-mode -U` so everything on
  the wire before stop() is in the pcap deterministically.
- The nsenter target is libvirtd itself = the CHILD of the pid recorded in range.json (the
  unshare wrapper stays in the host PID ns); `spawn_capture` resolves it via
  `supervisor._child_pids`, and stop() signals tcpdump's host PID (the nsenter fork child).
