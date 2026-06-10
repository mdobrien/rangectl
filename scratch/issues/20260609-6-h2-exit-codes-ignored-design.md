# Design: H2 — install/verify/service Exit Codes Ignored — Options & Recommendation
**Created**: 2026-06-09
**Status**: In Progress (awaiting user review)

## Related Issues
- **Parent**: `20260609-3-architecture-code-review-findings.md` — H2 (this design closes it)
- **Where the code lives**: `20260527-9-phase4-5-images-dependencies.md` (the `install()`/`service()` dependency model), `20260527-8-phase3-state-machine-dag.md` (the engine's `_inject_dependencies`)
- **Sibling**: `20260609-5-h1-readiness-probes-design.md` — H1 is the *readiness* side of the same "silent success" class; H2 is the *command-failure* side

## Problem

### What the code does today
`Engine._inject_dependencies` (`engine.py:725-792`) runs a node's dependency steps in order. The **apt path is checked**; everything else is not:

```python
# packages — CHECKED (engine.py:751-760)
r = backend.exec(vm_id, f"sudo ... apt-get install -y {pkg_list}")
if r.exit_code != 0:
    log.error(...); raise RuntimeError(...)

# installs — NOT checked (engine.py:769-774)
for inst in node._installs:
    backend.upload(vm_id, inst.src, remote_src)
    backend.exec(vm_id, inst.install_cmd)        # ExecResult discarded
    if inst.verify_cmd:
        backend.exec(vm_id, inst.verify_cmd)     # ExecResult discarded

# services — NOT checked (engine.py:783-787)
for svc in node._services:
    if svc.enabled:
        backend.exec(vm_id, f"sudo systemctl enable {svc.name}")   # discarded
    start_cmd = svc.start_cmd or f"sudo systemctl start {svc.name}"
    backend.exec(vm_id, start_cmd)               # discarded

# powershell — NOT checked (engine.py:761-762)
for ps_cmd in node._powershell_commands:
    backend.exec(vm_id, f"powershell -Command {ps_cmd}")           # discarded
```

`backend.exec` returns an `ExecResult(exit_code, stdout, stderr)` (`types.py:86-90`) — the information is right there and thrown away for installs, verifies, services, and powershell.

### Why it's wrong
The whole reason `install()` accepts a `verify_cmd` (`dependencies.py:31-39`, `InstallSpec.verify_cmd`) is to **assert the install worked**. Running the verify command and ignoring whether it passed makes the parameter theater. Same for `systemctl start`: a unit that fails to start returns non-zero, and the engine sails past it to mark the node `RUNNING` (`engine.py:789-791`).

### Blast radius (a real failure a user hits)
A lab installs a custom agent: `target.install(name="sensor", src="./sensor.tar.gz", install_cmd="./install.sh", verify_cmd="sensor --version")`. The tarball is corrupt, `install.sh` exits 1, `sensor --version` exits 127 (not found). The engine logs nothing about it and reports a **green deploy**. The user's `verify()` then fails deep in the lab, or worse, the sensor silently isn't running during an exercise and the data is wrong. The failure surfaces far from its cause — the exact opposite of "fail fast at the broken step."

A subtler one on EC2: `systemctl start myunit` fails because of a typo'd ExecStart. Node goes RUNNING, the dependent node's configure-fn assumes the unit is up, and the whole range is quietly broken while the CLI says everything's fine.

### Background a learner needs
On a POSIX system a process exit code of 0 means success; non-zero means failure (`systemctl start` returns non-zero if the unit fails to activate; a missing binary yields 127; `set -e`-style scripts propagate the first failure). An orchestrator that runs remote commands has exactly one job at each step: **check the code and stop if it's bad**, with enough of stderr to diagnose. The apt path already models this (`engine.py:755-760`); the fix is to make the other paths consistent with it.

---

## D1: Failure policy — raise-on-nonzero vs collect-and-report vs opt-out

| Option | Behavior | Pros | Cons |
|---|---|---|---|
| **A — Raise on first nonzero, mirroring the apt path** ✅ | each non-apt `exec` becomes `r = exec(...); if r.exit_code: log.error(stderr); raise RuntimeError(...)`; `cleanup_on_fail` (default) tears down | Consistent with the one path that's already right (`engine.py:755-760`); fails at the broken step with that step's stderr; minimal new concepts; existing BaseException cleanup handles teardown | A multi-package-of-installs deploy stops at the first failure rather than reporting all — acceptable, the first failure is usually the cause |
| B — Collect all failures, report at end | accumulate `(step, ExecResult)` failures, raise an aggregate after the loop | "See everything wrong at once" | Later steps run against a half-broken node (install failed but we still `systemctl start` something depending on it) → cascading noise that obscures the root failure; more code; diverges from the apt path |
| C — Per-command `check=` opt-out (default check=True) | every dep step takes an optional "ignore failure" flag | Maximum flexibility | New API surface (`install(..., check=False)`, `service(..., check=False)`) nobody requested; YAGNI; the real need is "don't silently pass," and A delivers that |
| D — Leave as-is, just log nonzero | log a warning, keep going | One-line change | This is the bug with a log line — node still goes RUNNING on a failed install; H2 not closed |

### ✅ Recommendation: **A**
The codebase already contains the correct pattern in the apt branch; the fix is to **extend that pattern**, not invent a new policy. Raise at the first failure with the step's stderr (trimmed, like apt does at `engine.py:759`). `verify_cmd` failing must raise — that's the entire point of declaring a verify. `systemctl enable`/`start` failing must raise. This keeps the engine's contract simple: "every dependency step either succeeds or the deploy fails loudly," and reuses the BaseException-safe cleanup that already exists.

---

## D2: A shared check helper vs inline checks

Five call sites (install_cmd, verify_cmd, systemctl enable, service start, powershell) need the same "run + raise on nonzero + log stderr" shape.

| Option | Pros | Cons |
|---|---|---|
| Inline `if r.exit_code: raise` at each site (like apt) | Explicit; matches existing apt style exactly; easy to read each step's error message | ~5 near-identical 3-line blocks |
| **A — One tiny helper `_run_checked(backend, vm_id, cmd, what)`** ✅ | DRY; one place to format the "step `what` failed (rc=N): stderr" message; the apt path can adopt it too for consistency | A small indirection — but it's a single-responsibility 4-line function, not an abstraction |
| B — Push the check into the backend (`exec` raises on nonzero) | callers never forget | Breaks `exec`'s contract (callers like readiness probes and `run(check=False)` NEED the raw code); would ripple everywhere; wrong layer |

### ✅ Recommendation: **A** (a 4-line `_run_checked` in `engine.py`)
Per the project's own rule ("three similar lines is better than a premature abstraction" — `development-rules.md`), five identical blocks crosses the line into justified extraction. Keep it a module-private helper that takes a human label (`"install sensor"`, `"start elasticsearch"`) for the error message, and have the apt path call it too so all six steps report failures identically. This is not premature — it removes real duplication that exists *right now*.

---

## D3: Powershell path — same treatment?

`node._powershell_commands` (`engine.py:761-762`) runs `powershell -Command <cmd>` and discards the result. Windows nodes are real (`OSType.WINDOWS`), but no integration coverage exists for them yet.

- **Recommendation**: apply the same `_run_checked` (a failed powershell command is still a failed deploy step). It's one more call site, zero extra risk, and keeps the rule uniform. If Windows exec semantics turn out to need special handling (e.g. `$LASTEXITCODE` vs process exit), that's a Windows-phase concern — but *silently ignoring* the code is wrong regardless of OS.

---

## Recommended shape (summary)
1. Add `_run_checked(backend, vm_id, cmd, what)` to `engine.py`: runs `exec`, and on non-zero `log.error` the rc+stderr and `raise RuntimeError(f"{what} failed (rc=...): {stderr[:300]}")`.
2. Route install_cmd, verify_cmd, `systemctl enable`, service start, and powershell through it (`engine.py:772, 774, 785, 787, 762`).
3. Convert the apt branch (`engine.py:751-760`) to call `_run_checked` too, so all dependency steps fail identically.
4. No change to `cleanup_on_fail` — the existing default (`engine.py:236-239`) already tears down on the raised error.

## Test strategy
**Gate 1 (unit, MockBackend):** MockBackend must be able to return a non-zero `ExecResult` for a scripted command (it currently returns canned success — see M12 in the findings).
- install_cmd returns rc=1 → deploy raises `RuntimeError` naming the install; node never reaches RUNNING; with `cleanup_on_fail` the partial range tears down.
- verify_cmd returns rc=1 → same.
- `systemctl start` returns rc=1 → same.
- happy path (all rc=0) → node reaches RUNNING, no raise (regression guard).
- error message includes trimmed stderr (assert substring).

**Gate 2 (EC2):** Lightweight but worthwhile — one integration test with an `install()` whose `verify_cmd` is `false` (guaranteed rc=1) asserting the SDK `deploy()` raises and the range is cleaned up. Confirms the real LibvirtBackend `exec` exit-code plumbing matches MockBackend's. Can piggyback on an existing topo fixture.

## Unresolved questions
- Should `verify_cmd` failure and `install_cmd` failure produce distinguishable error types, or is one `RuntimeError` with a clear `what` label enough? (Recommend the label — no caller branches on the type today.)

## Progress Log
- 2026-06-09: Read `engine.py:725-792` (`_inject_dependencies`), `dependencies.py` (`InstallSpec`/`ServiceSpec`), `types.py` (`ExecResult`). Confirmed apt path checks (`:755-760`) while install/verify/service/powershell discard `ExecResult`. Wrote options.

## Resolution
_(pending user review)_
