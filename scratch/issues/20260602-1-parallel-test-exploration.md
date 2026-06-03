# Exploration: Parallel Test Isolation — What Actually Breaks
**Created**: 2026-06-02
**Status**: Fixes implemented (6,1,4,2,3,5) — Gate 1 green (308); Gate 2 checkpoint C in progress

> **Implementation update (2026-06-03):** the fix plan below was approved and
> built. See **## Implementation Log** at the bottom for what landed, the
> commits, and the Gate-1/Gate-2 results. The analysis sections are unchanged
> (the original empirical findings).

## Related Issues
- **Parent / prior analysis**: `20260601-5-parallel-test-isolation.md` — desk analysis of the two root causes; this issue **empirically confirms** them by running the suite concurrently on EC2 and adds the exact failure signatures + a false-isolation finding the desk analysis missed.
- **Depends on**: `20260601-3-statedb-concurrent-read-flake.md` — StateDB read-lock (already landed: reads now serialize on the shared lock).
- **Plan**: `20260527-1-vm-testbed-platform-design.md` — Phase 24.

## Goal
Run all integration tests concurrently on EC2, catalogue every failure with its
root cause, inventory resource leaks, and propose a lightweight fix plan (no
heavy DB) so that `pytest -n 4 tests/integration/` "just works". Also verify the
**production** multi-range case (two differently-named ranges in parallel).

## Method
- Box: EC2 `c5.metal`-class, **96 cores**, KVM, Ubuntu 22.04, pytest 9.0.3.
- Each integration test file launched as its **own concurrent `pytest` process**
  (true concurrency), `timeout 900` each, then a host leak inventory.
  Harness: `scratch/scripts/iso-concurrent-run.sh`.
- Baseline: `test_topo1.py` alone, serial → **PASS in 38s** (env is healthy).
- xdist path: **unavailable** — EC2's pip index maxes at `pytest-xdist 1.24.1`,
  which doesn't register `-n` under pytest 9 (`error: unrecognized arguments: -n 2`).
  Background-process concurrency is the stand-in and is actually a *stronger*
  test (separate processes = separate StateDBs, exactly the production-like
  per-DB allocation that collides).

## Phase 1 — Inventory (collision surface)

13 test files, all range names **distinct across files** (this matters — see
below). Topo 1–7 call bare `topo.deploy()` which defaults to
`use_namespaces=False` → **legacy/host-bridge mode**; the `ns_*`, `cli`, `sdk`,
`persistent` files pass `use_namespaces=True` → **namespace mode**. So the suite
exercises BOTH deploy modes, and both collide.

| File | Range name(s) | Mode | Nodes |
|---|---|---|---|
| test_topo1 | `topo1` | legacy | 2 VM |
| test_topo2 | `topo2` | legacy | 3 VM |
| test_topo3 | `topo3` | legacy | 3 VM |
| test_topo4 | `topo4` | legacy | 4 VM |
| test_topo5 | `topo5` | legacy | 3 VM |
| test_topo6 | `red-team`, `blue-team` | legacy | 5 VM |
| test_topo7 | `topo7` | legacy | VM + container |
| test_persistent | `persist` | namespace | 2 VM (2 tests) |
| test_cli | `clilab` | namespace | 2 VM |
| test_sdk_range_class | `sdkrange` | namespace | 2 VM |
| test_ns_integration | `nstwo`,`nsvyos`,`nsmix`,`nsA`,`nsB` | namespace | 4 tests |
| test_ns_regression | `nstopo3..5`,`nsfreeze`,`nsinet*`,`nsred`,`nsblue` | namespace | 9 tests |

**Key nuance the desk analysis got slightly wrong:** range-NAME collision is
*not* the dominant failure for `pytest -n` across distinct files, because names
are already unique per file. The dominant, universal failure is **subnet/IP
collision** — it bites every concurrent range regardless of name.

