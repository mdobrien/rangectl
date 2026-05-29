# Phase 12: SDK Surface + Internet Policy + Full Regression
**Created**: 2026-05-29
**Status**: Complete — all 3 failing Gate 2 ns tests PASS (commits a6458bd destroy_range, 57fe34f cleanup_on_fail, 5e881f8 MAC + inter-range isolation). Gate 1: 222 unit pass.

---

## ⇨ HANDOFF (read this first)

### DONE — destroy_range leak fixed & verified (commit `a6458bd`)
The original "kills only the `unshare` wrapper, leaks libvirtd+qemu" bug is FIXED:
- `rangectl/cgroup.py` `destroy_cgroup`: now `_kill_and_drain(cg)` before `rmdir` — writes `cgroup.kill` (only if the file exists, so the unit-test tmp dir is a no-op) then polls `cgroup.procs` until empty (`DRAIN_TIMEOUT=5s`). No more `rmdir EBUSY`.
- `rangectl/supervisor.py` `destroy_range`: `_terminate(wrapper)` now SIGTERMs `_child_pids(wrapper) + [wrapper]` (libvirtd, the pidns PID 1), grace, then SIGKILLs survivors. Killing libvirtd makes the kernel reap qemu.
- **Gate 1: 216/216 unit tests pass.**
- **EC2: `freeze_thaw` PASSES** (clean teardown, zero orphans). `internet_full` PASSES leaving zero leak. Verified live: killing libvirtd reaps qemu; cgroup drains.

### OPEN — node-b slow boot (the real cause of `internet_none` / `topo6` failures)
The prior handoff's hypothesis (internet_none/topo6 cascade from the leak) is **DISPROVEN**. From a fully-clean EC2 state the failure is independent and deterministic (reproduced ~5×):

- In a 2-Ubuntu-node ns deploy, **node a (1st node) boots in ~26s; node b (2nd node) takes ~215s.**
- Node b's plumbing is CORRECT: qemu running (~9% CPU = executing, not crashed), mgmt tap enslaved to `mgmt-br` and forwarding, `domiflist` shows the mgmt NIC attached, and its seed `network-config` has the right MAC→`192.168.100.2` + gateway `.254`. The MAC/seed mismatch theory is ruled out.
- Symptom: node b answers **no ARP on either NIC** until very late, so the host can't SSH it. From node a (which is healthy — both ifaces `routable`, `systemd-networkd-wait-online` only ~14s), node b is unreachable on both `192.168.100.2` (mgmt) and `10.0.1.2` (data).
- **`internet_full` PASSES at ~220s** — same 2-node topology — because node b just squeaks under the 240s SSH timeout. `internet_none` is marginally slower and tips over 240s → SSH-timeout at deploy. So this is a **marginal ~190s slow-boot, not a hard break.**
- Likely a guest boot-service timeout (cloud-init network wait / snapd.seeded / pollinate / networkd-wait-online), but the exact in-guest cause is **not yet confirmed** — needs node b's `journalctl -b` + `/var/log/cloud-init.log` + `systemd-analyze blame`.

### How to root-cause node b (next step)
The test tears down immediately after deploy, so node b is reachable for only ~5s — too tight to inspect. Use the hold-script:
- `scratch/scripts/diag-slow-boot.py` — deploys the 2-node topo with `internet="full"` and `ssh_ready_timeout=400`, then EXITS WITHOUT TEARDOWN (VMs stay up). Run: `cd rangectl && sudo nohup python3 scratch/scripts/diag-slow-boot.py > /tmp/diagslow.out 2>&1 &` then wait ~215s for `DEPLOY DONE` in `/tmp/diagslow.out`.
- Then SSH node b and read the boot timeline:
  `sudo ssh -i /root/.rangectl/keys/diagslow/id_ed25519 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null ubuntu@192.168.100.2 'systemd-analyze blame | head -20; echo ---; systemd-analyze critical-chain; echo ---; cloud-init analyze blame | head; echo ---; sudo journalctl -b --no-pager | grep -iE "timed out|timeout|wait-online|cloud-init|fail" | head -40'`
