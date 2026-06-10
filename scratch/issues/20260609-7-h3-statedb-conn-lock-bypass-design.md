# Design: H3 — `_db._conn` Lock Bypass — Options & Recommendation
**Created**: 2026-06-09
**Status**: In Progress (awaiting user review)

## Related Issues
- **Parent**: `20260609-3-architecture-code-review-findings.md` — H3 (this design closes it)
- **Prior fix**: `20260601-3-statedb-concurrent-read-flake.md` — the original unlocked-read flake under parallel deploy (the write-lock gap was closed in `state.py`; this is the same disease one layer up, at the *call sites*)
- **Where the leak is**: `20260527-8-phase3-state-machine-dag.md` (StateDB + engine), `20260601-4-parallel-boot-decouple-from-dag.md` (parallel boot is what makes the race reachable), `20260601-5-parallel-test-isolation.md` (multi-range concurrency)
- **Memory**: `project_statedb-write-lock-gap.md`, `project_statedb-concurrent-read-flake.md`

## Problem

### What the code does today
`StateDB` (`state.py`) is explicitly designed to be shared across threads: it opens sqlite with `check_same_thread=False` and guards **every** access with an `RLock` (`state.py:118-121`):

> "check_same_thread=False so wave-parallel deploys can write from worker threads. The lock below serializes access — sqlite itself isn't safe to share without it."

Every public method on `StateDB` takes `with self._lock:` before touching `self._conn`. But three call sites reach **past** the public API and poke `self._conn` directly, with no lock held:

1. **Bridge inserts** — `engine.py:336-340` (inside Step 4, `_deploy_impl`):
   ```python
   self._db._conn.execute("INSERT INTO bridges ...", (...))
   self._db._conn.commit()
   ```
2. **Link inserts** — `engine.py:691-697` (inside `_wire_link`):
   ```python
   self._db._conn.execute("INSERT INTO links ...", (...))
   self._db._conn.commit()
   ```
3. **Snapshot-id read** — `topology.py:1087-1094` (`LiveNode.restore`):
   ```python
   cur = self._db._conn.execute("SELECT snapshot_id FROM snapshots WHERE ...")
   row = cur.fetchone()
   ```
   (Note: the *write* side, `LiveNode.snapshot` at `topology.py:1071-1077`, **does** take `with self._db._lock:` — so the inconsistency is visible within the same class.)

### Why it's wrong — sqlite + threads + lock discipline
A single sqlite `Connection` shared across threads is **not safe** without external serialization. Two threads calling `execute`/`commit` on the same connection concurrently can interleave at the C level: cursors can be stepped by another thread mid-iteration, a `commit` from thread B can land between thread A's `execute` and its `fetchone`, and under WAL you can get `database is locked` or `SQLite objects created in a thread can only be used in that same thread`-class corruption. That's exactly why `StateDB` centralizes all access behind one `RLock`. The moment a caller bypasses that lock (`_db._conn.execute(...)`), it races with every *locked* writer running on a sibling deploy thread.

`_wire_link` (case 2) runs during Step 8. Today links are wired in a single loop (`engine.py:368-374`), but boot is already parallel (`engine.py:363-366`, `_deploy_node` writes nodes from worker threads via the *locked* `save_node`). A boot thread committing `save_node` while the main thread commits an unlocked bridge/link INSERT is the live race. As deploy parallelism grows (the stated direction — `20260601-4`), `_wire_link` itself is a candidate to parallelize, at which point the unlocked link INSERT races *itself*.

This is the **same bug** the team already fixed once: `20260601-3` and the write-lock-gap memory record closing unlocked StateDB access inside `state.py`. The gap simply **moved up** to the three external pokes. It also breaks the abstraction boundary — `engine.py`/`topology.py` reaching into `_db._conn` means the StateDB's locking invariant can't be reasoned about by reading `state.py` alone.

### Blast radius (a real failure)
Two ranges deploying concurrently (or one range with parallel boot writing nodes while the main thread writes bridges/links) intermittently throws `sqlite3.OperationalError: database is locked` or a cursor error mid-deploy → a spuriously failed deploy that "passes on retry." Non-deterministic, hard to repro, and a hard blocker on parallelizing deploy further (the findings list this as "prerequisite for parallel deploy").

---

## D1: How to close the bypass

