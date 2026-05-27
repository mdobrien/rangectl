# rangectl — Testing Strategy
**Created**: 2026-05-27
**Status**: In Progress
**Related Issues**: `20260527-1-vm-testbed-platform-design.md`, `20260527-2-requirements-and-design-decisions.md`

## Goal
Establish a gated TDD workflow that agents follow for every code change. Unit tests gate commits, integration tests gate merges. All tests serve as regression tests for future changes.

## Development Philosophy
Write unit tests → write code → make unit tests pass → write integration tests → make integration tests pass. This flow is non-negotiable. Agents must follow it on every task.

## Test Directory Structure

```
tests/
  unit/                        # fast, no infra, runs anywhere
    conftest.py                # MockBackend, in-memory StateDB fixtures
    test_dag.py                # topo-sort, wave computation
    test_state.py              # SQLite operations (in-memory DB)
    test_dependencies.py       # dependency resolution, ordering, apply()
    test_engine.py             # engine orchestration with MockBackend
    test_topology.py           # Topology/Node/Link declaration, interface specs
    test_images.py             # ImageRegistry/Builder metadata operations
    test_readiness.py          # probe factory functions
    test_types.py              # data classes, InterfaceSpec.__getitem__
  integration/                 # slow, needs KVM host (EC2 box)
    conftest.py                # real LibvirtBackend, cleanup fixtures
    test_libvirt.py            # VM create/start/stop/destroy
    test_networking.py         # bridge create, tap wiring, connectivity
    test_ssh.py                # paramiko connect, exec, upload over mgmt
    test_full_deploy.py        # end-to-end topology deploy → interact → destroy
    test_image_builder.py      # boot-and-snapshot image creation
    test_snapshot.py           # per-node and topology-wide snapshots
```

## MockBackend

Unit tests use a MockBackend that records calls and returns canned responses. No VMs, no bridges, no libvirt. Tests the engine logic in isolation.

```python
class MockBackend:
    def __init__(self):
        self.calls = []
        self._vms = {}

    def create_vm(self, spec):
        self.calls.append(("create_vm", spec.name))
        vm_id = f"mock-{spec.name}"
        self._vms[vm_id] = {"state": "created", "spec": spec}
        return vm_id

    def start(self, vm_id):
        self.calls.append(("start", vm_id))
        self._vms[vm_id]["state"] = "running"

    def stop(self, vm_id):
        self.calls.append(("stop", vm_id))
        self._vms[vm_id]["state"] = "stopped"

    def destroy(self, vm_id):
        self.calls.append(("destroy", vm_id))
        del self._vms[vm_id]

    def create_bridge(self, name):
        self.calls.append(("create_bridge", name))
        return name

    def delete_bridge(self, name):
        self.calls.append(("delete_bridge", name))

    def attach_interface(self, vm_id, bridge, mac):
        self.calls.append(("attach_interface", vm_id, bridge, mac))

    def exec(self, vm_id, cmd):
        self.calls.append(("exec", vm_id, cmd))
        return ExecResult(exit_code=0, stdout="", stderr="")

    def upload(self, vm_id, src, dst):
        self.calls.append(("upload", vm_id, src, dst))

    def snapshot(self, vm_id, name):
        self.calls.append(("snapshot", vm_id, name))
        return f"snap-{name}"

    def restore(self, vm_id, snapshot_id):
        self.calls.append(("restore", vm_id, snapshot_id))

    def create_overlay(self, base_image, overlay_path):
        self.calls.append(("create_overlay", base_image, overlay_path))
        return overlay_path

    def host_resources(self):
        return HostResources(
            total_vcpu=24, total_memory_mb=65536, total_disk_mb=102400,
            available_vcpu=24, available_memory_mb=65536, available_disk_mb=102400,
        )
```

## In-Memory StateDB

Unit tests use SQLite in-memory mode — fast, isolated, no cleanup needed.

```python
@pytest.fixture
def db():
    state_db = StateDB(db_path=":memory:")
    yield state_db
    state_db.close()
```

## Agent Workflow (Per Task)

```
1. Agent receives task
2. Write unit tests in tests/unit/ — tests define expected behavior
3. Run: pytest tests/unit → new tests FAIL (red)
4. Write implementation
5. Run: pytest tests/unit → all tests PASS (green)
6. Run: pytest tests/unit (full suite) → no regressions
7. Write integration tests in tests/integration/ (if applicable)
8. Run: pytest tests/integration → PASS on EC2
9. Commit
```

Rules:
- Never skip step 2 (tests first)
- Never commit with failing tests
- Every public method gets at least one unit test
- Integration tests are required for any code that touches libvirt, networking, or SSH
- Existing tests must pass before new code is merged — they are regression tests

## Running Tests

```bash
pytest tests/unit                  # fast, anywhere — agents run on every change
pytest tests/integration           # slow, EC2 only — agents run before merge
pytest                             # full suite
pytest tests/unit -x               # stop on first failure (useful during TDD)
pytest tests/unit -k "test_dag"    # run specific test module
```