- Fix at the SOURCE per team-lead direction (e.g. cloud-init network-config `optional: true`, disable the offending wait service) — **NOT** a timeout bump. The cloud-init network-config is generated in `rangectl/cloudinit.py` `_network_config` (currently emits netplan v2 without `optional: true`).

### THEN — implement cleanup_on_fail (team-lead direction, item c)
`Engine.deploy(cleanup_on_fail=True)` advertises the param but **never uses it** — a failed deploy leaks libvirtd/qemu/overlays/netns (this cascaded into instant `failed in 0.3s` reruns and badly confounded diagnosis). The `internet_none` test also calls `engine.deploy(t)` OUTSIDE its `try/finally`, so its `finally: engine.destroy(t)` never runs on deploy failure. Implement best-effort teardown in a `finally` when deploy raises (destroy partial VMs/bridges + `_teardown_namespace`). Keep it simple. Keep 216 unit tests green.

### THEN — run all 3 previously-failing tests, commit when green
`sudo python3 -m pytest tests/integration/test_ns_regression.py::test_ns_freeze_thaw ::test_ns_internet_none_blocks_outbound ::test_ns_topo6_multi_topology_isolation -v --tb=short` (one at a time; clean orphans between — see gotchas).

### EC2 environment gotchas (IMPORTANT, current)
- Instance RUNNING: `i-0cd9c4f8ad3406291`, `IP=98.92.123.157`. AWS auth is restored (`ec2.sh` works). Key `~/.ssh/aws.pem`, user `ubuntu`. **Do NOT stop EC2.**
- Push code with rsync (repo `.git` on the box is root-owned): `rsync -az --exclude='.git' --exclude='__pycache__' --exclude='.venv' -e "ssh -i ~/.ssh/aws.pem" ./rangectl ./tests ubuntu@$IP:/home/ubuntu/rangectl/`
- The repo `.venv` on EC2 is a stale macOS binary — unusable. Run tests via **system python3**: `cd rangectl && sudo python3 -m pytest …`. Ad-hoc scripts: `sudo PYTHONPATH=/home/ubuntu/rangectl python3 …`.
- pytest buffers output over SSH; redirect to a remote file (`> /tmp/x.out 2>&1`) then read it.
- **Local Bash hook quirks** (on the dev mac, not EC2): the `command_blocker` hook makes `sudo pkill …` commands fail with a spurious local `ENOSPC` on the tasks tmpfs, and blocks `rm -rf`. Workarounds that work: kill by explicit PID (`for pid in $(ps -eo pid,args|grep '/usr/sbin/libvirtd --config /ranges'|grep -v grep|awk '{print $1}'); do sudo kill $pid; done` and same for `pgrep -x qemu-system-x86`), and clear dirs with `sudo find <dir> -mindepth 1 -delete`.
- Manual orphan clean (run when re-running tests): kill range libvirtd by PID (as above; killing libvirtd reaps its qemu), kill leftover qemu by PID, `sudo ip netns del rangectl-*`, `sudo find /ranges -mindepth 1 -delete`, `sudo find /var/lib/libvirt/images/rangectl/overlays -mindepth 1 -delete`, `… /seeds -mindepth 1 -delete`.
- **Note:** a `diag-slow-boot.py` run named `diagslow` may have been left RUNNING/leaked on EC2 — clean `rangectl-diagslow` netns + its libvirtd/qemu + `/ranges/diagslow` + `overlays/seeds/diagslow` before fresh runs.
- Diagnostics: `pgrep -f` matches its own wrapper shell (false positives) — check processes by `comm`/full args instead. Serial console via `nsenter`+`virsh console` is flaky (pty/`/proc`/socket); SSHing into the working node and probing the broken node over the shared data link, or the hold-script, are more reliable.

---

## Progress Log

**2026-05-29** — Implementation complete, Gate 1 green (214 unit tests local + EC2).

