# Design: H6 — Link Impairments Not Persisted — Options & Recommendation
**Created**: 2026-06-09
**Status**: In Progress (awaiting user review)

## Related Issues
- **Parent**: `20260609-3-architecture-code-review-findings.md` — H6 (this design closes it)
- **Where the code lives**: `20260603-3-phase19-link-properties.md` (tc netem impairments), `20260529-11-phase14-cli.md` (`rangectl link ... status`), `20260529-10-phase13-persistent-ranges.md` (cross-process `Range.connect` rebuild)
- **Related design**: `20260609-2-phase20-hub-switch-design.md` — its D6 explicitly defers the impair-persistence column to *this* issue and notes the re-apply-on-`Link.up()` interaction
- **Sibling fix**: `20260609-10-h8-link-down-up-container-design.md` (H8) — `Link.up()`'s re-apply path is shared surface

## Problem

### What the code does today
Phase 19 applies WAN impairments (latency/jitter/bandwidth/loss/reorder/corrupt/duplicate) by running `tc netem` on a link's TAP devices. The current state lives **only in memory** on the `Link` object:

- `Link._impairments: dict[str, dict]` (`topology.py:310`) — keyed by endpoint node name, holds the params currently applied per side. Set in `impair()` (`topology.py:396`) and `clear()` (`topology.py:413`).
- `Link.impairments` property (`topology.py:430-437`) reads that in-memory dict.
- The CLI `link ... status` (`cli.py:323-327`) prints `link.impairments`.

The `links` table (`state.py:49-60`) has columns for endpoints, bridge, and `is_up` — but **no impairment column**. Nothing writes the impairment params to the DB, and `Range.connect`'s link rebuild (`topology.py:752-767`) constructs each `Link` fresh, so `_impairments` is reset to the empty default (`Link.__init__`, `topology.py:310`).

### Why it's wrong — the CLI is a separate process
The `rangectl` CLI does **not** share memory with the SDK process that deployed the range. Every CLI invocation calls `Range.connect(name)` (`cli.py:306`), which rebuilds `Link` objects from the DB. Because impairments aren't in the DB:

- `rangectl link a b status` shows **"none"** for every side, even while `tc netem` is actively delaying packets on the TAPs. The kernel has the impairment; the DB doesn't know; the CLI reports a lie.
- `rangectl link a b impair --latency 50ms` (a *different* CLI process) sets `_impairments` on its own short-lived `Link`, applies the tc command, and exits. The in-memory state evaporates. A later `status` again says "none."

There's a second, worse failure: **`Link.up()` silently drops impairments**. When a reconnected link is toggled `down()` then `up()` (e.g. `rangectl` fault-injection, or the SDK after reconnect), `up()` recreates the bridge and calls `_reapply_impairments()` (`topology.py:343, 416-428`), which reads `self._impairments` — empty after reconnect — so it re-applies **nothing**. The link comes back *clean*, silently undoing a WAN condition the user set earlier. The user thinks "50ms latency, link bounced, still 50ms"; reality is "link bounced, latency gone."

### Background a learner needs
`tc qdisc` (queueing discipline) on a network device is **kernel state**, not file state — it lives on the TAP/veth interface and vanishes when the interface is destroyed (which `Link.down()` does by deleting the bridge, orphaning + resetting its TAPs). So there are two candidate "sources of truth" for "what impairment is on this link":
1. **The kernel** (`tc qdisc show dev <tap>`) — authoritative for *right now*, but ephemeral (gone after `down()`), and the TAP name is only resolvable live (`virsh domiflist` by MAC — see `20260609-2` D6).
2. **A persisted record** (the DB) — survives process boundaries and `down()`/`up()`, and is what the CLI in a fresh process can read; but it can drift from the kernel if something changes tc out-of-band.

The design has to pick what `status` reports and what `up()` re-applies from.

---

## D1: Source of truth for impairment state