### What IS isolated per range (works) vs NOT (collides)
| Resource | Keyed on | Isolated concurrently? |
|---|---|---|
| netns `rangectl-<name>` | range name (unique/file) | ✅ |
| in-netns bridges (`mgmt-br`,`data-N`) | clean names inside netns | ✅ |
| host veth `mgh<hash>` (ns mode) / host bridge `rlmgt-<topo>` (legacy) | sha1(name)/topo | ✅ distinct *device* … |
| **mgmt subnet `192.168.100.0/24`** | **per-DB pool (empty temp DB)** | ❌ **every range grabs `.100.0`** |
| **host route to mgmt /24 + `.254` gateway** | — | ❌ **N duplicate routes, all `src .254`** |
| **VM mgmt IPs (`.100.1`,`.100.2`…)** | per-DB | ❌ **same IPs reused across ranges** |
| seed/overlay dirs | range name (unique/file) | ✅ (for distinct names) |

## Phase 2/3 — Failure catalogue (concurrent run, 12 files at once)

Per-file result (1 process each, all launched simultaneously):

| File | Result | Wall | Failure category |
|---|---|---|---|
| test_topo1 | **FAIL** | 189s | mgmt-IP/route collision → ssh to wrong VM |
| test_topo2 | **FAIL** | 190s | same |
| test_topo3 | **FAIL** | 190s | same |
| test_topo4 | **PASS** | 185s | won the race this run (flaky) |
| test_topo5 | **FAIL** | 190s | same |
| test_topo6 | **FAIL** | 132s | **false ISOLATION BREACH** (subnet bleed) |
| test_topo7 | **FAIL** | 174s | same ssh collision |
| test_cli | **FAIL** | 190s | same |
| test_sdk_range_class | **FAIL** | 190s | same |
| test_persistent | **1 FAIL / 1 PASS** | 193s | reconnect test hit collision |
| test_ns_integration | **1 FAIL / 3 PASS** | 377s | `nstwo` hit collision; rest won races |
| test_ns_regression | **2 FAIL / 7 PASS** | 798s | 2 tests hit collision; rest won races |

**Total: ~12 / 24 test functions failed; 10 / 13 files had ≥1 failure. Wall clock
798s (gated by ns_regression). topo4 and most ns tests PASS — the failures are
RACE-DEPENDENT (flaky), not deterministic.** Re-running would shift
*which* files pass. That is the signature of an addressing collision resolved by
ARP/route races, not a logic bug.

### Failure mode A — duplicate mgmt IP + duplicate host route (the universal one)
Every range allocates from a fresh temp DB → identical subnet → identical guest
IPs. Logged proof (one run, all concurrent):

```
topo1-ubuntu-a   mgmt_ip=192.168.100.1     clilab-a   mgmt_ip=192.168.100.1
sdkrange-a       mgmt_ip=192.168.100.1     persist-a  mgmt_ip=192.168.100.1
nstwo-a          mgmt_ip=192.168.100.1     topo2-router mgmt_ip=192.168.100.1
topo3-router/topo5-router/topo7-server all = 192.168.100.1
```
Mid-run host snapshot (5 ranges up at the instant):
```
# 12 routes, all to the same /24, all src .254, across different veths:
192.168.100.0/24 dev rlmgt-11879c ... src 192.168.100.254
192.168.100.0/24 dev mgh0a7a647a ... src 192.168.100.254
... (x12)
mgh0a7a647a 192.168.100.254/24
mgh3e74a2ed 192.168.100.254/24   # <-- N interfaces, same gateway IP, same subnet
```
Result — host→VM SSH is non-deterministic:
```
E  RuntimeError: ssh connect to 192.168.100.1 (user=ubuntu) failed:
   Authentication failed.            # reached a DIFFERENT range's .100.1 (different keys)
ERROR paramiko: Error reading SSH protocol banner   # reached a half-booted neighbor
```
The VMs themselves boot fine ("ready"); only the **host's path to them** is
ambiguous because the host has multiple equal-cost connected routes to
`192.168.100.0/24` and multiple VMs answer `.100.1`.

