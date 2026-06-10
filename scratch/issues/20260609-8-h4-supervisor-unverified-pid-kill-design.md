# Design: H4 — Supervisor Kills an Unverified PID as Root — Options & Recommendation
**Created**: 2026-06-09
**Status**: In Progress (awaiting user review)

## Related Issues
- **Parent**: `20260609-3-architecture-code-review-findings.md` — H4 (this design closes it)
- **Where the code lives**: `20260529-7-phase8-10-namespace-isolation-gate1.md` (per-range libvirtd supervisor + PID-ns teardown), `20260603-2-phase18-security-hardening.md` (root-safety hardening)
- **Memory**: `project_range-cleanup-must-be-scoped.md` — "never host-wide pkill; only touch this range's processes" — H4 is a hole in exactly that invariant

## Problem

### What the code does today
A range's libvirtd runs as PID 1 of its own PID namespace; the supervisor persists the **host-side** PID of the `unshare` wrapper to `<range_dir>/<name>/range.json` (`supervisor.py:194-202`, field `"pid"`). Teardown reads that PID back and signals it:

```python
# destroy_range (supervisor.py:234-245)
state = json.loads(state_file.read_text())
_terminate(state["pid"])

# _terminate (supervisor.py:216-231)
targets = _child_pids(wrapper_pid) + [wrapper_pid]
survivors = [pid for pid in targets if _signal(pid, signal.SIGTERM)]
...
time.sleep(TERM_GRACE_SECONDS)
for pid in survivors:
    _signal(pid, signal.SIGKILL)              # os.kill(pid, SIGKILL)
```

`_signal` is a bare `os.kill(pid, sig)` (`supervisor.py:207-213`). The PID comes straight from a JSON file on disk and is **never checked against what's actually running at that PID**. rangectl runs its privileged ops as root (it does `ip netns`, `unshare`, bind-mounts), so this `os.kill` is a **root-privileged kill of whatever process currently holds that PID number**.

### Why it's wrong — PID reuse
PIDs are recycled. The kernel hands out PIDs from a bounded space and wraps around; after a process exits, its PID becomes available and will eventually be reassigned to an unrelated process. The stored PID is only meaningful **as long as the original libvirtd is still alive**. Two ways it goes stale:

- **Host reboot.** `range.json` lives on disk under `/ranges` and survives a reboot; the libvirtd it names does not. After reboot, PID 4242 (once our libvirtd) is now, say, `sshd` or a database. `destroy_range`/`Range.cleanup` reads `pid=4242`, finds its "children," and SIGTERM→SIGKILLs them as root. We just killed an arbitrary root process — potentially the box's sshd, locking the operator out, or another tenant's workload.
- **libvirtd already died + PID wrapped.** Long-lived host, our libvirtd crashed hours ago, the PID got reused by something else. Same outcome.

This directly violates the scoped-cleanup rule (`project_range-cleanup-must-be-scoped.md`): cleanup must touch *only this range's* processes. Killing by a stale stored PID with no identity check is the unscoped-kill failure mode, just arrived at via reuse instead of `pkill`.

`Range.connect` (`topology.py:676-677`) already guards its own use of the PID with `_pid_alive(info["pid"])` — but `_pid_alive` only checks *existence* (`os.kill(pid, 0)`), not *identity*. A reused PID is "alive," so even that check would happily proceed to operate on the wrong process. Teardown does no check at all.

### Background a learner needs
`os.kill(pid, 0)` tests whether *a* process with that PID exists — it cannot tell you *which* process. To safely act on "the process I started earlier," you must verify identity, and the robust signals are:
- **`/proc/<pid>/cmdline`** — the argv of the running process; our wrapper is `unshare --pid --fork ... /bin/bash -c <launch script>`, a recognizable signature.
- **Start time** (`/proc/<pid>/stat` field 22, in clock ticks since boot) — combined with the PID, a `(pid, starttime)` tuple is unique for the life of the boot: a reused PID has a *different* start time, so a mismatch means "not my process." This is the classic safe-kill primitive (systemd, supervisors use it).
- **pidfd** (`pidfd_open`, kernel ≥5.3) — a file descriptor that refers to a *specific* process; signalling via `pidfd_send_signal` cannot hit a reused PID because the fd is bound to the original process. The catch: you must hold the pidfd from the time you created the process, which we don't across a separate teardown invocation — so pidfd helps a long-running supervisor, not a fresh `rangectl destroy` process.

---

## D1: How to verify identity before signalling

| Option | Mechanism | Pros | Cons |
|---|---|---|---|
| **A — `(pid, starttime)` tuple, persisted + verified** ✅ | record `/proc/<pid>/stat` field 22 in `range.json` at create; before any signal, re-read it and require it to match | Robust against reuse across reboots AND mid-boot wrap (start time differs); cheap (one file read); the canonical safe-kill primitive; no new kernel deps | One extra field in `range.json`; one parse helper; legacy `range.json` files lack the field (handle as "unverifiable → skip + warn") |
| B — `/proc/<pid>/cmdline` substring check | match the launch-script signature (`unshare ... libvirtd ... <range name>`) | Human-readable; can even confirm it's *our* range by name in the argv | cmdline can be spoofed/duplicated; a *different* rangectl range's wrapper also matches "unshare...libvirtd"; substring matching is fuzzier than an exact starttime; still better than nothing |
| C — pidfd (`pidfd_open` + `pidfd_send_signal`) | fd bound to the process | Immune to reuse by construction | Only safe if you held the fd since spawn; a separate `destroy`/`cleanup` process re-opens by PID and races the very window we're closing; kernel ≥5.3 only; doesn't fit our spawn-then-separately-teardown model |
| D — cgroup membership | kill via the range's cgroup (`cgroup.kill`) instead of by PID | Already used for the cgroup'd path (`cgroup.kill+drain`, `supervisor`/`cgroup.py`); kernel guarantees only that cgroup's tasks die; intrinsically scoped | Only ranges deployed *with resources* get a cgroup (`engine.py:419-421` — cgroup is conditional on `self._resources`); ranges without resource limits have no cgroup to kill through, so this can't be the sole mechanism |