| Option | Shape | Pros | Cons |
|---|---|---|---|
| **A — Persist impairment JSON in the `links` table (write-through)** ✅ | add an `impairments` TEXT column (JSON: `{node_a: {...}, node_b: {...}}`); `impair()`/`clear()` write it under the StateDB lock; `connect` reads it back into `Link._impairments`; `status` and `_reapply_impairments` read the in-memory dict hydrated from the DB | Survives process boundaries (CLI `status` is correct cross-process); survives `down()/up()` (re-apply has real params to restore); one obvious place to read; cheap | Can drift if someone runs raw `tc` out-of-band (acceptable — rangectl owns the TAPs); needs the H3 `add_link` seam extended (already flagged in H3 D2) |
| B — Read live `tc qdisc show` as the only source | `status` shells `tc qdisc show dev <tap>` in the netns and parses netem params; no DB column | Always matches reality; no write path to keep in sync | Parsing netem output back into our param model is fragile (tc's text format ≠ our kwargs; units, `limit`, `tbf` burst all leak); **TAP doesn't exist after `down()`** so `up()` has nothing to re-apply from → still drops impairments; resolving the TAP requires the VM live |
| C — Both: persist (write-through) AND live-verify on read | DB is the record; `status` optionally cross-checks `tc qdisc show` and flags drift | Most accurate; catches out-of-band tampering | More code; live verify needs the TAP resolvable (VM up); the drift case is rare; over-engineered for v1 |

### ✅ Recommendation: **A** (persist write-through), with **C's live-verify as a deferred follow-up**
The decisive constraint is `Link.up()`: after `down()` deletes the bridge and resets the TAPs, the *only* way to restore the user's impairment is to read it from somewhere that outlives the TAP — i.e. the DB. Live `tc` (B) can't restore what no longer exists on a destroyed interface, and can't be read by a fresh CLI process before the link is back up. So persistence is **required** for correctness, not just for `status`. The DB becomes the record; the kernel is reconciled *from* it (on `up()` and on definition-time apply). Out-of-band `tc` drift is a non-goal (rangectl owns these TAPs); if it ever matters, layer C's `tc qdisc show` cross-check on top without changing the storage model.

---

## D2: Schema shape — one JSON column vs per-side columns vs a new table

| Option | Pros | Cons |
|---|---|---|
| **A — One `impairments` TEXT column on `links`, JSON `{node: params}`** ✅ | Matches the in-memory model 1:1 (`_impairments` is already `dict[str, dict]`); per-side asymmetric impairments (the `outbound=` case) fit naturally; one column, additive migration; serialize/deserialize is `json.dumps`/`loads` | JSON-in-a-column isn't queryable by param (we never query by latency, so irrelevant) |
| B — Columns per param per side (`lat_a`, `lat_b`, `loss_a`, ...) | "Relational" | ~14 columns for 7 params × 2 sides; rigid (adding a netem param = a migration); ugly; no upside since we never filter on them |
| C — A separate `impairments` table (link_id, node, params) | Normalized | A whole table + join for data that's 1:1 with a link and always read whole; over-normalized for a per-link blob |

### ✅ Recommendation: **A** — a single JSON `impairments` column on `links`
It mirrors `Link._impairments` exactly, so persist = `json.dumps(self._impairments)` and hydrate = `json.loads(...)`. Additive `ALTER`/schema bump only. (`is_up` is already a plain column; impairments are richer and per-side, so JSON is the right fit where `is_up` was a bool.)

---

## D3: When does the write happen, and through what API?

`impair()`/`clear()` currently end by calling `self._backend.run_tc(cmds)` (`topology.py:397, 414`) and mutating `self._impairments`. They must also persist.

| Option | Pros | Cons |
|---|---|---|
| **A — `impair()`/`clear()` call a locked `StateDB.set_link_impairments(topology, link_key, json)` after applying tc** ✅ | Persist follows the kernel change (DB reflects what we actually applied); goes through the locked StateDB API (consistent with H3 — no `_conn` poke); `connect` hydrates from it | Needs a stable link identity in the DB (node_a/iface_a/node_b/iface_b or the bridge_name) to target the right row |
| B — Persist inside `run_tc` / the backend | "automatic" | Wrong layer — the backend runs commands, it doesn't know about Links or the StateDB; tangles concerns |
| C — Persist lazily at process exit | fewer writes | A crashing/killed CLI loses the update; defeats cross-process correctness (the whole point) |

### ✅ Recommendation: **A**
Add a locked `StateDB.set_link_impairments(...)` (extends the H3 `add_link` family — same file, same lock discipline) and call it at the end of `impair()` and `clear()`, keyed by the link's endpoints (or `bridge_name`, which is unique per link within a range). `Range.connect`'s link rebuild (`topology.py:752-767`) reads the column and does `link._impairments = json.loads(row["impairments"] or "{}")`, so the reconnected link's `status`, `up()` re-apply, and further `impair()` all start from the true state.

---

## Interaction notes (called out by the Phase 20 design, `20260609-2` D6)
- **`Link.up()` re-apply**: once `_impairments` is hydrated from the DB on `connect`, `_reapply_impairments()` (`topology.py:416-428`) restores the real params after a bounce — H6's fix is what makes that path correct.
- **Veth endpoints (Phase 20 L2↔L2 links)**: the persisted JSON is keyed by node name and stores params only — it's agnostic to whether the targeted device is a TAP or a veth, so it composes with the Phase 20 endpoint generalization without change.
- **`add_link` seam**: H3 introduces `StateDB.add_link`; this design adds the `impairments` column it should default to `NULL`/`'{}'`, and a sibling `set_link_impairments` updater. Coordinate so both land coherently.

## Recommended shape (summary)
1. `state.py`: add `impairments TEXT` to the `links` table (default `'{}'`); add locked `set_link_impairments(topology_name, bridge_name, impairments_json)` and include the column in `list_links`.
2. `topology.py` `impair()`/`clear()`: after `run_tc`, persist `json.dumps(self._impairments)` via the new StateDB method.
3. `topology.py` `Range.connect` link rebuild (`:752-767`): hydrate `link._impairments = json.loads(row.get("impairments") or "{}")`.
4. No change to the `impairments` property or `_reapply_impairments` logic — they already read `_impairments`; they just finally have real data cross-process.

## Test strategy
**Gate 1 (unit, MockBackend + in-memory SQLite):**
- `impair(latency="50ms")` then read the DB row → `impairments` JSON contains the params for the right side.
- Round-trip: deploy → impair → **new** `Range.connect` (fresh objects, same in-memory DB) → `link.impairments` returns the persisted params (the cross-process `status` bug, reproduced and fixed).
- `down()` then `up()` on a reconnected link → `_reapply_impairments` re-issues the netem cmds (assert via MockBackend `run_tc` recording) — the silent-drop bug, fixed.
- `clear()` → DB row updated to empty; reconnect shows none.
- asymmetric `impair(outbound=node_a, ...)` → only node_a's side persisted/restored.

**Gate 2 (EC2):** Recommended. After `20260603-3`'s real netem path: SDK-deploy a 2-node link, `impair --latency 50ms`, then **from a separate `rangectl` process** run `link status` and assert it reports 50ms (not "none"); bounce the link (`down`/`up`) and assert ping RTT still shows ~50ms (proves re-apply restored real kernel state, not just the DB record).

## Unresolved questions
- Key the DB update by `bridge_name` (unique per link in a range) or by the `(node_a, iface_a, node_b, iface_b)` tuple? Recommend `bridge_name` — it's already the link's stable handle in `_wire_link`/`connect`. Confirm L2↔L2 veth links (Phase 20) also get a stable `bridge_name`-equivalent key, or switch to the endpoint tuple if not.

## Progress Log
- 2026-06-09: Read `topology.py` `Link` (`_impairments`, `impair`/`clear`/`impairments`/`_reapply_impairments`, `connect` rebuild `:752-767`), `cli.py` `cmd_link` (`:302-328`), `state.py` `links` schema + `list_links`. Confirmed no impair column and that `connect` resets `_impairments` to empty → `status` shows "none" cross-process and `up()` drops impairments. Wrote options; reconciled with `20260609-2` D6 and H3's `add_link`.

## Resolution
_(pending user review)_