| Option | Shape | Pros | Cons |
|---|---|---|---|
| **A — Add locked public methods on StateDB; delete the `_conn` pokes** ✅ | `add_bridge(topology_name, name, bridge_type)`, `add_link(...8 cols...)`, `get_snapshot_id(topology_name, node_name, snapshot_name)` — each `with self._lock:` like every sibling method; engine/topology call those | Restores the single-lock invariant; locking is *uniform and auditable in `state.py`*; matches the existing method style exactly; removes the abstraction leak; testable in isolation | Three small methods + three call-site edits |
| B — Expose a context-managed transaction (`with db.transaction() as cur:`) | a public method yielding a locked cursor | Lets callers run arbitrary SQL safely; fewer named methods | Leaks SQL strings back into engine/topology (the boundary problem persists, just lock-safe); invites more ad-hoc queries; bigger concept than the 3 fixed shapes need |
| C — "Just take `self._db._lock` in the callers" | wrap the existing `_conn` pokes in `with self._db._lock:` | Smallest diff; no new methods | **Keeps the leak**: engine/topology still own SQL strings and reach into two privates (`_lock` *and* `_conn`); the locking invariant is now spread across three files; this is the write-lock-gap memory's anti-pattern re-applied. Bad. |
| D — Give each thread its own connection | per-thread `sqlite3.connect` | No shared-conn race at all | Throws away the WAL single-writer model; cross-thread visibility/commit semantics get subtle; far bigger change than the bug warrants; contradicts the deliberate `check_same_thread=False`+lock design |

### ✅ Recommendation: **A**
Three locked methods named like their siblings (`save_node`, `update_node_state`, `log_event` are all `with self._lock:` + execute + commit) is the minimal change that *restores* the design rather than patching around it. The bridge and link INSERTs become `add_bridge`/`add_link`; the snapshot read becomes `get_snapshot_id`. After this, **all** sqlite access is inside `state.py` under the lock — you can verify the invariant by reading one file. C is explicitly rejected because it perpetuates exactly the leak the prior fix and the write-lock-gap memory warned against. B is a reasonable future tool but over-scoped for three fixed query shapes.

---

## D2: Should `add_link` also own the `is_up` default / future impair column?

The `links` table (`state.py:49-60`) has `is_up BOOLEAN DEFAULT 1` and (per H6) will gain an impairment column. The current INSERT (`engine.py:691-696`) lists 8 columns and omits `is_up` (relying on the DEFAULT).

| Option | Pros | Cons |
|---|---|---|
| **A — `add_link` inserts the 8 current columns, lets `is_up` default** ✅ | Smallest correct change; preserves today's behavior; H6's impair column lands as an additive param later | — |
| B — `add_link` also takes `is_up`/impair now | "Future-proof" | Speculative params for columns not yet wired; premature (H6 is a separate design) |

### ✅ Recommendation: **A** — keep `add_link`'s signature to today's 8 columns; H6 extends it when that lands. (Noted so the H6 author knows `add_link` is the seam to extend, not `_conn`.)

---

## Recommended shape (summary)
1. `state.py`: add three locked methods —
   - `add_bridge(self, topology_name, name, bridge_type)` → locked INSERT into `bridges`.
   - `add_link(self, topology_name, node_a, iface_a, ip_a, node_b, iface_b, ip_b, bridge_name)` → locked INSERT into `links`.
   - `get_snapshot_id(self, topology_name, node_name, snapshot_name) -> str | None` → locked SELECT ... ORDER BY id DESC LIMIT 1, return the id or None.
2. `engine.py:336-340` → loop calling `self._db.add_bridge(...)` (drop the manual `commit`).
3. `engine.py:691-697` → `self._db.add_link(...)`.
4. `topology.py:1087-1094` → `snap_id = self._db.get_snapshot_id(...)`.
5. Optionally fold `LiveNode.snapshot`'s already-locked INSERT (`topology.py:1071-1077`) into a matching `add_snapshot(...)` for symmetry — recommended for consistency, low cost.
6. Grep for any remaining `._conn` / `._db._conn` references outside `state.py` to confirm the boundary is sealed.

## Test strategy
**Gate 1 (unit):**
- Direct: `add_bridge`/`add_link`/`get_snapshot_id` round-trip against `StateDB(":memory:")`; `get_snapshot_id` returns newest on duplicate names and `None` when absent.
- Concurrency regression: spawn N threads each deploying a small topology against shared StateDB instances (mirroring `20260601-3`'s repro) and assert no `OperationalError`/cursor errors — this is the test that would have caught the bypass.
- Boundary guard: a test (or a grep-based check) asserting no `_conn` access exists outside `state.py`.

**Gate 2 (EC2):** Not required — this is pure StateDB/threading correctness with no libvirt/network/SSH surface. Gate 1 (in-memory sqlite + threads) reproduces the race faithfully. The existing multi-range integration tests provide incidental coverage.

## Unresolved questions
- None. (H6 will extend `add_link`; flagged in D2.)

## Progress Log
- 2026-06-09: Read `state.py` (lock discipline, every method `with self._lock:`), the three bypass sites (`engine.py:336-340`, `engine.py:691-697`, `topology.py:1087-1094`), and confirmed `LiveNode.snapshot` already locks while `LiveNode.restore` doesn't. Cross-checked against the `statedb-write-lock-gap` memory. Wrote options.

## Resolution
_(pending user review)_
