# [Review]: Architecture & Code Review Findings (v0.4.0)
**Created**: 2026-06-09
**Status**: Complete (review) — fixes not yet scheduled

## Related Issues
- **Plan**: `20260527-1-vm-testbed-platform-design.md` - implementation phases
- **Requirements**: `20260527-2-requirements-and-design-decisions.md` - R4/D3 readiness gap found here
- **API**: `20260527-3-sdk-api-reference.md` - several documented APIs are silent no-ops
- **Testing**: `20260527-4-testing-strategy.md` - CLI `link` shipped untested at both gates
- **StateDB locks**: prior write-lock gap fixed in state.py; gap moved to `_conn` call sites (H3)

## Goal
Full-codebase architecture + code review. Catalog verified problems with file:line and fixes; record what's working well so it isn't refactored away.

## Verdict
Architecture is sound and right-sized (Topology → Engine → Backend protocol → StateDB; ns-mode as per-range backend variant, not an engine fork). Implemented paths are evidence-driven and well-engineered. Main risk: **gap between documented API surface and what actually executes** (silent no-ops), plus abstraction leaks (`_db._conn`, backend privates from topology.py).

## HIGH severity
- [ ] **H1 Readiness probes never executed** — `ready_when` accepted (`topology.py:89`, `dependencies.py:58`) stored (`types.py:119`) and never read. Zero evaluation sites in engine.py. This is R4/D3, the project's core differentiator. Fix: evaluate node probes before configure fns and service probes after start in `_inject_dependencies`; or remove API until implemented. → **Design: `20260609-5-h1-readiness-probes-design.md`**
- [ ] **H2 install/verify/service exit codes ignored** — `engine.py:772-787` discards ExecResult for `install_cmd`, `verify_cmd`, `systemctl enable/start`. Failed installs ⇒ green deploy. Fix: raise on nonzero like the apt path (`engine.py:755-760`). → **Design: `20260609-6-h2-exit-codes-ignored-design.md`**
- [ ] **H3 `_db._conn` lock bypass** — `engine.py:336-340,691-697`, `topology.py:1088-1094` execute/commit on shared sqlite conn without `_lock`. Recreates the StateDB race one layer up (prerequisite for parallel deploy). Fix: add `StateDB.add_bridge/add_link/get_snapshot_id` (locked); delete `_conn` pokes. → **Design: `20260609-7-h3-statedb-conn-lock-bypass-design.md`**
- [ ] **H4 supervisor kills unverified PID as root** — `supervisor.py:244-245` SIGTERM/KILLs stored PID + children with no identity check; PID reuse after host reboot ⇒ kill arbitrary root process. Violates scoped-cleanup rule. Fix: verify `/proc/<pid>/cmdline` (or starttime) before signalling. → **Design: `20260609-8-h4-supervisor-unverified-pid-kill-design.md`**
- [ ] **H5 internet teardown leak / policy bypass** — `engine.py:859-861` only calls `disable_internet` if engine flag == "full"; `Range.enable_internet()` (`topology.py:896`) isn't consulted. Stale POSTROUTING jump survives, next range with the recycled /24 inherits full internet. Fix: call `disable_internet` unconditionally (it's idempotent). → **Fix in flight via Phase 16b (`20260603-1`)** — no separate design doc.
- [ ] **H6 impairments not persisted** — `cli.py:323-327` reads in-memory `Link._impairments`; no impair column in state.py. Cross-process `rangectl link ... status` shows "none" while netem is active; reconnected `Link.up()` drops impairments. Fix: persist per-side params (JSON col on links) or read live `tc qdisc show`. → **Design: `20260609-9-h6-impairments-not-persisted-design.md`**
- [ ] **H7 `Range.connect()` double-applies RANGECTL_RANGE_PREFIX** — `topology.py:72` + `topology.py:697`. With prefix set, connect builds "w0-w0-foo"; `engine.destroy` lookups miss ⇒ silently skips per-node teardown (containers leak). Fix: connect constructs Topology without re-prefixing. → **Design: `20260609-11-h7-range-prefix-double-applied-design.md`**
- [ ] **H8 Link.down()/up() broken for container endpoints** — `engine.py:702` binds `link._backend` to LibvirtBackend always (deploy wiring correctly uses `_backend_for` at :717). Also `ContainerBackend.attach_interface` (`container_backend.py:178-183`) skips when orphaned veth exists. Fix: per-side backends on Link; re-assert master+up when veth exists. → **Design: `20260609-10-h8-link-down-up-container-design.md`**