### Failure mode B — FALSE isolation breach (the dangerous one)
`test_topo6` deploys `red-team` + `blue-team` in ONE process / ONE shared DB, so
they correctly get distinct subnets (`.100.0` / `.101.0`) and the test asserts
red CANNOT reach blue's mgmt. It **failed**:
```
E  AssertionError: ISOLATION BREACH: attacker reached siem(192.168.101.1):
E    64 bytes from 192.168.101.1: icmp_seq=1 ttl=63 time=0.276 ms
```
Root cause: a *different* concurrent test file independently allocated
`192.168.101.0/24` from its own DB and put `.254` for it on the host. The host
then has a route into "101" that the inter-range DROP (`-i mgh+ -o mgh+` /
`rlmgt+`) doesn't fully cover across the mixed legacy+ns device names, so red's
ping leaks to *some* `.101.1`. **Concurrency fabricates a security result** —
worst kind of flake because it can both false-positive and (elsewhere)
false-negative an isolation guarantee.

## Phase 4 — Leak inventory (post-run, after all pytest processes exited)

| Resource | Leaked? | Detail |
|---|---|---|
| netns `rangectl-*` | ✅ clean | 0 leaked — netns teardown is robust |
| host veth `mgh/mgp/rlmgt` | ✅ clean | 0 leaked |
| `/ranges/*` dirs | ✅ clean | empty |
| **qemu processes** | ❌ **8 leaked** | all `nstopo4-*` (an ns_regression range) |
| **libvirtd processes** | ❌ **2 leaked** | `nstopo4` pid-ns init + child, holding the 8 qemu |
| **overlay dirs** | ❌ **8 leaked** | `blue-team,red-team,topo1,2,3,4,5,topo7` |
| **seed dirs** | ❌ **8 leaked** | same 8 |

Two distinct leak classes, both important for a parallel runner:

1. **Timeout-kill orphans (process leak).** `ns_regression` was still running
   when its `timeout 900` fired; SIGKILL of the pytest process **skips the
   teardown fixtures**, so the in-flight range (`nstopo4`) leaked its per-range
   libvirtd (PID-ns init) + all 4 QEMU children. A parallel runner that
   time-bounds workers MUST pair it with an external reaper, or these accumulate
   and exhaust RAM. (Reaped cleanly here with a single SIGTERM to the pid-ns
   init — the kernel reaped the QEMU; no SIGKILL needed.)

2. **Legacy-mode disk leak.** Every legacy-mode range (`topo1-7`, incl. the
   PASSING `topo4`) left its overlay+seed dirs behind. `cleanup_vm_storage` is
   only called on the **namespace-mode** teardown path (`engine.destroy` →
   `_use_namespaces`), so legacy teardown never reclaims `OVERLAY_ROOT/<topo>` /
   `SEED_ROOT/<topo>`. This is a pre-existing single-range bug, not specific to
   concurrency, but it compounds under repeated parallel runs (disk fills).

(Cleaned up after the run: SIGTERM the orphan pid-ns init + `find -delete` the
stale overlay/seed dirs. Host returned to qemu=0 libvirtd=0 netns=0 veth=0.)

## Phase 6 — Production multi-range (the real product capability)
`scratch/scripts/iso-multirange-prod.py`: two differently-named ranges
(`iso-a`/`iso-b`), each with identical internal data addressing (`10.0.5.0/24`),
deployed in **parallel threads against ONE shared StateDB** — the production
path. Result:

```
[iso-a] ok=False subnet=None  ERROR: OperationalError: cannot start a transaction within a transaction
[iso-b] ok=True  subnet=192.168.100.0/24  mgmt={a:.100.1, b:.100.2}  ping_rc=0
distinct_subnets=False  both_ok=False
```

**Two findings, and they pull in opposite directions:**

1. ✅ **The subnet allocator is CORRECT under concurrency.** The logs show iso-a
   was handed `192.168.101.0/24` (`[iso-a/b] ready mgmt_ip=192.168.101.2`) while
   iso-b got `192.168.100.0/24` — distinct /24s from the shared pool, exactly as
   designed. The desk analysis's claim that production multi-range "is already
   correct" holds **for the subnet dimension**.

