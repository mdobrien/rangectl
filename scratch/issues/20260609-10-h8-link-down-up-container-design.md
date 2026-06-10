# Design: H8 — `Link.down()/up()` Broken for Container Endpoints — Options & Recommendation
**Created**: 2026-06-09
**Status**: In Progress (awaiting user review)

## Related Issues
- **Parent**: `20260609-3-architecture-code-review-findings.md` — H8 (this design closes it)
- **Where the code lives**: `20260603-3-phase19-link-properties.md` (Link wiring + `_backend`), `20260529-1-topo5-link-toggle.md` (`Link.down()/up()` fault injection), `20260529-4-phase7-docker-container-nodes.md` (ContainerBackend veth wiring)
- **Related design**: `20260609-2-phase20-hub-switch-design.md` — D6 folds the per-side backend resolution into its endpoint generalization; M1 (Backend Protocol drift) is adjacent
- **Sibling**: `20260609-9-h6-impairments-not-persisted-design.md` — `up()`'s re-apply path is shared

## Problem

### What the code does today — two distinct bugs

**Bug 1: `Link._backend` is always the VM (libvirt) backend.**
`_wire_link` sets one backend ref on the Link for later `down()`/`up()`:

```python
# engine.py:702
link._backend = self._vm_backend(topology.name)      # ALWAYS LibvirtBackend
```

But the very next loop wires the endpoints using the **correct per-node** backend:

```python
# engine.py:712-717
for side in (link.if_a, link.if_b):
    ...
    side_node = topology._nodes[side.node_name]
    self._backend_for(topology.name, side_node).attach_interface(vm_id, br, mac)   # right backend per side
```

So the engine *knows* how to pick the right backend (`_backend_for`, `engine.py:142-155`, returns the ContainerBackend for container nodes), and uses it for the initial attach — but throws that knowledge away when stashing `link._backend`. `Range.connect` repeats the mistake: `link._backend = lvb` always (`topology.py:758`).

Now look at what `up()` does with that single backend (`topology.py:327-346`):
```python
self._backend.create_bridge(self._bridge_name)
for vm_id, mac in self._endpoints:
    self._backend.attach_interface(vm_id, self._bridge_name, mac)   # libvirt backend, even for a container vm_id
```
For a **VM↔container** link, one endpoint's `vm_id` is a Docker container name. Calling `LibvirtBackend.attach_interface(container_name, ...)` is the wrong code path entirely — the container's interface is a veth pair managed by `ContainerBackend.attach_interface` (`container_backend.py:158-220`), which does the docker-pid/nsenter/veth dance. The libvirt backend has no idea how to re-wire a container's NIC. So after a `down()`/`up()` toggle, the container side of the link is **not reconnected** — the link comes back half-wired (VM side restored, container side dead).

**Bug 2: `ContainerBackend.attach_interface` skips when an orphaned veth exists.**
The container attach short-circuits if the host-side veth already exists (`container_backend.py:178-183`):
```python
check = _run(self._netns(["ip", "link", "show", host_veth]), check=False)
if check.returncode == 0:
    log.debug("attach_interface: veth %s already present, skipping")
    return
```
The veth name is deterministic per `(vm_id, mac)` (`_veth_names`, `container_backend.py:38-45`). When `Link.down()` deletes the bridge (`topology.py:321`), the kernel does **not** delete the veths that were enslaved to it — it just **un-enslaves** them (removes their `master`) and they may go down. The host-side veth *still exists*. So on `Link.up()`, even if we called the right backend, `attach_interface` sees the veth present and **returns early without re-mastering it to the recreated bridge or bringing it up** → the container is still cut off.

### Why it's wrong / blast radius (a real failure)
A lab with a container IDS linked to a VM: `lab.link("ids", "router").down()` then `.up()` to simulate a flap. After `up()`, the VM↔container link is dead: the container never rejoins the bridge. The user sees "I bounced the link and now the IDS can't see anything" — a fault-injection primitive that corrupts the very topology it's meant to test. It's silent (no error; `up()` "succeeds"). Pure VM↔VM links happen to work because the libvirt backend is correct for both ends — so the bug only bites mixed/container topologies, which is exactly where it's least expected.

