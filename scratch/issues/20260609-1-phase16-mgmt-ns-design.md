# Design: Phase 16 Management Namespace — Options & Recommendation
**Created**: 2026-06-09
**Status**: In Progress

## Related Issues
- **Parent**: `20260603-1-phase16-management-namespace.md` — Phase 16 spec (links back here)
- **Architecture**: `agents/network-architecture.md` §5.1, §7.1 — three-tier model
- **Subnet registry**: `20260601-5-parallel-test-isolation.md` — host-global flock allocator (`rangectl/subnet_registry.py`)
- **Plan**: `20260527-1-vm-testbed-platform-design.md` — Phase 16

## Problem
Today every range wires a veth pair **directly to the host** (`netns.py:create_mgmt_network`), puts the `.254` gateway IP on the host, and adds per-range FORWARD/NAT rules to **host iptables** (`internet.py:enable_internet`, chain `RANGE-<name>`). Every deploy/destroy mutates host networking. Phase 16 interposes a persistent `rangectl-mgmt` namespace so the host is configured exactly once (1 veth + 1 route + 1 FORWARD + 1 MASQUERADE) and all per-range churn happens inside the mgmt-ns.

The central design question (user's framing): **how does the engine know the mgmt-ns exists and is healthy — config, check, or both?**

---

## D1: Lifecycle ownership — who creates the mgmt-ns, and when

### Option A — Lazy ensure on deploy (idempotent, flock-guarded)
Engine calls `ensure_mgmt_ns()` before `_setup_namespace()`. It checks, creates/heals what's missing, returns. Concurrent deploys serialize on an flock (same `_locked()` pattern as `subnet_registry.py:46`).
- **Pro**: zero operator steps; survives reboot (netns + iptables vanish on reboot — lazy ensure heals automatically); matches existing idempotent patterns (`-C` before `-A`, "already exists" tolerance).
- **Pro**: concurrent `pytest -n` / multi-process deploys already coordinate via flock — proven pattern.
- **Con**: host setup (the 4 host-side ops) happens implicitly on first deploy; slightly magic.

### Option B — Explicit one-time setup (`rangectl setup` / provisioning script)
Arch doc §7.1 assumes Ansible/cloud-init does this at host provisioning. Deploy fails fast with a clear error if mgmt-ns missing.
- **Pro**: explicit, auditable host mutation; matches "host config is locked" story.
- **Con**: **does not survive reboot** without a systemd unit (more machinery); breaks the 20-minute-to-first-topology goal; every test run on a fresh EC2 box needs an extra step; orphan-cleanup scripts that `ip netns del` everything would brick deploys until re-setup.

### Option C — Hybrid: lazy ensure + verify-and-heal + explicit CLI
`ensure_mgmt_ns()` always runs before deploy and **verifies the full invariant** (ns exists, veth up, addresses, route, 4 iptables rules), healing any missing piece. `rangectl mgmt-ns status` exposes the same check read-only; `rangectl mgmt-ns reset` force-recreates and reconnects ranges from StateDB.

### ✅ Recommendation: **Option C**
Reboot reality forces check-and-heal regardless, so a config flag saying "it's set up" can never be trusted — **the kernel is the source of truth, not config**. Verification is ~6 read-only commands (microseconds); run it every deploy. No new config file needed for "is it running".

---

## D2: Health check & drift detection

What "healthy" means (the invariant `ensure_mgmt_ns()` checks):
1. netns `rangectl-mgmt` exists
2. veth pair `veth-mgmt-host` ↔ `veth-mgmt-ns` exists, both UP
3. Host: `10.254.0.1/30` on veth, route `<aggregate> via 10.254.0.2`, FORWARD ACCEPT, MASQUERADE, `ip_forward=1`
4. Mgmt-ns: `10.254.0.2/30`, default route via `10.254.0.1`, `lo` up, `ip_forward=1`

**Heal strategy**: recreate missing pieces individually (all ops idempotent). If the *namespace itself* is missing but ranges are running (mgmt-ns was killed), full reconnect: re-run `connect_range()` for every range StateDB says is running. This makes the Phase 16 recovery test ("kill mgmt-ns → recreate → ranges reachable") the same code path as ordinary healing — no special reset logic.

**Partial-failure stance**: ensure never half-fails silently — raise with the specific missing piece named if a heal op fails (e.g., iptables denied).

---

## D3: Configuration — what's configurable, where it lives

Parameters: transit subnet (`10.254.0.0/30`), aggregate route, ns name, veth names.

| Option | Verdict |
|---|---|
| New config file `~/.rangectl/config.yaml` | ❌ Premature — nothing else uses one; one knob doesn't justify a config subsystem |
| Constants in `mgmt_namespace.py` + env-var overrides | ✅ Matches `RANGECTL_SUBNET_REGISTRY` / `RANGECTL_STATE_ROOT` precedent |
| Store in StateDB | ❌ DB is per-user; mgmt-ns is host-global. Kernel state is authoritative (D1) |

Proposed env vars (**user-approved 2026-06-09**):
- `RANGECTL_MGMT_TRANSIT` (default `10.254.0.0/30`) — host↔mgmt-ns link
- `RANGECTL_MGMT_POOL` (default `10.255.0.0/16`) — range mgmt subnet pool / host aggregate route

### D3b: CIDR overlap on host → ABORT (user decision 2026-06-09)
Before creating the mgmt-ns (and on every `ensure_mgmt_ns()` verify pass), check the host routing
table (`ip route`) and interface addresses for any existing route/address overlapping the transit /30
or the pool aggregate **that rangectl did not create itself**. On overlap: **abort deploy/setup with a
clear error** naming the conflicting route/interface and pointing at the env vars to remap. Never
warn-and-continue — a shadowed real route fails silently otherwise.

### ⚠️ Sub-decision D3a: range subnet pool must move
Current pool is `192.168.100.0/24 … 192.168.199.0/24` (`subnet_registry.py:30`) — **not summarizable as one aggregate route** (100–199 doesn't align to a CIDR boundary). The host's single route is the whole point of Phase 16.
- **Option 1**: keep 192.168 pool, add 100 host routes — defeats "host never modified per-range". ❌
- **Option 2**: route `192.168.0.0/16` — collides with common LANs/VPNs, and EC2 conftest NAT already had to widen to this (commit `4f5eaf4`) as a workaround. ❌
- **Option 3** ✅: migrate pool to `10.255.1.0/24 … 10.255.254.0/24` per arch doc §7.1. One clean host route `10.255.0.0/16`, 254 subnets (vs 100 today), no RFC1918 home-LAN collisions. Registry change is one constant + tests; allocations are ephemeral (per-deploy) so no data migration.

---

## D4: Migration & compatibility

- **Namespace mode** (the default since `4f5eaf4`): hard cutover to mgmt-ns wiring. No toggle — a "with/without mgmt-ns" flag doubles the test matrix for an internal change with no user-facing API.
- **Legacy mode** (`use_namespaces=False`): untouched — keeps direct host wiring. It's already a separate code path.
- `internet.py` chains (`RANGE-<name>`) move into the mgmt-ns (`ip netns exec rangectl-mgmt iptables …`). Host keeps the single static MASQUERADE for the aggregate.
- EC2 conftest `vm_internet_nat` fixture and the `192.168.0.0/16` widening from `4f5eaf4` get deleted — superseded by the structural design.

## D5: Concurrency & teardown semantics

- **Creation race**: flock file `~/.rangectl/mgmt_ns.lock` around ensure (reuse `_locked()` pattern).
- **Teardown**: mgmt-ns is **never auto-destroyed** — not on last range destroy, not on process exit. It's host infrastructure, like the subnet registry file. Only `rangectl mgmt-ns reset` (recreate) touches it. This avoids refcounting across processes entirely.
- **Orphan reaping**: `Range.cleanup()` and the EC2 orphan-clean snippet must NOT delete `rangectl-mgmt` (today's snippet deletes every `rangectl*` netns — update it to exclude the mgmt ns, or rely on ensure healing it next deploy).

## D6: Naming
- ns: `rangectl-mgmt` (matches `rangectl-<range>` convention, but excluded from range-orphan sweeps by exact-name check)
- veths: `veth-mgmt-host` / `veth-mgmt-ns` (static names fine — only one mgmt-ns per host)
- Per-range veths from mgmt-ns: keep existing `mgh{hash}`/`mgp{hash}` scheme — they just live in the mgmt-ns now instead of the host

---

## Recommended shape (summary)

1. New `rangectl/mgmt_namespace.py`: `ensure_mgmt_ns()` (verify-and-heal, flock-guarded, called by engine before every namespace deploy), `connect_range()`, `disconnect_range()`, `destroy_mgmt_ns()` (reset only), `status()` (dict for CLI).
2. **No config flag for "is it running"** — kernel state is verified each deploy; env vars override the two subnets only.
3. Migrate subnet pool to `10.255.0.0/16` (D3a) — prerequisite commit, can land first with its own tests.
4. Per-range wiring (`netns.py`) and internet chains (`internet.py`) execute inside mgmt-ns; host iptables code paths deleted from per-range flows.
5. mgmt-ns persistent, never refcounted, healed not guarded.
6. CLI: `rangectl mgmt-ns status` (runs the same invariant check) and `rangectl mgmt-ns reset`.

Suggested split for coding agents (each one context window):
- **16a**: subnet pool migration to 10.255/16 + registry tests (small, independent)
- **16b**: `mgmt_namespace.py` + engine/netns/internet rewiring + unit tests
- **16c**: CLI subcommands + integration tests on EC2 (incl. kill/heal recovery test)

## Progress Log
- 2026-06-09: Explored current code (netns.py, internet.py, supervisor.py, engine.py, subnet_registry.py, cli.py); wrote options + recommendation.
- 2026-06-09: **User approved** D1=C (verify-and-heal, no config flag), D3a (pool → 10.255.0.0/16), D5 (never auto-destroy). Amendments: both subnets env-overridable (D3), host CIDR overlap = hard abort with clear error (D3b). 16a kicked off.
- 2026-06-09: **16a DONE** (this commit). Pool migrated 192.168.100–199/24 → 10.255.0.0/16 carved into /24s, dropping the .0 and .255 edge /24s → `10.255.1.0/24 … 10.255.254.0/24` (254 subnets). Aggregate is exactly `10.255.0.0/16`.
  - `subnet_registry.py`: replaced `POOL_BASE`/`POOL_SIZE` with `DEFAULT_POOL` + new helpers `_resolve_pool()`, `pool_aggregate()`, `pool_subnets()`. `allocate()` now iterates `pool_subnets()`. `POOL_SIZE` kept as default capacity (=254) for callers/tests.
  - **`RANGECTL_MGMT_POOL` env override**: a CIDR (e.g. `10.200.0.0/16`); the /24 list is derived from it. Resolution order **explicit `pool` arg > env > default**, mirroring `RANGECTL_SUBNET_REGISTRY`. Validated: bad CIDR raises `ValueError` naming the env var; prefix > /24 raises "must be at least /24". A /20 override yields 14 usable /24s (edge /24s dropped); a single /24 stays as one usable subnet.
  - conftest `vm_internet_nat`: `MGMT_SUBNET_CIDR` now derives from `subnet_registry.pool_aggregate()` (single source of truth, honors the env var) instead of the hardcoded `192.168.0.0/16` from `4f5eaf4`.
  - Purged old pool from `scratch/scripts/diag-slow-*.py` (node IPs/NAT) and `agents/docs/cli-reference.md`. Migrated mgmt-subnet/mgmt-IP fixture values across unit tests to 10.255.x. Data-plane (10.0.x) examples and historical issue files left untouched.
  - **Gate 1**: `pytest tests/unit` → **333 passed**, 0 skips (326 prior + 7 new subnet_registry tests).
  - **Gate 2** (EC2 i-0cd9c4f8ad3406291, registry reset + orphans cleaned first; pre-check showed no stale 192.168 MASQUERADE):
    - `test_topo1.py::test_topo1_boots_and_pings` → **1 passed in 41.27s** (basic 2-node SDK deploy on 10.255.1.0/24).
    - `test_ns_regression.py::test_ns_internet_full_allows_outbound` → **1 passed in 50.56s** (outbound ping + `apt-get update` through MASQUERADE — proves internet works on the 10.255 subnet/new conftest NAT path).
    - Post-run: no rangectl netns, no qemu orphans, no leftover mgmt MASQUERADE. Instance left running.
  - NOT in scope (16b): `mgmt_namespace.py`, host-CIDR overlap abort (D3b), host route changes.

- 2026-06-09: **16b DONE** — new `rangectl/mgmt_namespace.py` (`ensure_mgmt_ns` verify-and-heal + flock + D3b overlap abort + heal-reconnect from `/ranges/*/range.json`; `connect_range`/`disconnect_range`/`destroy_mgmt_ns`/`status`; `RANGECTL_MGMT_TRANSIT`). Rewired `netns.py` (per-range `mgh<hash>` + gateway + FORWARD/isolation now inside `rangectl-mgmt`), `internet.py` (`netns=` param → `RANGE-<name>` chain in mgmt-ns), engine/supervisor/topology call sites. **H5 fixed**: teardown always disables internet (`destroy_range`→`disconnect_range`, idempotent). Orphan sweeps exclude `rangectl-mgmt`. **Egress NAT**: host static MASQUERADE is `-s <transit /30>` (not the aggregate); `full` ranges MASQUERADE to the transit in the mgmt-ns and the host re-NATs, `none` ranges have no chain so the transit MASQUERADE never matches — free gating. Gate 1: 358 unit, 0 skips. Gate 2 (EC2): smoke a–d + `test_ns_two_node` + internet none/full/toggle all green. See `20260603-1` progress log for full detail. 16c (CLI + broad suite) remains.

## Resolution
16a + 16b complete — see progress entries. Pool is 10.255.0.0/16; transit `10.254.0.0/30` (`RANGECTL_MGMT_TRANSIT`). Persistent `rangectl-mgmt` interposed; host carries only the 4 static ops. 16c remains: `rangectl mgmt-ns status/reset` CLI + broad integration-suite migration (retire conftest blanket NAT per D4).