## CI (Future)

GitHub Actions self-hosted runner on EC2 box:
- PR opened → unit tests (GitHub-hosted runner, seconds)
- PR merged to main → integration tests (EC2 self-hosted runner, minutes)

## What Gets Tested Per Phase

| Phase | Unit Tests | Integration Tests | Test Topologies |
|---|---|---|---|
| 0: EC2 Setup | N/A | KVM works, base images exist, smoke test VM | Manual virsh smoke test |
| 1-2: Backend + Networking | MockBackend, StateDB schema, resource validation, IP allocation, subnet math, bridge naming | VM create/start/stop/destroy, bridge create, tap wiring, ping | Topo 1, Topo 2 |
| 3: State Machine + DAG | State transitions, topo-sort, wave computation | Full deploy with readiness probes | Topo 2, Topo 4 |
| 4-5: Images + Dependencies | Metadata CRUD, ordering, apply(), configure registration | Boot-and-snapshot image build, SSH exec, package install, file upload | Topo 3 |
| 6: SDK Surface | Topology/Node/Link API, export/import | End-to-end topology lifecycle, link toggle, multi-topology isolation | Topo 4, Topo 5, Topo 6 |

## Test Topologies

Progressive topologies that validate features phase by phase. A phase is not complete until its topologies deploy, pass all assertions, and destroy cleanly.

### Topo 1: Two Ubuntu VMs (Phase 1-2)
Simplest possible. Validates VM lifecycle, mgmt network, basic connectivity.

```
ubuntu-a ---- ubuntu-b
         10.0.1.0/24

mgmt: rangectl-mgmt-test1
host at .254, ubuntu-a at .1, ubuntu-b at .2
```

**Validates**:
- VM create/start/destroy
- COW overlays from base image
- Mgmt network (host can SSH to both via mgmt IPs)
- Single topology bridge
- Ping between nodes on topology link

### Topo 2: Two Ubuntu VMs + VyOS Router (Phase 2-3)
Two subnets, VyOS routing between them. First dependency chain.

```
ubuntu-a ---- vyos-router ---- ubuntu-b
  10.0.1.0/24             10.0.2.0/24
```

**Validates**:
- `depends_on` (ubuntu nodes depend on router)
- Wave-based deploy (wave 1: router, wave 2: ubuntu-a + ubuntu-b)
- Readiness probes (L2 ping for all, L3 port_open(22) for router)
- Multi-subnet routing
- Different OS images in same topology
- Cross-subnet connectivity (ubuntu-a pings ubuntu-b through router)

### Topo 3: Services + DependencySet (Phase 4-5)
Web server behind a router, with dependency injection.

```
attacker ---- vyos-router ---- web-server
  10.0.1.0/24             10.0.2.0/24
```

**Validates**:
- `packages(["nginx"])` on web-server
- `DependencySet` applied to a node
- `@configure` decorator (template a config file using router's IP)
- `service("nginx", enabled=True, ready_when=port_open(80))`
- L3 readiness (web-server not "ready" until nginx responds on 80)
- `exec()` from attacker to curl the web server through the router

### Topo 4: Diamond Dependency + Snapshot (Phase 3, 6)
Complex DAG — tests wave computation and snapshot/restore.

```
              ┌── web-server ──┐
router ──────┤                 ├── monitor
              └── db-server  ──┘
  10.0.1.0/24    10.0.2.0/24     10.0.3.0/24
```

**Validates**:
- Diamond dependency: monitor depends on both web-server and db-server, both depend on router
- Three waves: [router] → [web-server, db-server] → [monitor]
- Topology-wide `rng.snapshot("baseline")`
- Make a change (install a package, modify a file)
- `rng.restore("baseline")` — verify change is reverted
- Per-node snapshot/restore

### Topo 5: Link Toggling + Fault Injection (Phase 6)
Exercises imperative interaction.

```
ubuntu-a ---- vyos-router ---- ubuntu-b
  10.0.1.0/24             10.0.2.0/24
```

**Validates**:
- `rng.link("router", "ubuntu-b").down()` — ubuntu-a can't reach ubuntu-b
- `rng.link("router", "ubuntu-b").up()` — connectivity restored
- Logs capture link state changes
- `rng.logs()` returns structured entries for toggle events

### Topo 6: Multi-Topology Isolation (Phase 6)
Two topologies deployed simultaneously. Validates namespace isolation.

```
Topology "red-team":                Topology "blue-team":
  attacker -- router -- target        siem -- sensor
  10.0.1.0/24     10.0.2.0/24        172.16.0.0/24

  mgmt: 192.168.100.0/24             mgmt: 192.168.101.0/24
```

**Validates**:
- Two topologies coexist, separate mgmt bridges and subnets
- No cross-topology connectivity on mgmt network
- Independent deploy/destroy lifecycle
- `list_topologies()` shows both
- Destroy one, other is unaffected