### Background a learner needs
- **Deleting a Linux bridge doesn't delete its ports.** Enslaved interfaces (TAPs, veths) survive; they just lose their `master` and often their `UP` state. Recreating a same-named bridge does **not** auto-re-adopt them — you must explicitly `ip link set <dev> master <br>` and `set <dev> up` again. `Link.up()` already does this re-assert for VM TAPs via `attach_interface`; it must do the equivalent for container veths.
- **A link can span two different backends.** A VM endpoint is driven by libvirt (TAP inlined in domain XML); a container endpoint is driven by Docker + manual veth plumbing. "The link's backend" is not a single thing — it's per-endpoint. The engine already encodes this in `_backend_for(node)`; the Link just needs to carry it.

---

## D1: How should `Link` reach the right backend per endpoint?

| Option | Shape | Pros | Cons |
|---|---|---|---|
| **A — Resolve the backend at call time via the engine's `_backend_for(node)`** ✅ | Link holds a ref to the engine (or a small resolver callable); `up()`/`down()` look up each endpoint's backend by its node, exactly like `_wire_link:717` does | Single source of truth (`_backend_for` already exists and is correct); no per-side state to keep in sync; `connect` gets it right for free by reusing the same resolver; naturally extends to Phase 20 L2 endpoints | Link gains a dependency on the engine/resolver (it already holds `_db`, `_topology_name` — not a new kind of coupling) |
| B — Store per-side backend refs on the Link (`_backend_a`, `_backend_b`) | `_wire_link` stashes `_backend_for(node_a)` and `_backend_for(node_b)`; `up()` iterates `(endpoint, backend)` pairs | Explicit; no engine ref on Link; `down()`'s `delete_bridge` can use either side (bridge lives in one netns) | Two more fields to populate in *both* `_wire_link` and `connect`; risk of them drifting from `_backend_for`; bridge ops (`create_bridge`/`delete_bridge`) still need *a* backend — which side owns that? |
| C — Document VM-only, raise on container links | `down()`/`up()` raise if any endpoint is a container | Smallest code | Abandons a real, shipped capability (container nodes + link toggle both exist); turns a bug into a permanent limitation; users with mixed labs lose fault injection — regressive |
| D — Make `attach_interface` uniform so any backend can wire any node | push a common interface down | "One backend to rule them" | A VM's NIC and a container's veth are genuinely different mechanisms; forcing one API over both is a leaky abstraction; far more than the bug needs |

### ✅ Recommendation: **A** (resolve per-endpoint at call time), with **B as an acceptable fallback**
`_backend_for(node)` is already the correct, single dispatcher (`engine.py:142-155`) and `_wire_link` already uses it for the initial attach. The cleanest fix is for `Link.up()`/`down()` to resolve each endpoint's backend the **same way at call time**, rather than caching one wrong backend. Give the Link a resolver — either a back-reference to the engine (it can call `engine._backend_for(topology._nodes[node_name])`) or a tiny stored `{node_name: backend}` map built once from `_backend_for`. The bridge-level ops (`create_bridge`/`delete_bridge`) target the netns where the bridge lives, so either endpoint's backend works for those (in ns-mode both the per-range libvirt and container backends operate in the same netns); pick the VM backend if present, else the container backend. B is fine if the team prefers no engine ref on Link; the key invariant is **per-endpoint dispatch via `_backend_for`, never a single hard-coded `_vm_backend`**.

---

## D2: Fix the container-veth early-return (Bug 2)

`ContainerBackend.attach_interface` must distinguish "already fully wired" from "veth exists but orphaned after a bridge delete."

| Option | Behavior | Pros | Cons |
|---|---|---|---|
| **A — When the veth exists, re-assert `master <bridge>` + `up` instead of returning** ✅ | replace the early `return` (`container_backend.py:179-182`) with: if veth present, run `ip link set <host_veth> master <bridge>` and `set <host_veth> up` (idempotent), then return | Restores connectivity after `down()/up()`; still cheap on the duplicate-attach-during-deploy case (re-master to the same bridge is a no-op); matches what VM `attach_interface` effectively guarantees | Must run the re-master inside the right netns (`self._netns([...])`, already the helper) |
| B — Delete the orphaned veth and rebuild the whole pair | tear down + recreate veth, re-enter container netns | "Clean slate" | The container-side veth was moved into the container's netns and renamed; tearing down both ends means re-doing the nsenter/rename/IP dance — heavier and riskier than a re-master; the container side never lost its config, only the host side lost its bridge |
| C — Track wired-state in `_specs` and branch on it | remember whether we've attached | Explicit state | Adds mutable wiring state that can desync from the kernel; the kernel (`is this veth mastered to the bridge?`) is the real truth — query it, don't mirror it |

