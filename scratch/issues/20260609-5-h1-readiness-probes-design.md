# Design: H1 — Readiness Probes Never Executed — Options & Recommendation
**Created**: 2026-06-09
**Status**: In Progress (awaiting user review)

## Related Issues
- **Parent**: `20260609-3-architecture-code-review-findings.md` — H1 (this design closes it)
- **Requirements**: `20260527-2-requirements-and-design-decisions.md` — R4 (Explicit Dependencies with Readiness), D3 (Readiness Over Optimism), D17 (waves block until current-wave nodes pass readiness)
- **Where the API was built**: `20260527-8-phase3-state-machine-dag.md` (DAG/wave engine), `20260527-9-phase4-5-images-dependencies.md` (`service(ready_when=...)`, the probe builders)

## Problem

### What the code does today
The SDK ships a complete readiness-probe **surface** that is never read:

- `rangectl/readiness.py` exports four builders — `port_open(port)`, `ping()`, `process_running(name)`, `command_succeeds(cmd)` — each returning a `ReadinessProbe` dataclass (`types.py:130-135`: `probe_type`, `target`, `timeout=300`, `interval=5`). They are re-exported from the package root (`__init__.py:12`), so they are public API.
- A node accepts `ready_when=` (`topology.py:89`) and stores it on `Node.ready_when` (`topology.py:268`).
- A service accepts `ready_when=` (`dependencies.py:58`) and stores it on `ServiceSpec.ready_when` (`dependencies.py:61`).

Then nothing consumes them. Grepping the whole package, the only reads of `ready_when`/`ReadinessProbe` are the *assignments* above. `engine.py` has **zero** evaluation sites:

- `_deploy_node` (`engine.py:507-634`) transitions `PROVISIONING → READY` immediately after `backend.start(vm_id)` returns (`engine.py:631`). "READY" here means "libvirt accepted the start call" — exactly the L1 "VM running" level that R4 calls "nearly useless," and exactly the GNS3 failure mode D3 was written to avoid ("API returned 200 ≠ operation complete").
- `_inject_dependencies` (`engine.py:725-792`) starts services (`engine.py:783-787`) and never waits for `svc.ready_when`. The next dependency wave (`engine.py:378-381`) begins as soon as the loop returns, so a node whose `depends_on` target is "started but not yet listening" proceeds anyway.

### Why it's wrong
R4/D3 are the **project's stated differentiator** — "Readiness Over Optimism," probes instead of optimistic sleeps. The default was meant to be L2 (OS booted, ping passes); L3 is the user-declared service check. Today the engine is pure L1. The feature users are told exists (it's in the SDK API reference and has helper builders) silently does nothing.

### Blast radius (a real failure a user hits)
A monitoring lab: `siem = node(...); sensor = node(..., depends_on=[siem], ...)` and `siem.service("elasticsearch", ready_when=port_open(9200))`. Elasticsearch takes ~40s to open 9200. The engine marks `siem` READY the instant the VM starts, injects `sensor`'s config immediately, and `sensor`'s "register with SIEM" configure-fn fails because 9200 isn't up yet. The user gets a flaky deploy that "works if I re-run it" — precisely the optimism bug the design forbids. Worse, it's silent: no log says "I skipped your probe."

### Background a learner needs
A **readiness probe** answers "is this thing actually usable yet?", not "did the API call return?". The three levels:
- **L1** — hypervisor says the VM is running. Cheap, almost meaningless (BIOS may still be POSTing).
- **L2** — the guest OS booted and answers on the mgmt network (ping / SSH). This is rangectl's intended default gate before any config is injected.
- **L3** — a *specific service* is serving (`port_open(9200)`, `process_running("nginx")`, `command_succeeds("pg_isready")`). Declared per-service because only the author knows what "ready" means.

A probe needs a **timeout** (give up and fail loudly) and an **interval** (poll cadence) — both already on the dataclass, both currently ignored. The whole point of waves (D17) is that wave N+1 must not start until wave N is *ready*, not merely *started* — without probe evaluation, "wave ordering" degrades to "wave starting order."

---

## D1: Where do probes get evaluated?

