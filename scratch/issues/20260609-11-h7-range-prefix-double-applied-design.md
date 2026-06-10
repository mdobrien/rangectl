# Design: H7 — `Range.connect()` Double-Applies `RANGECTL_RANGE_PREFIX` — Options & Recommendation
**Created**: 2026-06-09
**Status**: In Progress (awaiting user review)

## Related Issues
- **Parent**: `20260609-3-architecture-code-review-findings.md` — H7 (this design closes it)
- **Where the code lives**: `20260529-10-phase13-persistent-ranges.md` (`Range.connect` reconnect), `20260601-5-parallel-test-isolation.md` (the prefix exists to make concurrent same-named ranges host-unique)
- **Memory**: `project_shared-checkout-coordination.md` (agents share one tree; parallel test isolation is why the prefix exists)

## Problem

### What the code does today
`RANGECTL_RANGE_PREFIX` makes two concurrent runs of the *same* range name not collide (xdist worker id / uuid). It's applied **once, in `Topology.__init__`** (`topology.py:68-72`):

```python
# topology.py:72
self.name = f'{os.environ.get("RANGECTL_RANGE_PREFIX", "")}{name}'
```

So `Topology("foo")` under `RANGECTL_RANGE_PREFIX=w0-` becomes a topology named `w0-foo`. Deploy persists everything under `w0-foo`: the StateDB `topologies.name`, the netns `rangectl-w0-foo`, `/ranges/w0-foo/range.json`, overlays, etc. Good.

Now reconnect. `Range.connect(name, ...)` (`topology.py:650-771`) receives the **already-resolved** name — the CLI passes `args.range`, which the user reads from `rangectl list` (i.e. `w0-foo`). But connect builds a fresh Topology from it (`topology.py:697`):

```python
# topology.py:697
topology = Topology(name, backend=lvb, db=db, container_backend=cb)
```

If `RANGECTL_RANGE_PREFIX` is **still set in the connecting process** (it is, inside the same xdist worker), `Topology.__init__` prefixes **again** → `topology.name == "w0-w0-foo"`.

### The split-brain that results
Everything else in `connect` keys off the **param** `name` (`"w0-foo"`), not `topology.name` (`"w0-w0-foo"`):
- `db.get_topology(name)`, `_read_range_json(name, rdir)`, `db.list_nodes(name)` — all use `name` → find the real range.
- `engine._range_info[name]`, `engine._vm_ids[(name, node)]`, `engine._mgmt_ips[(name, node)]` — all keyed on `name`.

But the `Range`/`Topology` returned wraps `topology.name == "w0-w0-foo"`. Then `Range.destroy()` → `engine.destroy(self.topology)` (`topology.py:644`) iterates `topology._nodes` and looks up teardown state by **`topology.name`**:
```python
# engine.py:798-800
vm_id = self._vm_ids.get((topology.name, node.name))   # ("w0-w0-foo", node) — NOT in the dict
if vm_id is None:
    continue                                            # silently skips teardown of every node
```
and `_teardown_namespace(topology.name)` → `supervisor.destroy_range("w0-w0-foo")` → no `/ranges/w0-w0-foo/range.json` → "nothing to do" no-op (`supervisor.py:240-242`). So **destroy via a reconnected range tears down nothing**: VMs/containers/netns all leak; the DB rows for `w0-foo` are never cleared (and `free_mgmt_subnet`/`delete_topology` run against `topology.name == "w0-w0-foo"`, hitting nothing).

### Blast radius (a real failure)
Under parallel test isolation (the prefix's entire reason to exist), any test that does `Range.connect(...)` then `.destroy()` — or the CLI `rangectl destroy` invoked with the prefix env set — **leaks the whole range**: qemu/containers keep running, the netns and `/ranges/<name>` dir persist, the mgmt subnet stays allocated, and the DB shows the range as still alive. Containers are the worst case (they're Docker processes not reaped by any namespace kill, and the per-node `docker rm -f` loop is exactly what gets skipped). Net effect: the isolation mechanism that's supposed to make parallel runs *safe* causes them to *leak resources* on teardown.

### Background a learner needs
A name like `RANGECTL_RANGE_PREFIX` is a **namespacing transform** applied at the boundary where a *logical* name (what the user/test writes: `"foo"`) becomes a *physical, host-unique* name (`"w0-foo"`). The cardinal rule of such a transform: **apply it exactly once, at the point of creation, then store and reuse the resolved name** — never re-apply it to a value that's already resolved. `Topology.__init__` is the creation boundary and applies it correctly. `Range.connect` is a *rehydration* boundary: its input is already the resolved physical name (it came out of the DB / `range.json`), so re-running the transform is a category error.

---

## D1: Where should prefixing happen, so it happens exactly once?