2. ❌ **A real, previously-unflagged StateDB concurrency bug.** iso-a died with
   `OperationalError: cannot start a transaction within a transaction`. Root
   cause is in `state.py`: three write methods execute multi-statement writes
   **without** taking `self._lock`, while every other writer (`save_node`,
   `update_node_state`, `save_topology`, …) does:
   - `delete_topology` — 6 DELETEs + commit, **unlocked** (the one that bit us:
     both threads call it during concurrent `destroy`)
   - `add_image` — INSERT + commit, **unlocked**
   - `remove_image` — DELETE + commit, **unlocked**

   sqlite's default `isolation_level=""` opens an implicit transaction on the
   first write; when thread B's `execute` fires while thread A is mid-implicit-
   transaction on the **shared connection**, sqlite raises the error. The
   `threading.RLock` was meant to serialize all access, but these three methods
   slip past it.

**Implication:** multi-range via *separate processes* (each its own StateDB
connection) is safe; multi-range via *threads in one process* sharing one
StateDB is NOT, until those three methods are locked. The existing `ns_*`
multi-range tests never caught this because they deploy ranges **sequentially**
in a single thread. This is the **write-lock sibling** of the already-tracked
read-lock issue (`20260601-3`).

## Architecture assessment
**What works for concurrency (production, single shared DB):**
- netns per range: bridges/data-plane fully isolated, identical internal
  addressing across ranges is genuinely non-colliding.
- PID-ns libvirtd reap: clean teardown, no per-VM virsh.
- Subnet allocator IS correct **when all ranges share one DB** — `mgmt_subnets`
  PK + first-free scan hands out `.100`,`.101`,… with no overlap.

**What breaks concurrency — entirely test-infra, one root cause:**
- The integration `db` fixture gives every test its **own temp StateDB**. The
  subnet pool lives *in that DB*, so N concurrent tests each see an empty pool
  and all grab `.100.0`. Everything downstream (guest IPs, host route, `.254`
  gateway) then collides on the host, which is the single shared namespace they
  all touch.
- Secondary: legacy-mode (`topo1-7`) host bridges `rlmgt-<topo>` add a second
  device-naming scheme; the inter-range DROP rule is keyed per-scheme, so mixed
  concurrent legacy+ns ranges aren't fully isolated from each other on the host
  (contributes to mode B).
- Independent (surfaced by Phase 6, NOT a test-infra issue): `StateDB`'s
  `delete_topology`/`add_image`/`remove_image` write outside `self._lock`, so
  thread-parallel multi-range from one process corrupts the shared sqlite
  transaction state. Affects the *product*, not just tests.

## Proposed fix plan (lightweight, no PostgreSQL)
Goal: `pytest -n 4 tests/integration/` just works. Ordered by leverage.

**Fix 1 — Host-wide subnet allocator backed by one shared file + flock (CORE).**
The subnet pool is the ONLY truly host-global resource that needs a host-global
source of truth. Don't share the whole StateDB (keeps per-test DBs hermetic);
just move *subnet allocation* to a tiny file-locked registry:
- A fixed path e.g. `/run/rangectl/mgmt_subnets.json` (or a dedicated sqlite
  file `/run/rangectl/subnets.db` in WAL mode).
- `allocate_mgmt_subnet`/`free_mgmt_subnet` take an `flock` on it, read the taken
  set, pick first-free, write back, release. ~30 lines.
- Tests keep their own temp StateDB for everything else.
- **Effort: ~0.5 day.** This alone makes distinct-named concurrent ranges get
  distinct subnets → kills failure modes A and B.

**Fix 2 — Per-worker range-name prefix (defense for same-name reuse).**
Range names are unique across files today, but two *runs* (or future duplicate
parametrization) would collide on netns/veth/seed paths. Add an env hook:
`RANGECTL_RANGE_PREFIX` (xdist sets `worker_id`; harness sets a uuid). Topology
prepends it. Makes netns/veth-hash/seed/overlay paths run-unique.
- **Effort: ~0.5 day.** Not strictly required for `-n 4` over distinct files,
  but cheap insurance and required for "N copies of the whole suite".

