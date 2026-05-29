# SDK Polish: Dependency Audit + ImageBuilder + Doc Update
**Created**: 2026-05-29
**Status**: Backlog

## Related Issues
- **SDK Reference**: `20260527-3-sdk-api-reference.md`
- **Parent Plan**: `20260527-1-vm-testbed-platform-design.md`

## Goal
Close the ~15% gap between SDK doc and reality. Find and fix register-only methods, implement ImageBuilder, update docs.

## Tasks

### 1. Dependency execution audit
Audit `engine.py`'s deploy path to confirm every `DependencyMixin` method's registered specs are consumed:
- `packages()` — consumed via `_inject_dependencies` (apt-get install)
- `service()` — consumed via `_inject_dependencies` (systemctl)
- `powershell()` — likely register-only (no Windows engine path yet)
- `install()` — verify consumed
- `file()` — verify consumed
- `user()` — verify consumed
- `run_on_boot()` — verify consumed
- `@configure` — verify consumed

Flag any that register but never execute. Either wire them into the engine or raise NotImplementedError so users don't silently lose config.

### 2. Implement ImageBuilder.build()
Currently raises NotImplementedError at `images.py:80`. Implement:
- Boot base image
- Apply registered deps (packages, run commands)
- Snapshot the VM
- Register the snapshot as a new image in the registry
- Destroy the build VM

### 3. Update SDK doc
- Topology() constructor: document `backend=`, `db=`, `container_backend=` params
- Container nodes: document `container=` kwarg on `node()`
- InjectMethod enum: document or mark internal
- StateDB: document or mark internal
- Add implementation status header at top

## Success Criteria
- [ ] Every DependencyMixin method either executes during deploy or raises NotImplementedError
- [ ] ImageBuilder.build() works end-to-end
- [ ] SDK doc matches implementation
- [ ] Unit tests for any new/fixed execution paths
