# rangectl Development Rules

> **Audience: coding agents.** Token-optimized reference for the Python test harness (`testharness/`). For design rationale see `scratch/issues/20260309-2-test-harness-phased-plan.md`.

## TDD Workflow (Non-Negotiable)

Every code change follows this exact sequence:

1. **Write unit tests first** in `tests/unit/` — tests define expected behavior
2. **Run `pytest tests/unit`** — new tests FAIL (red)
3. **Write the implementation**
4. **Run `pytest tests/unit`** — all tests PASS (green)
5. **Run full unit suite** — no regressions in existing tests
6. **Write integration tests** in `tests/integration/` (if the change touches libvirt, networking, or SSH)
7. **Run `pytest tests/integration`** on EC2 box — PASS
8. **Commit**

Never skip step 1. Never commit with failing tests.

## Test Layers

### Unit Tests (`tests/unit/`)
- Run anywhere, no infrastructure needed
- Use `MockBackend` — records calls, returns canned responses, no real VMs
- Use in-memory SQLite — `StateDB(db_path=":memory:")`
- Fast (seconds), deterministic
- Every public method gets at least one unit test
- Run on every change: `pytest tests/unit`

### Integration Tests (`tests/integration/`)
- Require KVM host (EC2 box)
- Test real VMs, real bridges, real SSH connections
- Slow (minutes)
- Required for any code that touches libvirt, networking, or SSH
- Must pass before a phase is deemed complete: `pytest tests/integration`

### Running Tests

```bash
pytest tests/unit                  # unit only — run on every change
pytest tests/integration           # integration only — run on EC2
pytest                             # full suite
pytest tests/unit -x               # stop on first failure (TDD mode)
pytest tests/unit -k "test_dag"    # specific module
```

## Debugging Rules

When a test fails or a bug is found:

1. **Stop.** Do not make code changes yet.
2. **Create an issue** in `scratch/issues/` — get the next prefix from `~/.claude/scripts/next-prefix scratch/issues`
3. **Isolate the root cause** — add logs or write a minimal reproducing test in `tests/unit/`
4. **Document findings** in the issue — update continuously
5. **Implement the fix** only after root cause is identified
6. **Run full test suite** — confirm fix and no regressions
7. **Update the issue** with resolution

### Do NOT:
- Make code changes before understanding root cause
- Try multiple fixes hoping one works
- Add complexity without evidence
- Assume anything without checking

## Code Standards

- No premature abstractions — three similar lines is better than a premature abstraction
- No features beyond what the task requires
- No comments unless the WHY is non-obvious
- No error handling for scenarios that can't happen
- Clean, readable code with descriptive variable names
- Functions focused on single responsibilities

## Package Manager Abstraction

The SDK uses `packages()` — not `apt()`, `pip()`, `choco()`. The engine resolves the package manager from the image/OS at deploy time. Users declare what they want, not how to install it.

## Naming Conventions

- Topology resources prefixed with topology name: `{topo-name}-{resource}`
- Management bridges: `rangectl-mgmt-{topo-name}`
- VM names: `{topo-name}-{node-name}`
- Topology bridges: `{topo-name}-br{N}`

## State Management

- All state in single SQLite DB (`~/.rangectl/rangectl.db`)
- Images: metadata in SQLite, qcow2 files in `~/.rangectl/images/`
- Disk overlays: COW qcow2 backed by read-only base images — no full copies ever

## Git Workflow

- One commit per logical change
- Tests must pass before committing
- Never commit with `--no-verify`
- Commit messages: concise, focus on WHY not WHAT

## Issue Tracking

All work must be grounded in issues in `scratch/issues/`. Get the filename prefix by running `~/.claude/scripts/next-prefix scratch/issues`, then append `-feature-name.md`. Update issues continuously as work progresses.