**Fix 3 — Per-run seed/overlay roots for integration.**
Mirror the unit-side autouse fixture: point `SEED_ROOT`/`OVERLAY_ROOT` at a
per-worker tmp subdir (env override). Prevents disk-path races if names ever
repeat. **Effort: ~0.25 day.**

**Fix 4 — Unify the inter-range DROP to cover both device schemes.**
Make the isolation DROP match both `mgh+`/`mgp+` and `rlmgt+`, or standardize on
one host-side naming. Closes mode B's residual path. **Effort: ~0.25 day.**

**Fix 5 — Get a working xdist.**
Build a venv with `pytest-xdist>=3` (pip from PyPI, not the stale local index),
or vendor a wheel. Then `pytest -n 4`. **Effort: ~0.25 day.** Optional — the
background-process runner already proves concurrency works once 1–4 land.

**Fix 6 — Lock the three unguarded StateDB writers (PRODUCT bug, do first).**
Wrap `delete_topology`, `add_image`, `remove_image` in `with self._lock:` like
every other writer. ~6 lines. Fixes the `cannot start a transaction within a
transaction` crash for thread-parallel multi-range (Phase 6). Independent of the
test-infra fixes; it's a real product concurrency gap and the cheapest, highest-
certainty change here. Sibling to `20260601-3` (the read-lock that already
landed). **Effort: ~0.25 day.**

**SQLite verdict:** SQLite is fine. The test flake was never SQLite throughput;
it was *semantic* (per-DB subnet pools). The Phase-6 crash was a missing-lock
bug, also not a throughput problem. Fix 1's flock-guarded shared file (or a
single shared WAL sqlite *only for the subnet table*) + Fix 6's lock are the
right weight. No PostgreSQL.

### What a `rangectl test --parallel` / batched runner looks like
A thin wrapper, not new infra:
1. Ensures `/run/rangectl/subnets.db` exists (Fix 1).
2. Sets `RANGECTL_RANGE_PREFIX=$worker` + per-worker `SEED/OVERLAY_ROOT` (Fix 2/3).
3. `pytest -n <N> tests/integration/` (Fix 5) — or, with no xdist, fan out one
   process per file (the harness in this issue) and aggregate exit codes.
4. Pre-flight clean + post-run leak assert (count netns/veth/qemu == 0) so a
   leaking range fails the run loudly instead of silently.

## Effort summary
| Fix | Effort | Unblocks |
|---|---|---|
| 1. flock subnet allocator | 0.5d | **kills modes A & B** |
| 2. per-worker name prefix | 0.5d | whole-suite duplication / safety |
| 3. per-run seed/overlay root | 0.25d | disk-path races |
| 4. unify inter-range DROP | 0.25d | mode B residual |
| 5. working xdist | 0.25d | ergonomic `-n` |
| 6. lock 3 StateDB writers | 0.25d | **product** thread-parallel multi-range |
| **Total** | **~2.0d** | `pytest -n 4` green + safe in-process multi-range |

Recommended order: **6 → 1 → 4 → 2 → 3 → 5** (cheapest/highest-certainty product
fix first, then the test-infra keystone, then hardening).

## Implementation order & test strategy (build-test-repeat)
Every fix lands with its own **Gate 1 unit test** (Gate 1 gates each commit —
MockBackend + SQLite, runs anywhere). Gate 2 (EC2 KVM, ~13 min) is expensive and
several fixes only demonstrate value *together*, so run it at **3 checkpoints**,
not after every fix. Each Gate 2 run also asserts the post-run leak inventory
(`netns == veth == qemu == 0`) so a leaking fix fails loudly.

