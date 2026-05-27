# Phase 4-5: Image Registry + Dependency Injection (Gate 1 Only)
**Created**: 2026-05-27
**Status**: Complete (Gate 1)
**Phase**: 4-5

## Related Issues
- **Plan**: `20260527-1-vm-testbed-platform-design.md` — Phase 4 + Phase 5
- **Orchestrator**: `20260527-5-rangectl-orchestrator.md`
- **Phase 3**: `20260527-8-phase3-state-machine-dag.md`

## Goal
Implement ImageRegistry (add/remove with file copy), dependency injection in the engine (packages, installs, configure, services via MockBackend), and DependencySet apply integration. Gate 2 deferred.

## What Exists (from Phase 3, commit e0e7a82)
- `Engine.deploy()` / `destroy()` — fully working with MockBackend
- `Engine._inject_dependencies()` — stub that just transitions LINKED → RUNNING
- `ImageRegistry` — `list()`, `get()`, `exists()` work via StateDB; `add()` and `remove()` raise NotImplementedError
- `ImageBuilder` — stub, `build()` raises NotImplementedError
- `DependencyMixin` — all registration methods work (packages, install, file, user, service, configure, apply)
- `DependencySet` — inherits DependencyMixin, `apply()` merges all fields
- `StateDB` — `add_image`, `remove_image`, `get_image`, `list_images`, `image_exists` all implemented
- `MockBackend` — has `exec()`, `upload()` that record calls and return canned results
- 64 unit tests pass

## Scope — What to Implement

### 1. ImageRegistry.add() (images.py)
- Copy the qcow2 file to `self._storage_path / name.qcow2` (use shutil.copy2)
- Calculate file size in MB
- Call `self._db.add_image(name, dest_path, inject, os_type, size_mb)`
- For unit tests: use a tmp directory as storage_path, create a small dummy file

### 2. ImageRegistry.remove() (images.py)
- Get image record from DB to find path
- Delete the qcow2 file from storage
- Call `self._db.remove_image(name)`

### 3. Engine._inject_dependencies() — real implementation (engine.py)
Replace the stub. For each node, execute in order:
1. **packages**: `backend.exec(vm_id, "apt-get install -y pkg1 pkg2 ...")` for Linux
2. **files**: `backend.upload(vm_id, src, dst)` for each registered file
3. **installs**: `backend.upload(vm_id, inst.src, "/tmp/{inst.name}")` then `backend.exec(vm_id, inst.install_cmd)`, optionally verify with `backend.exec(vm_id, inst.verify_cmd)`
4. **configure fns**: call each `fn(live_node)` — needs a LiveNode handle with exec/upload
5. **services**: `backend.exec(vm_id, f"systemctl enable {svc.name}")` if enabled, `backend.exec(vm_id, svc.start_cmd or f"systemctl start {svc.name}")`
6. Transition LINKED → RUNNING after all deps injected

Key detail: configure functions receive a `LiveNode` handle. For the engine, create a temporary LiveNode that routes exec/upload through the backend using the vm_id.

### 4. LiveNode.exec() and LiveNode.upload() (topology.py)
Currently raise NotImplementedError. For the engine to inject deps via configure functions, LiveNode needs a backend reference. Options:
- Add `_backend` and `_vm_id` fields to LiveNode (set during deploy)
- LiveNode.exec() calls `self._backend.exec(self._vm_id, cmd)`
- LiveNode.upload() calls `self._backend.upload(self._vm_id, src, dst)`

### 5. DependencySet integration test
Verify that `node.apply(dep_set)` merges packages/installs/configure/services, and when the engine deploys, all merged deps are injected in order.

## Unit Tests to Write

### tests/unit/test_images.py
- `test_registry_add_copies_file` — add an image, verify file exists in storage dir
- `test_registry_add_records_in_db` — after add, db.get_image returns record
- `test_registry_remove_deletes_file` — remove, file gone from storage
- `test_registry_remove_deletes_from_db` — remove, db.get_image returns None
- `test_registry_list` — add 2 images, list returns both
- `test_registry_exists` — true after add, false after remove
- `test_image_builder_collects_deps` — packages/run/configure are collected (no build — that needs real VMs)

### tests/unit/test_dependencies.py
- `test_dependency_set_packages` — register packages, verify stored
- `test_dependency_set_install` — register install, verify InstallSpec stored
- `test_dependency_set_configure` — @dep_set.configure registers function
- `test_dependency_set_service` — register service, verify ServiceSpec stored
- `test_dependency_set_apply_merges` — apply a dep_set to a node, verify all fields merged
- `test_dependency_set_file` — register file, verify stored
- `test_apply_ordering_preserved` — apply two dep_sets, packages from both in order

### tests/unit/test_engine.py (extend existing)
- `test_inject_dependencies_packages` — deploy with node.packages(["nginx"]), verify backend.exec called with apt-get install
- `test_inject_dependencies_files` — deploy with node.file(), verify backend.upload called
- `test_inject_dependencies_install` — deploy with node.install(), verify upload + exec
- `test_inject_dependencies_configure` — deploy with @node.configure, verify function called with LiveNode
- `test_inject_dependencies_services` — deploy with node.service(), verify exec called
- `test_inject_dependencies_ordering` — packages before installs before configure before services
- `test_inject_with_dependency_set` — node.apply(depset), deploy, verify all merged deps injected

## Important Notes
- Use tmp directories for ImageRegistry tests (pytest tmp_path fixture)
- LiveNode needs backend+vm_id wiring for configure functions to work
- For configure fn tests: the fn receives a LiveNode and calls exec/upload on it — verify those calls hit MockBackend
- All 64 existing tests must still pass
- ImageBuilder.build() stays as NotImplementedError (needs real VMs)
- Windows powershell injection: just backend.exec with powershell command, no special handling needed for unit tests

## Success Criteria
- [x] ImageRegistry.add() and remove() implemented
- [x] Engine._inject_dependencies() fully implemented
- [x] LiveNode.exec() and upload() work via backend
- [x] DependencySet apply + injection works end-to-end
- [x] All unit tests pass (new + existing 64)
- [x] Zero failures, zero skips
- [x] Committed to git

## Gate 1 Output
```
============================= test session starts ==============================
platform darwin -- Python 3.9.6, pytest-8.4.1, pluggy-1.6.0
collected 91 items

tests/unit/test_dag.py ......                                            [  6%]
tests/unit/test_dependencies.py .......                                  [ 14%]
tests/unit/test_deploy.py ............                                   [ 26%]
tests/unit/test_engine.py ....                                           [ 30%]
tests/unit/test_images.py .........                                      [ 40%]
tests/unit/test_inject.py ............                                   [ 52%]
tests/unit/test_networking.py .......                                    [ 60%]
tests/unit/test_readiness.py .....                                       [ 65%]
tests/unit/test_state.py ..........                                      [ 76%]
tests/unit/test_state_machine.py .................                       [ 95%]
tests/unit/test_types.py ....                                            [100%]

============================== 91 passed in 0.24s ==============================
```

64 prior tests + 27 new (9 images, 7 dependencies, 11 inject) = 91 passed.

## Gate 2
Deferred — EC2 bare-metal blocked.

## Resolution
ImageRegistry.add/remove copy/delete qcow2 files and sync StateDB. LiveNode
gained optional `backend`/`vm_id` for routing exec/upload through Backend.
Engine._inject_dependencies executes packages → files → installs → configure →
services in order, passing a backend-bound LiveNode to configure callables.
DependencySet.apply merges into nodes and all merged deps are injected.
