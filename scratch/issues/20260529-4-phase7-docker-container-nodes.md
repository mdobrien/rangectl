# Phase 7: Docker Container Nodes
**Created**: 2026-05-29
**Status**: Complete

## Related Issues
- **Parent**: `20260527-1-vm-testbed-platform-design.md` (Phase 7)
- **Prior**: `20260529-2-topo6-multi-topology-isolation.md` (last Gate 2 phase)
- **Requirements**: `20260527-2-requirements-and-design-decisions.md`
- **API Reference**: `20260527-3-sdk-api-reference.md`
- **Testing Strategy**: `20260527-4-testing-strategy.md`

## Goal
Add Docker container support to rangectl so topologies can mix VMs and containers on the same bridges. A container node starts in ~1s vs ~30s for a VM, uses ~10MB vs ~hundreds of MB RAM.

## Design

### SDK Syntax
```python
topo = Topology("mixed-lab")
router = topo.node("router", container="frrouting/frr:latest", vcpu=1, memory=512)
server = topo.node("server", image="ubuntu-22.04", vcpu=1, memory=1024, depends_on=[router])
topo.link(router.eth1["10.0.1.1/24"], server.eth1["10.0.1.2/24"])
```

`container=` kwarg → ContainerBackend. `image=` kwarg → LibvirtBackend.

### ContainerBackend (new file: `rangectl/container_backend.py`)
Implements the `Backend` protocol. Core mechanics:

```bash
docker run --network=none -d --name <topo>-<node> <image>
# For each interface:
ip link add veth_<node>_<N> type veth peer name veth_<node>_<N>p
ip link set veth_<node>_<N> master <bridge>
ip link set veth_<node>_<N>p netns <container-pid>
nsenter -t <pid> -n ip link set veth_<node>_<N>p name eth<N>
nsenter -t <pid> -n ip addr add <ip>/<cidr> dev eth<N>
nsenter -t <pid> -n ip link set eth<N> up
```

Method mapping:
| Backend Protocol | Container Implementation |
|-----------------|------------------------|
| `create_vm(spec)` | `docker create --network=none --name <name> --cap-add=NET_ADMIN --cap-add=NET_RAW <image>` |
| `start(vm_id)` | `docker start <name>` |
| `stop(vm_id)` | `docker stop <name>` |
| `destroy(vm_id)` | `docker rm -f <name>` |
| `exec(vm_id, cmd)` | `docker exec <name> <cmd>` |
| `upload(vm_id, src, dst)` | `docker cp <src> <name>:<dst>` |
| `create_bridge(name)` | Same as LibvirtBackend (ip link add) — or reuse |
| `delete_bridge(name)` | Same as LibvirtBackend |
| `attach_interface(vm_id, bridge, mac)` | veth wiring: create pair, enslave to bridge, move peer into container netns |
| `create_overlay(base, path)` | No-op for containers (no qcow2) |
| `snapshot(vm_id, name)` | `docker commit <name> <name>:<tag>` (v1: basic) |
| `restore(vm_id, snap_id)` | Skip in v1 — raise NotImplementedError |
| `host_resources()` | Reuse LibvirtBackend's impl or share a utility |

### Engine Changes
The engine currently uses a single backend for the entire topology. For mixed topologies:
- Engine holds both backends: `self._libvirt_backend` and `self._container_backend`
- `_deploy_node()` checks `node.container` vs `node.image` to pick the backend
- `_wire_link()` is unchanged — bridges don't care if member is TAP (VM) or veth (container)
- LiveNode gets whichever backend deployed its node

### Node Changes (topology.py)
- `Node.__init__` gains `container: str | None = None` kwarg
- `Topology.node()` gains `container=` kwarg, passes through
- Validation: exactly one of `image=` or `container=` must be set
- `node.is_container` property for type detection

### Types Changes
- `VMSpec` may need a `container_image` field, or use a separate `ContainerSpec`
- Simpler: reuse `VMSpec` with `image` field holding the Docker image name, add `is_container: bool = False`

### What Does NOT Change
- `link()` — bridges don't care about node type
- `LiveNode` — already backend-agnostic
- `Link.down()`/`Link.up()` — bridge operations, node-type independent
- State machine — containers follow same lifecycle
- DB schema — containers are just nodes with a different backend

## Implementation Steps

### Gate 1 (unit tests)
1. Create `rangectl/container_backend.py` with `ContainerBackend` class
2. Add `container=` kwarg to `Node` and `Topology.node()`
3. Add validation: exactly one of `image=` or `container=`
4. Write `MockContainerBackend` or extend `MockBackend` to handle containers
5. Write unit tests:
   - `test_container_node_creation` — container= kwarg accepted
   - `test_mixed_topology_dag` — VM + container nodes resolve correctly
   - `test_container_exec` — docker exec delegation
   - `test_container_lifecycle` — create/start/stop/destroy
   - `test_node_validation` — error if both image= and container= set