| # | Fix | Gate 1 unit test | Gate 2? |
|---|---|---|---|
| **6** | lock 3 StateDB writers | 2 threads hammer `delete_topology`/`add_image` on ONE StateDB → assert no `transaction within a transaction`. **Must use a file-backed temp sqlite, not `:memory:`** — the in-memory DB won't reproduce the shared-connection interleave. | no — fully unit-covered |
| **1** | flock subnet allocator | N procs/threads allocate against a temp registry → assert distinct /24s, no double-grab, flock contention serializes | **Checkpoint A** |
| **4** | unify inter-range DROP | unit-test only the rule-builder (emits both `mgh+` and `rlmgt+`); the actual block is iptables | **folds into A** |
| **2** | per-worker name prefix | env → netns / veth-hash / seed-path derivation is unique per prefix | Checkpoint B |
| **3** | per-run seed/overlay roots | env override → path; mirror the existing unit autouse fixture | folds into B |
| **5** | working xdist | (infra — no unit test) | **Checkpoint C** |

**Sequence & why it's also the test order (cheapest/most-unit-testable first):**
1. **Fix 6 — standalone.** Pure Python, instant Gate 1, zero infra; de-risks the
   *product* bug. Commit on green.
2. **Fix 1 — keystone.** Unit-test the allocator in isolation; it's what makes
   *any* concurrent integration possible. Commit on green.
3. **Fix 4** alongside 1 (closes mode B's residual cross-scheme path).
4. **Checkpoint A — first real Gate 2.** Run `scratch/scripts/iso-concurrent-run.sh`
   (per-file concurrency) + `scratch/scripts/iso-multirange-prod.py` (thread
   multi-range). Pass criteria: mode A gone (no ssh-auth failures), mode B gone
   (topo6 isolation correct), zero leaks. This validates 6+1+4.
5. **Fixes 2 + 3.** Unit-test the path/name derivation. Payoff is "N copies of
   the *whole* suite," not just distinct files.
6. **Checkpoint B — Gate 2 under duplicated suites** (run the same files 2–4×
   concurrently) to prove name/seed/overlay hermeticity.
7. **Fix 5** (working `pytest-xdist>=3` via venv).
8. **Checkpoint C — acceptance:** Gate 1 sharded under `-n` first (cheap), then
   `pytest -n 4 tests/integration/` green = done.

Rule of thumb: **unit-test every fix (per-commit Gate 1); spend Gate 2 only at A,
B, C.** A and B reuse the two harness scripts already committed in this issue.

## Resolution
Two independent concurrency problems, both lightweight to fix:

1. **Test-infra (the parallel-suite blocker):** per-test temp StateDBs each
   allocate the same `192.168.100.0/24`, so concurrent ranges collide on guest
   IPs + host routes → mode A (flaky `ssh ... Authentication failed`) and mode B
   (false `ISOLATION BREACH` in topo6). ~12/24 tests failed concurrently, and
   *which* ones is race-dependent. Keystone fix: a host-global, flock-guarded
   subnet allocator (~0.5d); name-prefix + seed-root + DROP-unify + xdist round
   it out.

2. **Product (surfaced by Phase 6):** three StateDB write methods
   (`delete_topology`/`add_image`/`remove_image`) skip `self._lock`, so
   thread-parallel multi-range from one process crashes with `cannot start a
   transaction within a transaction`. The subnet allocator itself is correct
   (hands out distinct `.100`/`.101`). Fix: lock the three methods (~0.25d).

Total ~2.0d, no PostgreSQL. SQLite is adequate; both issues are semantic/locking,
not throughput. Leaks observed: timeout-killed pytest orphans a range's
libvirtd+QEMU (teardown fixture skipped on SIGKILL), and legacy-mode teardown
never reclaims overlay/seed dirs — a parallel runner needs a post-run reaper +
leak assertion. **No code changed in this issue (analysis only); EC2 left
running and cleaned to a zero baseline.**

## Progress Log
- 2026-06-02: Read sources, inventoried 13 files (names unique; topo1-7 = legacy
  mode, rest = namespace mode). Confirmed xdist 1.24.1 broken under pytest 9.
- 2026-06-02: Serial smoke `test_topo1` PASS 38s. Launched 12 concurrent
  per-file pytest processes on the 96-core box.
- 2026-06-02: Captured mid-run proof — 12 host routes to `192.168.100.0/24` all
  `src .254`; duplicate guest IPs across ranges. Failure mode A (`ssh ...
  Authentication failed`) and mode B (`ISOLATION BREACH` in topo6) isolated.
  topo4 + 3/4 ns_integration PASSED → failures are race-dependent/flaky.
- 2026-06-02: Run complete — wall 798s, ~12/24 tests failed. Leak inventory: 8
  qemu + 2 libvirtd orphaned by `ns_regression`'s `timeout`-SIGKILL (`nstopo4`,
  teardown fixture skipped); 8 legacy-mode overlay/seed dirs not reclaimed.
  netns/veth clean. Reaped via SIGTERM to the pid-ns init + `find -delete`.