### ✅ Recommendation: **A**, with **D as the preferred path when a cgroup exists**
`(pid, starttime)` is the minimal, dependency-free fix that actually defeats PID reuse: persist field 22 of `/proc/<pid>/stat` at `create_range`, and in `_terminate` re-read it and **only signal if it matches**. On mismatch (or missing starttime in a legacy `range.json`, or `/proc/<pid>` gone): **skip the kill, log a clear warning** ("range %s: stored pid %s no longer matches the libvirtd we started — refusing to signal; continuing teardown of netns/dirs"). Where a range has a cgroup, prefer cgroup-scoped kill (D) and keep (A) as the guard for the wrapper PID — the cgroup gives a kernel-enforced "only this range's tasks" guarantee that PID-signalling can't.

`_pid_alive` in `topology.py:24-34` should be upgraded the same way (or gain a sibling `_pid_is_ours(pid, starttime)`) so `Range.connect`'s liveness check (`topology.py:676`) and `_range_status` (`topology.py:51`) also reject reused PIDs — otherwise a reused PID reads as a "running" range.

---

## D2: Failure semantics on mismatch — skip vs error

| Option | Behavior | Pros | Cons |
|---|---|---|---|
| **A — Skip the signal, warn, continue tearing down the rest** ✅ | don't kill; still remove netns, mgmt network, range dir, cgroup | Teardown of a rebooted/orphaned range still cleans the *safe* artifacts (netns/dirs vanish on reboot anyway, but the dir + DB rows must go); never kills a stranger; idempotent | The (already-dead) original libvirtd isn't "killed" — but it's already gone, so nothing to do |
| B — Raise / abort teardown on mismatch | stop and surface an error | Loud | Leaves the range dir + DB rows + subnet allocation stranded because we refused to proceed; turns a safe situation (stale PID) into a stuck range needing manual `cleanup` |
| C — Kill anyway, "probably fine" | current behavior | — | This is the bug |

### ✅ Recommendation: **A**
A stale PID means "the thing I would have killed is already dead" — the correct response is *don't signal, but finish cleaning up the inert artifacts*. Skip+warn keeps teardown idempotent and never harms a bystander. This pairs with the existing `destroy_range` "no state file → no-op" tolerance (`supervisor.py:240-242`).

---

## Recommended shape (summary)
1. `supervisor.py`: add `_proc_starttime(pid) -> int | None` (parse field 22 of `/proc/<pid>/stat`; None if `/proc/<pid>` absent).
2. `create_range`: capture `starttime = _proc_starttime(proc.pid)` and persist it in `range.json` alongside `pid`.
3. `_terminate`/`destroy_range`: before signalling, re-read the live starttime and require `== stored`. On mismatch/missing/None → skip all `os.kill`s, `log.warning` the refusal, and proceed to netns/network/dir teardown. When the range has a cgroup, kill via the cgroup and use the tuple-check only as the guard.
4. `topology.py`: make `_pid_alive` (or a new `_pid_is_ours`) identity-aware so `connect`/`_range_status` don't treat a reused PID as a live range; thread the persisted starttime through `_read_range_json`.
5. Legacy `range.json` without a `starttime` field → treated as unverifiable → skip-kill + warn (never blind-kill).

## Test strategy
**Gate 1 (unit):**
- `_proc_starttime(os.getpid())` returns a stable int; returns `None` for a definitely-absent PID.
- `_terminate` with a stored starttime that **mismatches** the live one issues **zero** `os.kill` signals (monkeypatch `os.kill` to record) and still calls the netns/dir teardown.
- `_terminate` with a **matching** tuple signals as today.
- `range.json` missing `starttime` → skip-kill + warn path.
- `_pid_is_ours` returns False for a live-but-mismatched-starttime PID (the reuse case), True for the real one.

**Gate 2 (EC2):** Recommended (light). Deploy a range, capture its `range.json`, then **simulate reuse**: rewrite `range.json`'s `pid` to a known-unrelated root PID (e.g. a sentinel `sleep infinity` we started) with a deliberately wrong `starttime`, run `rangectl destroy`, and assert the sentinel is **still alive** afterward while the netns/dir/DB rows are gone. This is the only test that proves we don't kill a bystander on the real teardown path. Must run scoped (don't touch other ranges/agents per the cleanup memory).

## Unresolved questions
- Adopt cgroup-scoped kill (D) as the *primary* teardown for cgroup'd ranges in this fix, or keep PID-signalling primary and add the tuple-guard only (smaller diff)? Recommendation: tuple-guard everywhere now (closes the CVE-class bug); migrate cgroup'd ranges to cgroup-kill as a follow-up if desired.
- `Range.cleanup` (`topology.py:798-829`) calls `destroy_range` too — it inherits the fix automatically, but confirm the orphan break-glass path's intent is also "never blind-kill" (it is, per the cleanup memory).

## Progress Log
- 2026-06-09: Read `supervisor.py` (`create_range` persistence, `_terminate`, `_signal`, `destroy_range`), `topology.py` (`_pid_alive`, `_range_status`, `connect` liveness, `cleanup`). Confirmed the stored PID is signalled with zero identity verification and that `_pid_alive` checks existence only. Cross-checked the scoped-cleanup memory. Wrote options.

## Resolution
_(pending user review)_