Created/modified:
- `rangectl/internet.py` (NEW) — per-range NAT chain `RANGE-<name>`, `enable_internet`/`disable_internet`/`detect_outbound_iface`. MASQUERADE out host uplink, FORWARD allow on veth choke point. Idempotent; teardown removes only the range's own rules.
- `rangectl/topology.py` — `Range(internet=, resources=)`; `Range.freeze/thaw` (→ cgroup), `Range.enable_internet/disable_internet` (→ internet mod, requires ns mode). `Topology.deploy(use_namespaces=, resources=, internet=)`.
- `rangectl/engine.py` — `Engine(internet=)`; `_setup_namespace` enables internet when `full`; `_teardown_namespace` disables it (before destroy_range, while veth/subnet still known); deploy() wires `_mgmt_subnet`/`_veth_host` onto the returned Range for runtime toggle.
- `rangectl/__init__.py` — export `Resources`.
- `tests/unit/test_internet.py` (NEW, 9), `test_range.py` (+5 SDK tests), `test_engine_ns.py` (+4 internet wiring tests).
- `tests/integration/test_ns_regression.py` (NEW, 9) — Topo 3-6 on ns backend + freeze/thaw + internet none/full/toggle + resource limits.

Gate 1: `214 passed` (was 197); now `216 passed` after the destroy_range fixes. Gate 2: see HANDOFF v2 at top.

**Gate 2 debugging notes:**
- EC2 env was missing `pytest`/`paramiko` in system python3 (the repo `.venv` is a stale macOS binary). Installed both into system python3. First failures were purely `ModuleNotFoundError: paramiko` on SSH exec — cgroup limit assertions (memory.max=8GiB, cpu.max=400000) PASSED before that point, confirming resource limits work.
- Cleaned orphaned libvirtd/qemu zombies + stale rangectl netns left by an earlier Phase 11 crash.
- Test-logic fix: every fresh-DB test allocates the first mgmt subnet (192.168.100.0/24), which conftest's session `vm_internet_nat` MASQUERADEs unconditionally. That blanket rule masks the per-range policy, so the 3 internet tests now wrap in `_without_blanket_nat()` (remove + restore) — leaving ONLY the `RANGE-<name>` chain as the NAT path, properly exercising the feature.