- 2026-06-02: Phase 6 production multi-range (`iso-a`/`iso-b`, parallel threads,
  shared DB): subnet allocator CORRECT (distinct `.100`/`.101`), but exposed a
  **product** StateDB bug — `delete_topology`/`add_image`/`remove_image` write
  outside `self._lock` → `cannot start a transaction within a transaction`. Added
  Fix 6. Host returned to zero baseline; EC2 left running.

## Implementation Log
TDD, build-test-repeat. Commits land each fix with its Gate-1 unit test.

### What landed (commit → fix)
| Commit | Fix | What |
|---|---|---|
| `e2adf9a` | **6 + 1** | `state.py`: lock `delete_topology`/`add_image`/`remove_image`. New `rangectl/subnet_registry.py` — host-global flock-guarded JSON allocator (`RANGECTL_SUBNET_REGISTRY`, default `~/.rangectl/mgmt_subnets.json`). `StateDB` delegates allocation to it, mirrors result into its table. Unit conftest points it at per-test tmp; integration conftest shares `/run/rangectl`. |
| `f701668` | **4 + 2 + 3** | `networking.mgmt_isolation_rules()` DROPs every ordered pair of `mgh+`/`rlmgt+` (cross-scheme); `netns.py` + `libvirt_backend.py` use it. `RANGECTL_RANGE_PREFIX` prepended to `Topology` names; `RANGECTL_STATE_ROOT` overrides overlay/seed root. Both opt-in, empty default. |
| `c8cb781` | **5** | xdist 3.8.0 (installs fine from PyPI — old "1.24.1 max" no longer holds). `iso-xdist-run.sh`: `-n N --dist loadfile` + **pytest-timeout in-process** (teardown-safe, no orphan leak) + per-worker boot stagger (gwN → N·5s) in integration conftest. |

### Gate 1 (unit) — GREEN
308 unit tests pass. New tests:
- `test_state_concurrency.py::test_concurrent_writers_no_transaction_error`
  (file-backed sqlite — reproduces then fixes the Phase-6 crash).
- `test_subnet_registry.py` — sequential/free/reuse/exhaustion + **16-thread
  contention → all-distinct subnets**.
- `test_networking.py::test_mgmt_isolation_rules_cover_all_prefix_pairs`.
- `test_parallel_isolation_env.py` — prefix + state-root overrides.

### Gate 2 checkpoint A (12 files at once, ~35 VMs) — keystone PROVEN
Run via `iso-concurrent-run.sh` after the fixes:
- **Subnet collision GONE**: registry handed out distinct `/24s` (nstwo=.101,
  topo3=.103, sdkrange=.105, nstopo4=.100); host shows **1 route per /24** (was
  12 routes to `.100` all `src .254`).
- **Mode B GONE**: `test_topo6` (false ISOLATION BREACH) now **passes**; so do
  cli, topo1, topo4, topo5, topo7.
- Remaining A failures (topo2 VyOS routed-ping, sdk 473s ssh, topo3 +
  ns_integration killed at the 900s cap, ns_regression 3/9) are **resource-
  contention timeouts from booting ~35 VMs at once** — not correctness. Proven
  by: VMs reach `ready` with correct distinct mgmt IPs; the slow ones just blow
  the per-node ssh-ready timeout under load.
