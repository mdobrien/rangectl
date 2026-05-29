# Phase 8-10: Namespace Isolation — Gate 1 (Unit Tests Only)
**Created**: 2026-05-29
**Status**: Complete

## Related Issues
- **Plan**: `20260527-1-vm-testbed-platform-design.md` — Phases 8-10 detail
- **Architecture**: `agents/network-architecture.md` — validated spike
- **Orchestrator**: `20260527-5-rangectl-orchestrator.md`

## Goal
Build the three namespace isolation modules (supervisor, netns, cgroup) and rewrite the backend to use per-range libvirt sockets. Gate 1 only — no integration tests. Integration testing happens in Phase 11.

## Phase 8: Supervisor + Network Namespace

### New: `rangectl/supervisor.py`
Launches libvirtd inside PID+net+mount namespaces. Key functions:

```python
def create_range(name: str, mgmt_subnet: str, range_dir: str = "/ranges") -> RangeInfo:
    """Create namespaces, bind-mounts, start libvirtd, wire veth mgmt."""

def destroy_range(name: str) -> None:
    """Kill libvirtd host-PID → kernel reaps all QEMU. Remove dirs, routes, iptables."""

@dataclass
class RangeInfo:
    name: str
    pid: int              # libvirtd's host-PID
    netns_name: str       # ip netns name
    libvirt_socket: str   # /ranges/<name>/run-libvirt/libvirt-sock
    mgmt_subnet: str
    veth_host: str        # host-side veth interface name
    veth_ns: str          # ns-side veth interface name
```

Implementation steps:
1. Create per-range dirs under `/ranges/<name>/` for all bind-mount targets
2. Generate per-range `qemu.conf` (`security_driver="none"`, `dynamic_ownership=0`, `user=root`, `group=root`) and `libvirtd.conf`
3. Use `unshare --pid --fork --net --mount --uts --propagation private --mount-proc` to create namespaces
4. Inside namespaces: bind-mount per-range dirs over libvirt state paths (see architecture doc section 6)
5. Block `/run/dbus` with empty dir bind-mount
6. `exec /usr/sbin/libvirtd --config <conf> --pid-file /run/libvirt/libvirtd.pid`
7. Libvirt socket exposed at `/ranges/<name>/run-libvirt/libvirt-sock`
8. Teardown: kill libvirtd's host-PID, remove dirs, routes

### New: `rangectl/netns.py`
Network namespace management. Key functions:

```python
def create_mgmt_network(netns_name: str, mgmt_subnet: str, range_name: str) -> MgmtNetwork:
    """Create mgmt bridge in netns, veth pair to host, host route + iptables FORWARD."""

def destroy_mgmt_network(mgmt: MgmtNetwork) -> None:
    """Remove veth, route, iptables rule."""

def create_data_bridge(netns_name: str, bridge_name: str) -> None:
    """Create a data-plane bridge inside the netns."""

def exec_in_netns(netns_name: str, cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a command inside the given network namespace."""

@dataclass
class MgmtNetwork:
    bridge_name: str      # "mgmt-br" (clean name, no hashing)
    veth_host: str
    veth_ns: str
    host_ip: str          # .254 on host side
    subnet: str
```

Inside netns, bridge names are clean: `mgmt-br`, `data-0`, `data-1` — no IFNAMSIZ hashing.

## Phase 9: Cgroups

### New: `rangectl/cgroup.py`
```python
@dataclass
class Resources:
    memory: str | None = None   # e.g. "32G"
    cpus: int | None = None
    pids: int | None = None
    cpuset: str | None = None   # e.g. "0-7"

def create_cgroup(range_name: str, resources: Resources) -> str:
    """Create cgroup at /sys/fs/cgroup/rangectl-<name>/, set limits. Returns cgroup path."""

def destroy_cgroup(range_name: str) -> None:
    """Remove the cgroup."""

def freeze(range_name: str) -> None:
    """Write 1 to cgroup.freeze."""

def thaw(range_name: str) -> None:
    """Write 0 to cgroup.freeze."""

def write_pid(cgroup_path: str, pid: int) -> None:
    """Write PID into cgroup so all descendants inherit it."""
```

Supervisor must write its own PID into the cgroup BEFORE calling unshare, so libvirtd + all QEMU processes are born into it.

## Phase 10: Backend Rewrite

### Changes to `rangectl/libvirt_backend.py` (~50% rewrite)
- Add `libvirt_socket` parameter: `LibvirtBackend(libvirt_socket=None, ...)` — when set, all virsh commands use `virsh -c qemu+unix:///system?socket=<socket>`
- Add `netns_name` parameter: when set, bridge/TAP operations execute inside the netns via `ip netns exec` or `nsenter`
- `create_bridge()` → executes inside netns (bridges are ns-scoped, clean names)
- `delete_bridge()` → executes inside netns
- `assign_host_ip()` → executes inside netns
- Domain XML: `<interface type='bridge'><source bridge='mgmt-br'/></interface>` (clean names)