## MEDIUM
- [ ] **M1 Backend Protocol drift** — `backend.py` omits `status()`, `run_tc()`, `_find_tap_for_mac()`; topology.py calls all three (`topology.py:391,397,1018`); ContainerBackend lacks `status` ⇒ AttributeError on container `node status`. Fix: extend Protocol, make tap lookup public or push impairment into backend.
- [ ] **M2 configure fns get degraded LiveNode** — `engine.py:735-741` omits os_type/ssh_user/db vs correct build at :394-403 ⇒ wrong driver for VyOS/Windows, `logs()` raises. Fix: reuse same construction.
- [ ] **M3 no XML escaping / name validation** — `libvirt_backend.py:56-104` raw interpolation; names unvalidated. Also shell injection surface in `supervisor.py:95-108` (unquoted base path in root `bash -c`). Fix: validate names `^[A-Za-z0-9_-]+$` at `Topology.node()` (cheapest, closes both) + `shlex.quote`.
- [ ] **M4 create_range not idempotent, no rollback, no libvirtd-socket wait** — `supervisor.py:166,175-204`. Stale netns blocks recreate; partial failure leaks; first virsh define races libvirtd start. Fix: tolerate/delete stale netns, try/except teardown, poll for socket.
- [ ] **M5 exec() can hang forever** — `libvirt_backend.py:646-647` `recv_exit_status()` blocks indefinitely. Fix: deadline loop on `exit_status_ready()`.
- [ ] **M6 container restart loses networking** — `docker stop` destroys veths; `start()` doesn't rewire (`container_backend.py:100-102`); specs retained in `self._specs` so fix is contained.
- [ ] **M7 packages()/user()/run_on_boot() silent no-ops** — `engine.py:744` (non-Linux packages dropped), `dependencies.py:45-51` (`_users`/`_boot_commands` never consumed). Documented in SDK ref. Fix: wire or remove.
- [ ] **M8 no busy_timeout on shared DB** — `state.py:120-124`; cross-process writer ⇒ immediate "database is locked". Fix: `PRAGMA busy_timeout=5000`.
- [ ] **M9 subnet allocate not idempotent** — `subnet_registry.py:146-159` second /24 per re-allocate; crash leaks pool entry permanently. Fix: return existing subnet for known topology_name.
- [ ] **M10 CLI `link` untested at both gates** (how H6 shipped); **M11 `rangectl exec` joins args destroying quoting** (`cli.py:123`, use `shlex.join`); **M12 MockBackend permissive** (any vm_id/image accepted; engine `engine.py:533-534` treats unknown image as path — typo fails late on EC2); **M13 CLI uses privates** (`cli.py:76,157,230` `rng._db`/`_nodes` ⇒ Range needs public nodes accessor); **M14 freeze() crashes without cgroup** (`cgroup.py:100` raw FileNotFoundError; match `is_frozen` handling).

## LOW (batch-fixable)
link is_up never persisted (`topology.py:315-346` vs restore :762) · impair records success despite tc failure (`topology.py:389-397`) · tbf burst 32kbit caps high rates (`link_properties.py:50-52`) · destroy transition table decorative (`engine.py:802-805`) · validate_resources omits disk (vs D18) · OVERLAY_ROOT frozen at import (`engine.py:51-52`) · use_namespaces defaults differ across 3 entry points · `_deploy_wave` raises only errors[0] (`engine.py:472`) · compute_waves drops foreign deps silently (`engine.py:205-207`) · duplicate-interface links unvalidated (`topology.py:120`) · delete_topology keeps logs rows · overlay hardcoded 10G (`libvirt_backend.py:554`) · netns iptables insert failure silent (`netns.py:74`) · `logs --node` exit-code contract (`cli.py:209`) · `Range.connect` hardcodes ssh_user (`topology.py:736-746`) · `destroy --all` aborts on first failure (`cli.py:336`) · pyproject version 0.1.0 vs v0.4.0; unused libvirt-python extra; pytest-xdist missing from dev · docs/rangectl-overview.md says CLI "not yet implemented" + stale test counts; cli-reference.md omits `link`.

## Good decisions (keep; do not refactor away)
- Boot decoupled from DAG w/ measured rationale (`engine.py:104-108,356-381`); clean Kahn sort (`engine.py:201-219`)
- BaseException-safe deploy cleanup + cleanup_on_fail escape (`engine.py:229-287`)
- Deterministic MACs/veth names enabling stateless reconnect/teardown (`engine.py:72-75`, `container_backend.py:38-45`)
- PID-ns teardown (libvirtd as PID 1 ⇒ kernel reaps all QEMU) + cgroup.kill+drain (`supervisor.py`, `cgroup.py:75-95`)
- flock'd host-global subnet registry, pool aggregates to one route (`subnet_registry.py:84-119`)
- Per-range NAT chains, surgical teardown, idempotent _ensure (`internet.py`)
- Cross-scheme isolation DROPs for all prefix pairs w/ rationale (`networking.py:56-70`)
- argv-list subprocess everywhere, XML via /dev/stdin (`libvirt_backend.py:379`)
- VyOS console bootstrap: marker-sliced log buffer, hw-id pinning, justified D19 deviation (`libvirt_backend.py:226-370`)
- Range.connect liveness verification (DB/json/PID/netns/socket) (`topology.py:666-685`)
- MockBackend recording fake + hermetic autouse env fixture; 333 unit tests in 4.4s; cross-process CLI integration test
- MAC-quoting netplan comment (sexagesimal YAML) (`cloudinit.py:42-47`); per-range backend instances (`libvirt_backend.py:110-149`)
- Lean public API + 4 runtime deps, argparse not a CLI framework

## Suggested fix order
1. H1+H2 (silent-success class — undermines project identity)
2. H4+H5 (root-safety + policy bypass)
3. H3+M8 (StateDB API additions; unblocks parallel deploy work)
4. H7+H8+M1+M2 (reconnect/link correctness, Protocol sync)
5. M3 name validation at Topology boundary
6. Low batch + doc refresh

## Progress Log
- 2026-06-09: review completed (4 parallel agents + manual verification of H1-H4, H7, H8). All HIGH findings verified by direct code read.

## Resolution
Review delivered. Fixes to be scheduled as separate issues/phases.