6. Update Engine to dispatch per-node backend
7. Run `pytest tests/unit` — all existing 117 + new tests pass

### Gate 2 (integration tests on EC2)
1. Ensure Docker is installed on EC2 (`docker --version`)
   - If not: add `docker.io` to ec2-bootstrap or install in conftest fixture
2. Write `tests/integration/test_topo7.py`:
   - Topology: FRR container router + Ubuntu VM host
   - FRR container with `--cap-add=NET_ADMIN NET_RAW`
   - Ubuntu VM on opposite side of a link
   - Verify: VM pings container through the bridge
   - Verify: `exec()` works on both (SSH for VM, docker exec for container)
3. Run all integration tests: topo 1-7 pass

## Hard Parts
- FRR / network containers need `--cap-add=NET_ADMIN --cap-add=NET_RAW`
- Multi-interface wiring with `--network=none` must happen before container's main process expects networking
- Container PID discovery: `docker inspect --format '{{.State.Pid}}' <name>`
- Bridge operations (create/delete/assign_host_ip) should be shared between backends, not duplicated
- Mgmt bridge veth for containers: same pattern as data plane, just on the mgmt bridge

## Success Criteria
- [x] ContainerBackend implements Backend protocol
- [x] `container=` kwarg works on Node/Topology.node()
- [x] Engine dispatches per-node to correct backend
- [x] Gate 1: all existing 117 + new container unit tests pass (138/138)
- [x] Gate 2: Topo 7 (mixed VM + container) passes on EC2 (~50s)
- [ ] Gate 2: Topo 1-6 still pass (sweep deferred by user)
- [x] Issue updated with gate output
- [x] Code committed

## Resolution

### Implementation
- `rangectl/container_backend.py` — new module. Docker create/start/stop/rm/exec/cp; veth pair wiring via `ip link` + `nsenter`. Container gets `--network=none --cap-add=NET_ADMIN --cap-add=NET_RAW --hostname <name>`. Veth names hashed from (vm_id, mac) to fit Linux IFNAMSIZ. Mgmt iface lands in container as `eth0`; topology link ifaces keep their declared name (`eth1`, `eth2`, ...). Snapshot/restore raise NotImplementedError (deferred to v2).
- `rangectl/topology.py` — `Node.container` field + `is_container` property. `Topology.node()` validates exactly one of `image=`/`container=` is set. `Topology(container_backend=)` plumbs through to Engine.
- `rangectl/engine.py` — `Engine(container_backend=)`. New `_backend_for(node)` dispatch used in `_deploy_node`, `_wire_link`, `_inject_dependencies`, `destroy`, and final LiveNode binding. New `_deploy_container_node` skips qcow2 overlay + cloud-init seed ISO.
- `tests/unit/test_container.py` — 21 new unit tests covering Node API, validation, MockContainerBackend dispatch, lifecycle, exec, upload, veth wiring (mgmt → eth0, topology link → ethN).
- `tests/integration/test_topo7.py` — mixed nginx + Ubuntu VM on shared bridge.

### Gate 1 result
```
138 passed in 0.54s
```
(117 existing + 21 new — no regressions)

### Gate 2 Topo 7 result
```
tests/integration/test_topo7.py::test_topo7_vm_container_mixed PASSED  [100%]
1 passed in 49.78s
```
Verified:
- Container deploys with `--network=none`, mgmt veth wired, eth1 veth wired
- Ubuntu VM deploys via libvirt, mgmt + eth1 TAPs enslaved to bridges
- `rng["server"].exec("hostname")` → returns `topo7-server` (docker exec path)
- `rng["client"].exec("hostname")` → returns `topo7-client` (SSH path)
- `ping 10.0.1.1` from VM client to container server: success
- `curl http://10.0.1.1/` from VM client returns HTTP 200 from nginx

### Fixes made during integration
1. `docker create` needed `--hostname <name>` so the container's `hostname` command returns the expected name rather than the auto-generated container ID short-hash.
2. nginx image has no `iproute2`, so the in-container `ip addr show` probe was dropped. Connectivity is proven via VM-side ping + curl.

### Known limitations (deferred)
- Container snapshot/restore: NotImplementedError. `docker commit` would work but isn't wired up.
- Link.up()/down() on a container endpoint re-creates the bridge but does NOT re-wire the container's veth — container would need to be restarted or the veth re-attached. Acceptable for v1.
- Pre-pull policy: container images must be present on the host. The integration test pre-pulls nginx in the EC2 setup step.