The engine boots all nodes in one parallel batch (`engine.py:363-366`, Step 7), wires links (Step 8), then runs dependency injection in DAG wave order (Step 9, `engine.py:378-381`). There are two distinct probe kinds — the **node-level** `ready_when` (is this node usable?) and the **service-level** `ready_when` (is this service serving?) — and they gate different things.

| Option | Where node-probe runs | Where service-probe runs | Pros | Cons |
|---|---|---|---|---|
| **A — Probe at both natural gates** ✅ | end of `_deploy_node`, before `READY` transition (`engine.py:631`); default to an L2 ping if no `ready_when` given | after each `svc` start in `_inject_dependencies` (`engine.py:787`), before the node goes `RUNNING` | Each probe gates exactly the transition it semantically means; matches R4's "deferred until ready" and D17's wave gate (dep-injection already runs in wave order, so a downstream wave naturally waits on the upstream node's service being ready) | Two call sites; node-probe runs inside the parallel boot batch (fine — each thread blocks on its own VM) |
| B — Only a node-level gate, ignore service probes | `_deploy_node` only | not run | Half the work | Drops L3 entirely — `service(ready_when=...)` stays a silent no-op, so H1 only half-closed |
| C — One barrier after Step 7, before links | a post-boot loop probes every node | service probes still in Step 9 | "Simple" single location | Re-derives per-node readiness outside the thread that owns the VM; loses the clean "transition is gated by its own probe" mapping; node that fails its probe still had links/DB written |
| D — Delete the API instead (honest regression) | — | — | Stops *advertising* a feature that doesn't work; smallest diff; zero false promises | Abandons the project's headline differentiator (R4/D3); the SDK ref + builders + `depends_on` semantics all lean on it; users lose the one thing that distinguishes rangectl from GNS3 |

### ✅ Recommendation: **A**
The node probe belongs at the `READY` transition because that transition *is* the claim "this node is usable" — gating it with a default L2 ping makes the default honest (R4's stated default), and an explicit `ready_when` upgrades it to L3. The service probe belongs right after service start because that's the only point where "is :9200 serving?" is meaningful, and because dependency injection already runs in wave order (`engine.py:378`), a downstream node's wave will not begin until the upstream node's services have passed — D17's wave gate falls out for free. D is the honest fallback if the user would rather ship-nothing than build-it, but it surrenders the differentiator; worth stating, not choosing.

---

## D2: How is a probe physically executed?

Probes must run *against the guest*. The engine already has a backend with `exec(vm_id, cmd)` over the mgmt network. The probe types map to guest commands:

| probe_type | guest command (run via `backend.exec`) |
|---|---|
| `ping` | (host→guest) `ping -c1 -W2 <mgmt_ip>` — or just rely on SSH-exec succeeding as the L2 signal |
| `port` | `bash -c 'exec 3<>/dev/tcp/127.0.0.1/<port>'` (pattern already used in `LiveNode.check_port`, `topology.py:988-995`) |
| `process` | `pgrep -x <name>` |
| `command` | the literal `target` string |

| Option | Mechanism | Pros | Cons |
|---|---|---|---|
| **A — Reuse `backend.exec`, poll until 0 or timeout** ✅ | a `wait_for(probe, backend, vm_id, mgmt_ip)` helper in `readiness.py` loops `exec`, sleeping `interval`, until exit 0 or `timeout` elapses | One mechanism; works for every backend (Mock included — it returns canned 0); `check_port` already proves the `/dev/tcp` trick; testable with MockBackend exec-recording | `ping` from inside the guest to itself is odd — for the node-level default, "SSH-exec returns" IS the L2 signal, so the default probe is "exec `true` succeeds" |
| B — Backend-native health (libvirt guest-agent, docker healthcheck) | per-backend probe impl | "Most accurate" | New per-backend surface; guest-agent not installed in our images; container vs VM divergence; over-engineered for port/process/command |
| C — Host-side only (ping the mgmt IP, nc the port) | host runs the checks | No guest cooperation | `process`/`command` can't be done from the host; port checks need the port bound to the mgmt IP not localhost; splits logic by probe type |

### ✅ Recommendation: **A**
A single `exec`-based poll loop covers all four probe types, reuses the `/dev/tcp` idiom already in the codebase, and is trivially unit-testable because MockBackend records `exec` calls and returns canned results. The node-level **default** (no `ready_when`) is "a trivial `exec` succeeds" — which IS L2 (the guest answered over mgmt). `ping()` as an explicit probe maps to a host→guest `ping -c1`.

---

## D3: What happens when a probe times out?

| Option | Behavior | Pros | Cons |
|---|---|---|---|
| **A — Fail the deploy loudly** ✅ | raise `ReadinessError(node, probe, elapsed)`; `cleanup_on_fail` (already the default, `engine.py:236-239`) tears the partial range down | Matches D3 ("not assumed") and the Phase-16 "abort loudly" precedent; a probe that never passes IS a broken lab; the existing BaseException-safe cleanup handles it | A slow-but-eventually-fine service fails if `timeout` is set too low — but `timeout` defaults to 300s, generous |
| B — Warn and continue | log a warning, proceed to READY/RUNNING anyway | Deploy "succeeds" on flaky services | This IS the current bug wearing a log line — reintroduces optimism; downstream waves still race |
| C — Configurable per probe (`required=True/False`) | author chooses | Flexible | New API surface for a knob nobody asked for; premature; the right default (fail) covers the real case |

### ✅ Recommendation: **A**
A readiness probe is a *gate*; a gate that lets everything through isn't a gate. Timeout → raise → existing cleanup. The 300s default timeout is the pressure-relief valve; if a user genuinely wants "best effort," that's a future `required=False`, not now.

---

## Recommended shape (summary)
1. Add `wait_for(probe: ReadinessProbe, backend, vm_id, mgmt_ip) -> None` to `readiness.py`: poll `backend.exec` (or host ping for `ping`) every `probe.interval`s until exit 0; raise `ReadinessError` after `probe.timeout`s. Map the four `probe_type`s to guest commands per D2.
2. In `_deploy_node`, **before** the `PROVISIONING → READY` transition (`engine.py:631`): run `node.ready_when` if set, else the default L2 check (a trivial `exec` succeeds). Log the probe + result (D-prefix structured log per D17/requirements §D17 logging).
3. In `_inject_dependencies`, **after** each `svc` start (`engine.py:787`): if `svc.ready_when` is set, `wait_for` it before moving on. Because Step 9 runs in wave order, this gates downstream waves automatically.
4. Add `ReadinessError` to `types.py` alongside the other engine exceptions.
5. Skip probes for L2 nodes / containers where they don't apply only if a future phase needs it — not in scope here (no L2 hub/switch nodes exist yet; see `20260609-2`).

## Test strategy
**Gate 1 (unit, MockBackend):**
- MockBackend grows a way to script `exec` results per command (return non-zero N times then zero) so a probe's poll loop is exercised deterministically.
- node-level: node with `ready_when=port_open(22)` → engine issues the `/dev/tcp/.../22` exec and only transitions READY after it returns 0; assert ordering via recorded calls.
- service-level: `service("es", ready_when=port_open(9200))` → engine waits after start; a node depending on it does not begin injection until the probe passes (assert wave ordering against recorded call sequence).
- timeout: probe that never returns 0 → `ReadinessError` raised; with `cleanup_on_fail=True` the partial range is torn down (assert teardown calls).
- default: node with no `ready_when` → exactly one default L2 exec before READY.

**Gate 2 (EC2):** Yes — one integration test where a real service has a measurable startup delay (e.g. start a unit that opens a port after a `sleep`), assert deploy blocks on the probe and that a dependent node's config sees the port already open. This is the only way to prove the probe gates real wall-clock ordering, not just recorded mock calls.

## Unresolved questions
- Should the **node-level default** be a host→guest `ping`, or "SSH/exec returns 0"? They differ subtly: exec-returns proves the mgmt path AND sshd; ping proves only ICMP. Recommendation leans exec-returns (it's what every later step needs anyway), but flagging for the user since R4 literally says "ping passes."
- `ReadinessProbe.interval` defaults to 5s; for a 300s timeout that's 60 polls. Acceptable, or want a backoff? (Recommend flat interval — simplest, matches the dataclass.)

## Progress Log
- 2026-06-09: Read `readiness.py`, `types.py`, `engine.py` (`_deploy_node`, `_inject_dependencies`), `dependencies.py`, R4/D3/D17 in requirements. Confirmed zero evaluation sites — the probe surface is entirely inert. Wrote options.

## Resolution
_(pending user review)_