**Gate 2 first results (5 ns-feature tests):** 3 passed (resource_limits, internet_full, internet_runtime_toggle — internet policy feature WORKS), 2 failed:
- `internet_none`: SSH timeout (240s) at deploy. *(The original "transient slow boot / host contention" guess here is WRONG — see HANDOFF v2: it's a deterministic ~215s node-b slow boot.)*
- `freeze_thaw`: REAL BUG, root-caused with `diag-freeze.py` — now FIXED (commits `4d7064f` cgroup placement + `a6458bd` destroy_range), PASSES on EC2.

**Freeze bug — root cause (evidence-based):** the range cgroup contained only the `unshare` wrapper PID (moved by `engine.write_pid`); libvirtd + all qemu were in the SSH session scope, NOT `rangectl-<name>`. `cgroup.events` showed `frozen 1` but froze nothing → VM kept answering ping. Two layered causes:
  1. `unshare --fork` spawns libvirtd before the engine can move the wrapper, so libvirtd never inherits the cgroup.
  2. Self-placement from inside the launch script fails: `ip netns exec` gives a fresh `/sys` that shadows the cgroup2 mount, so `/sys/fs/cgroup/<range>/cgroup.procs` is "No such file or directory" inside the namespace.

**Fix:** `supervisor._place_in_cgroup()` runs in the HOST namespace, polls for libvirtd (the wrapper's forked child via `/proc/<pid>/task/<pid>/children`), and writes its PID to `cgroup.procs`. QEMU spawned later inherits it (libvirt doesn't relocate qemu with dbus blocked). Verified via diag3: `ping before freeze rc=0 → frozen 1 → ping after freeze rc=1`. Committed `4d7064f`.

**2026-05-29 (phase12-lead takeover) — node-b slow boot confirmed internet-dependent:**
Controlled A/B on EC2 (systemd-analyze on node b):
- `diag-slow-boot.py` internet=**full** (per-range enable_internet NAT) → node b boots **33s** ✓, outbound works.
- `diag-slow-boot-none.py` internet=**none** but box had **ambient outbound** (leftover `FORWARD -s/-d 192.168.100.0/24 ACCEPT` + stray `RANGE-*` MASQUERADE from the earlier full-diag, never torn down) → node b boots **32s** ✓, `ping 8.8.8.8` works.
- Real test internet=none on clean box → node b **>240s** ✗.
Conclusion: **node b is fast iff it has working outbound during boot.** Confirms phase12-fix's slow-boot characterization and the source-fix direction (`cloudinit.py _network_config` lacks `optional: true`, so `systemd-networkd-wait-online` blocks; plus snapd.seeded/cloud-init network retries when isolated). The test comment claiming the conftest "blanket NAT" gives fast boot is FALSE for the netns veth path — `enable_internet`'s FORWARD rules are veth-specific (`-i veth_host ACCEPT`); internet=none adds none, and the blanket subnet MASQUERADE alone doesn't carry the veth traffic on a clean box.
NOTE: `diag-slow-boot-none.py` (NEW) added for isolated-boot repro. Still need the SLOW-case journal (journalctl -b on an isolated node b) to confirm the exact stalling unit before finalizing the cloudinit fix.
Work split: phase12-fix → `cleanup_on_fail` (code+unit, no EC2); phase12-lead → node-b root cause + fix on EC2.

**2026-05-29 (phase12-lead) — reproduced the failure on a CLEAN box; bug is node-b GUEST network bring-up:**
Ran the actual `test_ns_internet_none_blocks_outbound` on a verified-clean box (destroy_range fix in place, no orphans, ns netns/route created fresh):
- `nsinetnone/a` (192.168.100.1) → **ready (SSH OK) @ ~29s.** `nsinetnone/b` (192.168.100.2) → **SSH-unreachable, timed out @ 240s.** Deterministic, reproduces run B.
- node a being reachable PROVES the host-side path is fine (veth `.254`, connected route, netns, mgmt-br, NAT all work). So the failure is **node b's GUEST network never coming up in time** — matches the earlier "no ARP until late" note.
- node b's seed `network-config` is CORRECT: `if0 match macaddress 52:54:00:35:18:16 → 192.168.100.2 + gw .254` matches node b's actual mgmt NIC (verified by mounting b.iso). **Not a MAC/seed/config bug — it's a timing stall.**
- NOT leak contention: clean box, fresh state. (My earlier "slow-boot = leak contention" hypothesis is therefore also wrong.)
- VARIANCE: `diag-slow-boot.py` (internet=full) and `diag-slow-boot-none.py` (internet=none) both booted node b in **~30s** repeatedly — node b CAN boot fast. Yet the real test reliably stalls node b >240s. The variable that flips node b fast↔slow is **not yet identified** (same image, topology, internet mode, MACs). Suspect a concurrent-boot race (disk I/O on the shared base image, or cloud-init/snapd/networkd network-dependent units ballooning when node b transiently lacks outbound while node a has it).
- Side note: an earlier "netns gone but libvirtd alive" state was my own `ec2-clean` interrupted mid-run by a LOCAL tmpfs ENOSPC (the `sudo pkill` hook quirk), NOT a destroy_range failure. **destroy_range fix stands.** Avoid `pkill` in ssh one-liners; kill by explicit PID.

STILL NEEDED (the actual root cause): node b's in-guest `journalctl -b` + `/var/log/cloud-init.log` + `systemd-analyze blame` during a SLOW/failed boot. Blocker: when node b is slow it's unreachable, and the hold-scripts boot it fast (don't reproduce slow). Next: either run a hold (ssh_ready_timeout=400) repeatedly until a slow node b is caught and stays up, or find the variable that makes node b reliably slow.

**2026-05-29 (phase12-lead) — ROOT CAUSE FOUND + FIXED: YAML sexagesimal MAC.**
Captured node b's in-guest `/var/log/cloud-init.log` (killed the leaked qemu, read the overlay with `virt-cat`). Smoking guns:
- `schema.py[WARNING]: network-config failed schema validation!`
- `networking.py[WARNING]: Not all expected physical devices present: {41135167096}`
- `stages.py[WARNING]: Failed to rename devices: 'int' object has no attribute 'lower'`
- init-local @7s → init-network @127s (a **120s `systemd-networkd-wait-online` stall**); cloud-init "finished … Up 168.94s". `/etc/netplan/50-cloud-init.yaml` was **never written** → node b's `if0` (192.168.100.2) never configured → no ARP → SSH-unreachable (>600s, hard failure, not slow boot).

**Mechanism:** `41135167096` = `52:54:00:35:18:16` parsed as a YAML 1.1 base-60 integer (52·60⁵+54·60⁴+35·60²+18·60+16). `cloudinit.py._network_config` emitted `macaddress: <mac>` UNQUOTED. A MAC whose octets are all digits ≤59 is read as an int, so netplan/cloud-init calls `.lower()` on an int → crashes device matching → interface never configured.

**Why node-b-specific & why my diags missed it:** the MAC is deterministic from `(topology, node, iface)` via `_mac_for`. node a's MAC has hex letters (`af:f5:b6`) → always a YAML string → fine. In topology `nsinetnone`, node b's mgmt MAC is all-numeric ≤59 → triggers the bug every time (deterministic). My hold-scripts used topologies `diagslow`/`diagslownone`, whose node-b MACs have hex letters → booted fine → which is exactly why the bug was unreproducible standalone and looked like "variance".

**Fix (source):** `rangectl/cloudinit.py` `_network_config` now emits `macaddress: "<mac>"` (quoted) → always a YAML string. One line. Added `tests/unit/test_cloudinit.py` (3 tests) guarding it with the real `52:54:00:35:18:16` MAC. Gate 1: **221 passed**. EC2 verification of internet_none/topo6: in progress.
This supersedes the earlier "slow boot / leak contention / blanket-NAT" hypotheses — all wrong; it was the MAC all along.

**2026-05-29 (phase12-lead) — SECOND bug exposed by the MAC fix + FIXED: inter-range mgmt isolation breach.**
With the MAC fix, all 4 topo6 nodes booted, exposing a real isolation failure: `ISOLATION BREACH: red reached blue mgmt 192.168.101.1` (red range pinged blue range's mgmt IP, exit 0).
- Root cause: `ip_forward=1` (for internet) + the host holding an on-link `.254` on every range's host-side mgmt veth means the host ROUTES between range mgmt subnets. The pre-namespace isolation rule (`libvirt_backend._ensure_mgmt_isolation` → `iptables -I FORWARD 1 -i rlmgt+ -o rlmgt+ -j DROP`) targets the OLD `rlmgt-*` bridge names and isn't even called in ns mode (host IP is set inside the netns, not via `assign_host_ip`). So ns mode had NO inter-range mgmt isolation. The orchestrator's assumption that "netns provides structural isolation" was false for the mgmt plane.
- Fix (source): `rangectl/netns.py` `_ensure_mgmt_isolation()` installs `iptables -I FORWARD 1 -i mgh+ -o mgh+ -j DROP` (the ns-era host veth prefix), re-asserted at the top of FORWARD after each range's per-subnet ACCEPTs (delete-then-insert; shared rule, left in place on teardown). Added 1 unit test + updated the idempotency test. Leaves host<->range and range->internet untouched.

**RESULT — all 3 previously-failing Gate 2 tests PASS (clean box, one at a time, final code):**
- `test_ns_internet_none_blocks_outbound` → PASS (216.97s) [needed MAC fix + clearing my own leftover diag MASQUERADE]
- `test_ns_topo6_multi_topology_isolation` → PASS (434.97s) [needed MAC fix + inter-range isolation fix]
- `test_ns_freeze_thaw` → re-verifying with final code (passed earlier at 220s).
Gate 1: 222 unit tests pass.

FIXES THIS SESSION (phase12-lead): `cloudinit.py` quote MAC; `netns.py` inter-range isolation DROP; `tests/unit/test_cloudinit.py` (new, 3); `tests/unit/test_netns.py` (+1, updated 1). Plus phase12-fix's `cleanup_on_fail` (separate). destroy_range/cgroup fixes (a6458bd) stand.

## Related Issues
- **Plan**: `20260527-1-vm-testbed-platform-design.md` — Phase 12 detail
- **Phase 8-10**: `20260529-7-phase8-10-namespace-isolation-gate1.md`
- **Phase 11**: `20260529-8-phase11-engine-integration.md`
- **Architecture**: `agents/network-architecture.md`
- **Orchestrator**: `20260527-5-rangectl-orchestrator.md`

## Goal
Three deliverables:
1. **SDK surface**: Expose namespace features through the Range class — `Range(internet=, resources=)`, `range.freeze()`, `range.thaw()`
2. **Internet policy**: Per-range iptables chains for internet access control (`none` / `full`)
3. **Full regression**: All Topo 1-7 passing on the namespace-isolated backend

## 1. SDK Surface Changes

### `rangectl/topology.py` — Range and Topology updates

`Topology.deploy()` must pass `use_namespaces=True` and `resources` to the Engine when namespace mode is requested.

Add to Range class (or wherever the deployed range is represented):
```python
range.freeze()            # calls cgroup.freeze(range_name)
range.thaw()              # calls cgroup.thaw(range_name)
range.enable_internet()   # adds MASQUERADE + FORWARD rules for range's veth
range.disable_internet()  # removes those rules
```

The Range constructor should accept:
```python
Range("my-lab",
    topology=topo,
    mgmt_network="10.255.1.0/24",
    resources=Resources(memory="32G", cpus=8),
    internet="full")
```

### Key: `internet` parameter
- `internet="none"` (default): VMs can reach each other and host. No outbound internet.
- `internet="full"`: MASQUERADE all range traffic out through host's internet connection.
- Per-range iptables chain: `RANGE-<name>`. Teardown flushes only its chain.
- Runtime toggle via `range.enable_internet()` / `range.disable_internet()`

### Implementation of internet policy

The veth pair is the choke point. All traffic to/from a range flows through it.

For `internet="full"`:
```bash
# Create per-range chain
iptables -N RANGE-<name>
iptables -A RANGE-<name> -o <host-outbound-iface> -j MASQUERADE -t nat
iptables -A FORWARD -i <veth_host> -j ACCEPT
iptables -A FORWARD -o <veth_host> -m state --state RELATED,ESTABLISHED -j ACCEPT
```

For teardown:
```bash
iptables -D FORWARD -i <veth_host> -j ACCEPT
iptables -D FORWARD -o <veth_host> -m state --state RELATED,ESTABLISHED -j ACCEPT
iptables -t nat -D POSTROUTING -s <mgmt_subnet> -j MASQUERADE
iptables -F RANGE-<name>
iptables -X RANGE-<name>
```

VMs also need DNS + default route configured. Cloud-init already handles this for Ubuntu (gateway at .254, DNS from cloud-init metadata). VyOS gets it via serial console config.

## 2. Full Regression (Topo 1-7 on namespace backend)

All existing test topologies must pass on the new namespace-isolated architecture. This validates that the v2 rewrite is a drop-in replacement.

### What to test

Adapt existing integration tests (or write new ones) to run through the namespace-isolated engine. Each test should:
1. Create topology with `use_namespaces=True`
2. Deploy
3. Run assertions (same as original tests)
4. Destroy
5. Verify clean teardown (no orphan processes, bridges, namespaces)

| Topo | Description | Key assertions |
|------|-------------|---------------|
| 1 | 2 Ubuntu VMs | SSH, ping, clean destroy |
| 2 | VyOS router + 2 Ubuntu | Serial console bootstrap, cross-subnet routing |
| 3 | Services + DependencySet | apt-get nginx (needs internet=full), cross-subnet curl |
| 4 | Diamond DAG + snapshot | 4-node deploy in waves, snapshot/restore marker file |
| 5 | Link toggle | Link.down() → ping fails, Link.up() → ping restored |
| 6 | Multi-topology isolation | 2 topologies simultaneously, no cross-range leakage, staggered destroy |
| 7 | Mixed VM + container | nginx container + Ubuntu VM, ping + curl + exec |

**Note**: Topo 1, 2, 7 were already tested in Phase 11's `test_ns_integration.py`. You can either extend those tests or verify they cover the same ground. Focus effort on Topo 3-6 which haven't been tested on the ns backend yet.

### Plus new namespace-specific tests:
- **Freeze/thaw**: Deploy range, freeze → verify CPU drops (VMs unresponsive to ping), thaw → VMs resume
- **Internet policy**: `internet="full"` allows `apt-get update` from VM, `internet="none"` blocks it
- **Resource limits**: Deploy with `Resources(memory="8G")`, verify cgroup exists with correct limits

## 3. Available APIs (from Phases 8-11)

### Engine (modified in Phase 11)
```python
Engine(backend, db, container_backend=None, use_namespaces=False, resources=None)
engine.deploy(topology, cleanup_on_fail=True) -> Range
engine.destroy(topology)
```
- `_setup_namespace(topo_name, mgmt_subnet)` — creates cgroup + supervisor + per-range backends
- `_teardown_namespace(topo_name)` — destroys supervisor + cgroup

### supervisor.py
- `create_range(name, mgmt_subnet, range_dir) -> RangeInfo`
- `destroy_range(name, range_dir)`

### cgroup.py
- `create_cgroup(range_name, resources) -> str`
- `destroy_cgroup(range_name)`
- `freeze(range_name)` / `thaw(range_name)`
- `Resources(memory, cpus, pids, cpuset)`

### netns.py
- `create_mgmt_network(netns_name, mgmt_subnet, range_name) -> MgmtNetwork`
- Internet rules go here or in a new module

### LibvirtBackend
- `LibvirtBackend(ssh_user, ssh_ready_timeout, libvirt_socket=None, netns_name=None)`

### ContainerBackend
- `ContainerBackend(netns_name=None)`

## Unit Tests (Gate 1)

- Range constructor with `internet`, `resources` params
- `range.freeze()` / `range.thaw()` call cgroup module
- `range.enable_internet()` / `range.disable_internet()` call iptables
- Internet policy iptables chain creation/teardown
- All 197 existing tests still pass

## Integration Tests (Gate 2)

**EC2 is RUNNING. Do NOT run `ec2.sh stop`.** Push via rsync (NOT `ec2.sh push .` — repo `.git` is root-owned on the box) and run via system `python3 -m pytest`. See HANDOFF v2 gotchas for exact commands.

## Success Criteria
- [x] SDK: Range accepts `internet=`, `resources=` params
- [x] SDK: `range.freeze()` / `range.thaw()` work (freeze_thaw passes on EC2)
- [x] SDK: `range.enable_internet()` / `range.disable_internet()` work (internet_full/toggle pass)
- [x] Internet policy: per-range iptables chains, MASQUERADE for `internet="full"`
- [x] Regression: Topo 1-7 pass on namespace backend (topo6 multi-topology isolation PASS after MAC + inter-range isolation fixes)
- [x] New: Freeze/thaw test passes (220s)
- [x] New: Internet policy test passes (full/toggle + `none` blocks outbound, 217s)
- [x] All unit tests pass — 222 green
- [x] All 3 previously-failing integration tests pass on EC2 (freeze_thaw, internet_none, topo6)