### ✅ Recommendation: **A**
The container side of the veth survived the bridge delete intact (it lives in the container's netns); only the **host side lost its `master`**. So the minimal correct repair is to re-assert `master <bridge>` + `up` on the existing host veth — idempotent during normal deploy (it's already mastered to that bridge), curative after a flap. Keep the "veth exists" check, but make its branch *re-assert* rather than *skip*.

---

## Interaction notes
- **`connect` parity**: `Range.connect` (`topology.py:758`) must use the same per-endpoint resolution as `_wire_link`, or reconnected container links stay broken. Whatever D1 mechanism is chosen, apply it in both places (ideally one shared helper).
- **Phase 20 (`20260609-2` D6)**: that design's endpoint generalization (TAP-or-veth device names, L2↔L2 veth links) sits on top of this — once the Link dispatches per-endpoint, an L2 endpoint's backend/device resolves through the same seam. Land H8's per-endpoint dispatch first; Phase 20 extends it.
- **`_reapply_impairments` (H6)**: runs inside `up()` after re-attach; it also resolves the TAP per endpoint, so it benefits from the same per-endpoint backend correctness.

## Recommended shape (summary)
1. `Link` reaches the correct backend per endpoint via `_backend_for(node)` (engine ref or a `{node_name: backend}` map built in `_wire_link`/`connect`) — drop the single `link._backend = self._vm_backend(...)` assignment (`engine.py:702`) and `link._backend = lvb` (`topology.py:758`).
2. `Link.down()`/`up()` iterate `(node_name, vm_id, mac)` and call the resolved backend's `attach_interface`; bridge create/delete uses an endpoint backend that targets the bridge's netns.
3. `ContainerBackend.attach_interface` (`container_backend.py:178-183`): when `host_veth` exists, re-assert `master <bridge>` + `up` (idempotent) instead of returning early.
4. Keep one shared helper so deploy-time wiring and `connect`-time rebuild use identical per-endpoint dispatch.

## Test strategy
**Gate 1 (unit, MockBackend + a mock ContainerBackend):**
- VM↔container link: after `_wire_link`, assert the *container* endpoint's re-attach on `up()` is dispatched to the **container** backend, not the libvirt backend (assert via recorded calls on each mock).
- `down()` then `up()` on a VM↔container link → both endpoints re-attached through their correct backends (the half-wired bug, reproduced + fixed).
- `connect` rebuild of a VM↔container link → same per-endpoint dispatch as deploy.
- ContainerBackend `attach_interface` unit: pre-create the host veth, call attach → asserts a `master`+`up` re-assert is issued (not an early return). (MockBackend/recording variant since real veth/docker needs Gate 2.)

**Gate 2 (EC2):** Required — this is real veth/bridge/Docker netns behavior MockBackend can't prove. One integration test: deploy a VM↔container link (e.g. `20260529-4` topo), confirm ping both ways, `link down` → ping fails, `link up` → **ping succeeds again from the container side** (the regression that proves Bug 1+2 are fixed end-to-end). Add a VM↔VM `down/up` case as the no-regression guard.

## Unresolved questions
- D1 mechanism: engine back-ref on `Link` vs a `{node_name: backend}` map. Both are fine; the map avoids a Link→engine pointer but must be rebuilt in `connect`. Recommend the map for looser coupling — confirm preference.
- For `down()`'s `delete_bridge` on a VM↔container link, which endpoint's backend issues it? (Both operate in the same range netns; recommend "VM backend if any VM endpoint, else container backend" for determinism.)

## Progress Log
- 2026-06-09: Read `engine.py` `_wire_link` (`:684-723`, the `_backend` assignment at `:702` vs correct `_backend_for` at `:717`), `_backend_for` (`:142-155`), `topology.py` `Link.down/up` (`:315-346`) + `connect` link rebuild (`:752-767`, `_backend = lvb`), `container_backend.py` `attach_interface` (`:158-220`, early-return at `:178-183`). Confirmed both bugs by direct read. Wrote options; reconciled with `20260609-2` D6 and H6.

## Resolution
_(pending user review)_
