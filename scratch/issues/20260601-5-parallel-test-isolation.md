# Phase 24: Parallel Test Isolation
**Created**: 2026-06-01
**Status**: Scheduled — Phase 24 (low priority; depends on 20260601-3)

## Related Issues
- **Plan**: `20260527-1-vm-testbed-platform-design.md` — Phase 24
- **Depends on**: `20260601-3-statedb-concurrent-read-flake.md` — land the StateDB read-lock first (parallel deploys hammer the shared connection)
- **Phase 14**: `20260529-11-phase14-cli.md` — surfaced while running the suite in parallel on the 96-core EC2 box
- **Empirical confirmation**: `20260602-1-parallel-test-exploration.md` — ran the suite concurrently on EC2; confirmed the subnet collision (modes A/ssh-auth + B/false-isolation-breach), found leaks + a product StateDB write-lock bug, and a ~2.0d fix plan.

## Goal
Make the test suite runnable in parallel (xdist / concurrent processes) with no
netns/veth/subnet/disk collisions, so Gate 1+2 wall-clock drops on the 96-core
box. Production multi-range is already correct — this is test-infra only.

## Steps (TDD)
- [ ] Prefix range names per run/worker (xdist `worker_id` or uuid) so
      `rangectl-<name>` / `mgh<hash>` / seed+overlay paths never collide.
- [ ] Host-wide subnet allocator: one shared StateDB for concurrent ranges, OR a
      file-locked host subnet registry, so independent test DBs don't both grab
      `192.168.100.0/24`.
- [ ] Per-run seed/overlay roots for integration (unit side already done via the
      `_isolate_state_roots` autouse conftest fixture).
- [ ] Get a working `pytest-xdist` on EC2 (venv or working index — system index
      maxes at 1.24.1, broken under pytest 9).
- [ ] Gate 1: N concurrent full-suite copies stay green.
- [ ] Gate 2: Topo 1-7 + persistent + cli run concurrently via xdist on EC2 with
      zero collisions.

## Context
User asked to run the regression suite in parallel. Unit tests sharded one-per-
process: 23/23 green. But 60 *identical* concurrent full-suite copies → 29/60
failed, and parallel INTEGRATION is unsafe. This documents WHY, so a future
"parallelize the test suite / run N ranges concurrently per host" effort starts
from facts.

## What IS isolated per range today (works in production)
| Resource | Scope | Keyed on | Where |
|---|---|---|---|
| Network namespace `rangectl-<name>` | per-range | range **name** | supervisor.py:160 |
| Bridges (`mgmt-br`, `data-N`) | inside the netns | — (clean names) | netns.py:24,105,143 |
| libvirtd + PID/mount/UTS ns | per-range unshare | range dir/socket | supervisor.py:169 |
| Host-side veth `mgh<hash>` | host ns | sha1(range name) | netns.py:47-54 |
| mgmt subnet `192.168.100–199.0/24` | host | **StateDB pool** | state.py:129 |
| seed/overlay dirs | host disk | range **name** | engine SEED_ROOT/OVERLAY_ROOT |

**Production (single shared `~/.rangectl/rangectl.db`): no overlap.**
`allocate_mgmt_subnet` records taken subnets in the `mgmt_subnets` table and
hands out the first free /24 (up to 100 ranges); `topologies.name` is the PK so
names are unique. Multi-range on one host is correct.

## Why PARALLEL execution collides — two root causes
1. **Subnet allocation is per-DB, not per-host.** Integration conftest gives
   each test its own temp StateDB (`tmp_path/state.db`). Each fresh DB's
   `mgmt_subnets` is empty, so every test's first range allocates the SAME
   `192.168.100.0/24`. The host then carries two `mgh<hash>` veths both holding
   the `.254` gateway on the same subnet → route/ARP collision. (Host carries
   `.254` on every range's veth — see `_ensure_mgmt_isolation`, netns.py:77.)
2. **Isolation keys on the range NAME, and names are fixed/reused.** netns, veth
   hash, and seed/overlay disk paths all derive from the range name. Integration
   tests reuse fixed names (`persist`, `sdkrange`, `topo1..7`, `nsr`), so two
   concurrent runs collide on netns/veth/disk regardless of subnet. (This is the
   `FileExistsError: .../seeds/nsr` seen under 60 concurrent unit copies — note
   the unit-test slice of that, SEED_ROOT/OVERLAY_ROOT hermeticity, is being
   fixed separately by perf-analyst via a conftest autouse tmp-root fixture.)

## What a fix would need (NOT scheduled)
- **Unique per-run range names**: prefix range names with a run/worker id (e.g.
  `pytest-xdist` `worker_id`, or a PID/uuid) so netns/veth/disk never collide.
- **Host-wide subnet source of truth**: either a single shared StateDB for all
  concurrent ranges, or a host-level lock/allocator (file lock on a shared
  subnet registry) so independent DBs don't both grab `.100.0`.
- **Per-run seed/overlay roots** for integration too (unit side handled by the
  conftest fixture above).
- Then integration could run with `pytest-xdist` (note: EC2's pip index maxes at
  xdist 1.24.1, broken under pytest 9 — would need a working index or a venv).

## Decision
Scheduled as **Phase 24** in the plan. Low priority — production multi-range is
correct; this matters solely for parallelizing the *test suite*. Sequenced after
`20260601-3` (the real product concurrency bug). Pick up when test-suite
wall-clock becomes a bottleneck.