- **Leak class reconfirmed**: the 900s **shell** `timeout` SIGKILLs pytest →
  skips fixture teardown → orphans that range's libvirtd+QEMU (17 qemu + 5
  libvirtd here). The tests themselves destroy correctly (per-range
  `engine.destroy`); nothing pkills. ⇒ checkpoint C switches to **in-process
  pytest-timeout** (teardown runs) + bounded `-n 4` to avoid both the herd and
  the leak. Manually reaped A's orphans via SIGTERM to the pid-ns inits.

### Gate 2 checkpoint C — second root cause found: legacy-mode data-plane collision
The first `-n 4` attempt still failed several tests + piled up ranges. Diagnosed
to TWO further causes the mgmt-subnet fix didn't cover:

1. **topo1-7 ran in LEGACY mode.** Bare `topo.deploy()` defaulted to
   `use_namespaces=False` → host-level networking with NO data-plane isolation.
   topo1 and topo2 both use `10.0.1.0/24`; run concurrently they collide on the
   shared host stack. (Namespace mode isolates data subnets via netns — proven
   by `test_ns_multi_range_isolation`, two ranges on identical `10.0.9.0/24`.)
   **Fix:** `Topology.deploy` now defaults `use_namespaces=True` (commit
   `4f5eaf4`); legacy is explicit opt-in. All integration topo tests now exercise
   the production namespace path. MockBackend unit tests pass
   `use_namespaces=False` explicitly.
2. **A hardcoded mgmt-subnet assertion.** `test_topo2.py:52` asserted the router
   had `192.168.100.1/24` — but the registry (correctly) hands concurrent ranges
   `.101`, `.102`, … So topo2 passed solo (drew `.100`) but failed whenever
   another range held `.100`. **Fix:** derive the expected IP from
   `rng["router"].mgmt_ip`. Also broadened the conftest NAT MASQUERADE from
   `192.168.100.0/24` to the whole pool (`192.168.0.0/16`).

Verified: `topo1`+`topo2` (both `10.0.1.0/24`) now **pass concurrently** at
`-n 2` after these fixes. `xdist` itself works fine on Ubuntu 22.04 — the `-n 2`
run distributed files and tore down each range cleanly (registry frees logged);
an earlier `INTERNALERROR` was a `/tmp`-rootdir artifact, not an xdist bug.

### Product validation via the CLI (the decisive result)
Rather than fight the pytest harness, validated the actual product capability:
deploy N ranges concurrently (SDK, persistent) then manage them entirely through
the `rangectl` CLI. Scripts: `iso-cli-deploy.py` + `iso-cli-multirange.sh`.

| N ranges (2 VMs each) | deploy (concurrent) | fully working | `destroy --all` | peak load | leaks |
|---|---|---|---|---|---|
| 4 | 62s | **4/4** | 20s | 1.3 | **0** |
| 8 | 82s | **8/8** | 41s | 4.5 | **0** |

All ranges used identical internal addressing (`10.0.5.0/24`) — `exec` hostname +
intra-range ping `2 received` on every one proves netns isolation holds at scale.
Distinct mgmt subnets `.100`–`.107` from the flock registry. Post-run host:
`qemu=0 libvirtd=0 netns=0 veth=0 registry={}` — **zero leaks, every time**.

**Conclusion: concurrent multi-range is a working product capability.** The
`rangectl destroy` path reaps libvirtd+QEMU cleanly. The leaks seen under
`pytest -n 4` were therefore **harness artifacts** — the shell `timeout` (and a
manual mid-run kill) SIGKILLing pytest *skips fixture teardown*, orphaning that
range; plus contention from 4 VM-heavy files booting simultaneously. Neither is a
product/`destroy` bug. A `pytest -n 2` run is clean; `-n 4` needs either heavier
boot staggering or fewer concurrent VM-heavy files.

### Notes / follow-ups
- **Legacy-mode disk leak** (overlay/seed dirs for `topo1-7` not reclaimed):
  pre-existing single-range bug — `cleanup_vm_storage` runs only on the
  namespace teardown path. Not fixed here; candidate for its own issue.
- A real `rangectl test --parallel` should pair in-process timeouts with a
  **post-run reaper + leak assert** (count netns/veth/qemu == 0) as a backstop
  for any worker that dies before teardown.