### Changes to `rangectl/networking.py`
- Add `ns_bridge_name(index)` → returns `data-{index}` (clean, no hashing)
- Add `ns_mgmt_bridge_name()` → returns `mgmt-br`
- Keep old hashing functions for backward compatibility until Phase 11 wires everything

### Delete (in Phase 10 or 11)
- Bridge name hashing logic (no longer needed with netns scoping)
- `_ensure_mgmt_isolation()` iptables FORWARD DROP rule (structural isolation replaces it)
- Host-IP collision avoidance

## Unit Test Strategy

### Phase 8 tests (`tests/unit/test_supervisor.py`, `tests/unit/test_netns.py`)
- Supervisor: mock subprocess calls, verify correct unshare flags, bind-mount list, libvirtd args, config file content
- Netns: mock ip commands, verify bridge creation, veth pair, host route, iptables rule
- Teardown: verify kill, cleanup sequence

### Phase 9 tests (`tests/unit/test_cgroup.py`)
- Resources dataclass validation
- Cgroup creation: verify correct paths and file writes (mock filesystem)
- Freeze/thaw: verify cgroup.freeze file write
- PID write: verify cgroup.procs write

### Phase 10 tests (update existing `tests/unit/test_*.py`)
- LibvirtBackend with socket: verify virsh commands include `-c qemu+unix:///system?socket=...`
- LibvirtBackend with netns: verify bridge commands use `ip netns exec`
- Update MockBackend if interface changes
- All existing unit tests must still pass (138/138 + new tests)

## Success Criteria
- [x] `rangectl/supervisor.py` — create_range, destroy_range
- [x] `rangectl/netns.py` — mgmt network, data bridges, exec_in_netns
- [x] `rangectl/cgroup.py` — Resources, create/destroy cgroup, freeze/thaw
- [x] `rangectl/libvirt_backend.py` — socket-aware virsh, netns-aware bridges
- [x] All new unit tests pass
- [x] All 138 existing unit tests still pass (no regressions)
- [x] `pytest tests/unit` — all green

## Resolution (2026-05-29)

All four deliverables built TDD-first (tests red → implement → green). Gate 1
passing: **186 unit tests** (138 existing + 48 new), 0 regressions.

```
$ pytest tests/unit
186 passed in 0.54s
```

### Files created
- `rangectl/netns.py` — `MgmtNetwork`, `create_mgmt_network`,
  `destroy_mgmt_network`, `create_data_bridge`, `exec_in_netns`
- `rangectl/cgroup.py` — `Resources`, `create_cgroup`, `destroy_cgroup`,
  `freeze`, `thaw`, `write_pid`
- `rangectl/supervisor.py` — `RangeInfo`, `create_range`, `destroy_range`
- `tests/unit/test_netns.py` (11), `test_cgroup.py` (13),
  `test_supervisor.py` (12), `test_libvirt_backend.py` (12)

### Files modified
- `rangectl/libvirt_backend.py` — added optional `libvirt_socket` + `netns_name`
  ctor params; `_virsh()`/`_ip()`/`_virsh_console_cmd()` builders; all virsh and
  bridge/IP ops are now socket/netns-aware. Legacy mode (both None) unchanged.
- `rangectl/networking.py` — added clean-name helpers `ns_bridge_name(index)`
  → `data-{i}`, `ns_mgmt_bridge_name()` → `mgmt-br` (old hashed helpers kept).

### Design decisions / deviations
- **unshare omits `--net`.** The kickoff/spike listed `unshare --net`, which
  creates an *anonymous* netns. But `netns.py` (and the v2 design) manages
  bridges/veth against a *named* netns via `ip netns exec`. Reconciled by
  creating a named netns (`ip netns add rangectl-<name>`) and launching
  libvirtd with `ip netns exec <ns> unshare --pid --fork --mount --uts ...`.
  The named netns supplies network isolation; unshare supplies the rest. This
  makes supervisor + netns actually compose. Documented in `supervisor.py`.
- **Per-range state file.** `destroy_range(name)` only gets a name, so
  `create_range` persists pid/netns/veth/subnet to `<range_dir>/<name>/range.json`
  for teardown to reconstruct the `MgmtNetwork` handle and kill the host-PID.
- **assign_host_ip isolation rule** only installs the legacy FORWARD DROP rule
  in host-level mode; in netns mode isolation is structural so it is skipped.
- **cgroup memory** converts human sizes ("32G") to bytes for `memory.max`
  (cgroup v2 requires bytes).
- Cgroup controller `subtree_control` enabling and host-PID-of-libvirtd
  discovery vs. the unshare wrapper PID are left for Phase 11 integration
  (out of scope for unit-only Gate 1).

### Note
`test_deploy.py::test_deploy_two_nodes_with_link` flaked once (sqlite3
InterfaceError — threaded deploy + in-memory SQLite race) then passed on every
rerun. Pre-existing, unrelated to these modules.