| Option | Shape | Pros | Cons |
|---|---|---|---|
| **A — `connect` builds the Topology without re-prefixing (store the full resolved name, never re-transform)** ✅ | give `Topology` a way to take an already-resolved name verbatim — e.g. an internal `_resolved=True` flag / classmethod `Topology._from_resolved(name, ...)` that sets `self.name = name` with no env lookup; `connect` uses it | Fixes the bug at the conceptual root: the transform runs once at creation, rehydration uses the stored name as-is; no env-dependence in a reconnect path; symmetrical with how `name` already drives all of `connect`'s lookups | One new internal constructor path on Topology |
| B — Strip the prefix in `connect` before constructing | `connect` does `base = name.removeprefix(os.environ.get("RANGECTL_RANGE_PREFIX",""))` then `Topology(base)` re-adds it | No Topology change | Fragile and order-dependent: only correct if the *same* prefix is set now as at deploy; if the prefix changed or is unset now, it mangles the name; double-transform-then-untransform is brittle reasoning; still env-dependent in a path that shouldn't be |
| C — Make `connect` keep using `name` everywhere AND set `topology.name = name` after construction | construct Topology (double-prefixed) then overwrite `.name = name` | Tiny diff | Leaves a Topology that briefly held the wrong name (and derived nothing from it yet, so maybe ok) — but it's a patch over the symptom; any future code reading `topology.name` between construct and overwrite is wrong; smells |
| D — Remove env-var prefixing entirely; pass an explicit `prefix=`/unique name through the API | callers (tests) pass uniqueness explicitly | No hidden global; most honest | Bigger change touching every deploy entry point and the test harness; out of scope for a targeted bug fix (worth noting as the "where does prefixing belong at all?" north star) |

### ✅ Recommendation: **A**
The clean fix is to make the **rehydration path consume the stored name verbatim**. `Topology.__init__`'s env-var prefixing is correct for *new* topologies (the creation boundary); `connect` is reconstructing an *existing* one and must not re-run the transform. Add an internal constructor (`Topology._from_resolved(name, ...)` or an `_apply_prefix=False` kwarg) that sets `self.name = name` with no env lookup, and have `connect` use it. This makes `topology.name == name == "w0-foo"`, so `engine.destroy`'s `_vm_ids[(topology.name, node)]` lookups and `_teardown_namespace(topology.name)` all hit the real keys — teardown works, nothing leaks.

D is the principled long-term direction (explicit uniqueness beats a hidden global env var), and is worth recording as the eventual design — but it's a broader refactor than closing H7 warrants now.

---

## D2: Should other rehydration paths get the same treatment?

`Range.list` (`topology.py:773-796`) and `Range.cleanup` (`topology.py:798-829`) already take resolved names and key off them directly — `list` never constructs a Topology, and `cleanup` calls `supervisor.destroy_range(name)`/`db.delete_topology(name)` with the param, so they're **not** affected. The only place that rebuilds a `Topology` from a stored name is `connect`. `Topology.from_yaml` (`topology.py:194-234`) constructs `cls(data["name"])` — but a YAML export is a *logical* definition a user re-deploys, so prefixing it (once) is arguably correct there. 

- **Recommendation**: fix `connect` only. Audit `from_yaml` separately if round-tripping a deployed-then-exported range surfaces a related issue — but its input is a user-authored topology name, so single-prefixing is defensible. Note it; don't change it here.

---

## Recommended shape (summary)
1. `Topology`: add an internal no-prefix construction path (e.g. `@classmethod _from_resolved(cls, name, *, backend, db, container_backend)` that sets `self.name = name` directly, or an `apply_prefix: bool = True` param on `__init__`).
2. `Range.connect` (`topology.py:697`): build the rebuilt topology via that path so `topology.name == name` (the already-resolved DB name).
3. Verify every `topology.name` use inside the reconnected-range lifecycle (`engine.destroy`, `_teardown_namespace`, `free_mgmt_subnet`, `delete_topology`) now matches the engine bookkeeping keyed on `name`.
4. Leave `Topology.__init__`'s env prefixing for the *creation* path untouched.

## Test strategy
**Gate 1 (unit, MockBackend + in-memory SQLite):**
- With `RANGECTL_RANGE_PREFIX=w0-` set: deploy `Topology("foo")` → stored name is `w0-foo`. Then `Range.connect("w0-foo")` → assert `rng.topology.name == "w0-foo"` (not `w0-w0-foo`) — the core regression.
- Reconnect-then-destroy with the prefix set → assert teardown actually targets the nodes: `engine._vm_ids` lookups hit (mock backend `destroy`/`docker rm` calls recorded for each node), `delete_topology("w0-foo")` clears the row, subnet freed. (Today this silently no-ops.)
- Prefix unset at connect time but range deployed without prefix → still round-trips (guard against B-style env-coupling regressions).
- Container node in the range → its `destroy` (docker rm) is actually invoked on reconnect-destroy (the worst-leak case).

**Gate 2 (EC2):** Recommended (light), since the leak is a real-resource leak the unit test can only assert via mock calls. With `RANGECTL_RANGE_PREFIX` set: SDK-deploy a small range, `Range.connect` from a second process, `.destroy()`, then assert no leftover netns (`rangectl-w0-foo`), no `/ranges/w0-foo`, no qemu/containers, and the mgmt subnet is freed. This is the proof that parallel-isolation teardown no longer leaks. Run scoped per the cleanup memory.

## Unresolved questions
- Prefer a classmethod (`_from_resolved`) or an `apply_prefix=False` kwarg on `__init__`? (Recommend the kwarg — smaller surface, and `from_yaml` could opt in later if needed.)
- Longer term (D): retire the env-var prefix for an explicit uniqueness arg threaded from the test harness? Out of scope here; flagging the direction.

## Progress Log
- 2026-06-09: Read `topology.py` `Topology.__init__` prefixing (`:68-72`), `Range.connect` (`:650-771`, esp. `Topology(name,...)` at `:697` and the `name`-keyed engine bookkeeping `:707-746`), `Range.destroy`/`engine.destroy` (`topology.py:644`, `engine.py:794-830`) confirming `topology.name`-keyed lookups. Traced the double-prefix → key-mismatch → silent teardown skip. Wrote options.

## Resolution
_(pending user review)_
