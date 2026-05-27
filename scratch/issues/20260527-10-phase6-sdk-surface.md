# Phase 6: SDK API Surface (Gate 1 Only)
**Created**: 2026-05-27
**Status**: Complete (Gate 1)
**Phase**: 6

## Related Issues
- **Plan**: `20260527-1-vm-testbed-platform-design.md` — Phase 6
- **Orchestrator**: `20260527-5-rangectl-orchestrator.md`
- **Phase 4-5**: `20260527-9-phase4-5-images-dependencies.md`

## Goal
Wire the public SDK surface: Topology.deploy() delegates to Engine, export/import YAML, snapshot/restore, link toggle, structured log access, LiveNode.template(). Gate 2 deferred.

## What Exists (commit b9be02f)
- `Engine.deploy(topo) → Range` and `Engine.destroy(topo)` — fully working
- `Topology.deploy()` — raises NotImplementedError (not wired to Engine)
- `Topology.export()` / `from_yaml()` — raise NotImplementedError
- `Topology.destroy()` — raises NotImplementedError
- `Range.snapshot()` / `restore()` / `logs()` — raise NotImplementedError
- `LiveNode.template()` / `logs()` / `snapshot()` / `restore()` — raise NotImplementedError
- `Link.down()` / `up()` — raise NotImplementedError
- `list_topologies()` — works (uses StateDB directly)
- `Range.__exit__` calls `topology.destroy()` — which raises NotImplementedError
- 91 unit tests pass

## Scope — What to Implement

### 1. Topology.deploy() → wire to Engine (topology.py)
Topology needs access to a Backend and StateDB to create an Engine:
- Add optional `backend` and `db` params to `Topology.__init__()` (defaults None)
- In `deploy()`: create Engine with backend/db (or defaults), call `engine.deploy(self)`
- Store `self._engine` for use by `destroy()`
- For unit tests: pass MockBackend and in-memory StateDB
- For real use: defaults create LibvirtBackend and file-based StateDB (but don't implement LibvirtBackend — that's integration)

### 2. Topology.destroy() (topology.py)
Call `self._engine.destroy(self)` if engine exists.

### 3. Topology.export(path) (topology.py)
Serialize topology to YAML. Schema:
```yaml
name: pentest-lab
nodes:
  - name: router
    image: vyos-1.4
    vcpu: 2
    memory: 2048
    os: linux
    depends_on: []
    interfaces:
      - name: eth0
        ip: "10.0.1.1"
        cidr: "24"
links:
  - node_a: router
    iface_a: eth0
    ip_a: "10.0.1.1/24"
    node_b: target
    iface_b: eth0
    ip_b: "10.0.1.2/24"
```
Use `yaml.dump()` — pyyaml is already a dependency.

### 4. Topology.from_yaml(path) (topology.py)
Deserialize YAML back to a Topology object. Must reconstruct nodes, interfaces, IPs, links, depends_on references.

### 5. Range.snapshot(name) / restore(name) (topology.py)
- `snapshot`: for each LiveNode, call `backend.snapshot(vm_id, name)`, save to DB
- `restore`: for each LiveNode, call `backend.restore(vm_id, snapshot_id)`, lookup from DB
- Need access to backend — Range needs backend reference (add to __init__ or get from LiveNodes)

### 6. Range.logs(level=None) (topology.py)
Call `db.get_logs(topology_name, level=level)`. Range needs a DB reference.

### 7. LiveNode.logs(level=None) (topology.py)
Call `db.get_logs(topology_name, node_name=self.name, level=level)`. LiveNode needs a DB reference.

### 8. LiveNode.template(src, dst, vars=None) (topology.py)
- Read the template file at `src`
- Render with Jinja2: `jinja2.Template(content).render(vars or {})`
- Upload rendered content to the node via `self.upload()`
- Write rendered content to a temp file, upload, clean up

### 9. LiveNode.snapshot(name) / restore(name) (topology.py)
- `snapshot`: `backend.snapshot(vm_id, name)`, save to DB
- `restore`: `backend.restore(vm_id, snapshot_id)`, lookup from DB

### 10. Link.down() / up() (topology.py)
For MockBackend unit tests: update `self._is_up`, call `backend.delete_bridge(bridge)` for down, `backend.create_bridge(bridge)` for up. Links need a backend + bridge name reference.
- Add `_backend` and `_bridge_name` optional fields to Link
- Engine._wire_link() sets these after creating the bridge
- Link.down/up log events to DB

### 11. Range context manager fix
`Range.__exit__` currently calls `topology.destroy()`. With the engine wired, this should work. But Range also needs to clean up its own state. Verify this works.

## Unit Tests to Write

### tests/unit/test_topology.py
- `test_topology_deploy_returns_range` — Topology with MockBackend, deploy returns Range with LiveNodes
- `test_topology_deploy_context_manager` — `with topo.deploy() as rng:` works, destroy on exit
- `test_topology_node_interface_access` — `node.eth0`, `node.eth1` return InterfaceSpec
- `test_topology_node_interface_ip_binding` — `node.eth0["10.0.1.1/24"]` returns spec with IP
- `test_topology_export_yaml` — export to tmp file, verify YAML content
- `test_topology_from_yaml` — export then import, verify round-trip (nodes, links, IPs match)
- `test_topology_export_without_deploy` — export works before deploy

### tests/unit/test_range.py (or extend test_deploy.py)
- `test_range_getitem` — `rng["node"]` returns LiveNode
- `test_range_link_lookup` — `rng.link("a", "b")` returns Link
- `test_range_snapshot_restore` — snapshot calls backend.snapshot for all nodes, restore calls backend.restore
- `test_range_logs` — returns logs from DB
- `test_live_node_exec` — calls backend.exec
- `test_live_node_upload` — calls backend.upload
- `test_live_node_template` — renders Jinja2 and uploads
- `test_live_node_logs` — returns node-filtered logs from DB
- `test_live_node_snapshot_restore` — per-node snapshot/restore
- `test_link_down_up` — toggles link state, calls backend

## Important Notes
- Don't implement LibvirtBackend — use MockBackend for all tests
- Topology(name, backend=MockBackend(), db=StateDB(":memory:")) for unit tests
- Jinja2 is already a pip dependency
- YAML export should work WITHOUT deploying (R11)
- Keep the SDK surface clean — no internal details leaked
- All 91 existing tests must pass
- Fix Range.__exit__ → topology.destroy() chain to work with Engine

## Success Criteria
- [x] Topology.deploy() wired to Engine
- [x] Export/import YAML round-trip works
- [x] Range snapshot/restore works
- [x] Range/LiveNode logs work
- [x] LiveNode.template() works
- [x] Link.down()/up() work
- [x] All unit tests pass (new + existing 91)
- [x] Zero failures, zero skips
- [ ] Committed to git

## Gate 1 Output
```
$ pytest tests/unit -v
collected 117 items
...
tests/unit/test_topology.py::test_topology_deploy_returns_range PASSED
tests/unit/test_topology.py::test_topology_deploy_without_backend_raises PASSED
tests/unit/test_topology.py::test_topology_deploy_context_manager_destroys PASSED
tests/unit/test_topology.py::test_topology_destroy_via_engine PASSED
tests/unit/test_topology.py::test_topology_destroy_without_deploy_raises PASSED
tests/unit/test_topology.py::test_topology_node_interface_access PASSED
tests/unit/test_topology.py::test_topology_node_interface_ip_binding PASSED
tests/unit/test_topology.py::test_topology_export_yaml PASSED
tests/unit/test_topology.py::test_topology_export_without_deploy PASSED
tests/unit/test_topology.py::test_topology_from_yaml_roundtrip PASSED
tests/unit/test_topology.py::test_topology_from_yaml_no_links PASSED
tests/unit/test_range.py::test_range_getitem PASSED
tests/unit/test_range.py::test_range_link_lookup PASSED
tests/unit/test_range.py::test_range_snapshot_records_calls PASSED
tests/unit/test_range.py::test_range_restore_uses_db_lookup PASSED
tests/unit/test_range.py::test_range_logs PASSED
tests/unit/test_range.py::test_range_logs_level_filter PASSED
tests/unit/test_range.py::test_live_node_exec PASSED
tests/unit/test_range.py::test_live_node_upload PASSED
tests/unit/test_range.py::test_live_node_template_renders_and_uploads PASSED
tests/unit/test_range.py::test_live_node_template_cleans_up_tempfile PASSED
tests/unit/test_range.py::test_live_node_logs PASSED
tests/unit/test_range.py::test_live_node_snapshot_restore PASSED
tests/unit/test_range.py::test_link_down PASSED
tests/unit/test_range.py::test_link_up PASSED
tests/unit/test_range.py::test_link_down_without_deploy_raises PASSED
...
117 passed in 0.38s
```

Counts: 91 prior + 26 new = 117 passing, 0 failures, 0 skips.

## Gate 2
Deferred — EC2 bare-metal blocked.

## Resolution
SDK surface wired. `Topology(name, backend, db).deploy()` returns a Range that supports
snapshot/restore/logs and indexes LiveNodes that can exec/upload/template/snapshot/restore/logs.
`Topology.export()`/`from_yaml()` round-trip preserves nodes, interfaces, IPs, depends_on, links.
`Link.down()`/`up()` delete/recreate the backing bridge and log the event.
Engine wires Link/LiveNode references for SDK methods. Topology.link() now stores the
IP-bearing InterfaceSpec on the node so export captures it.
