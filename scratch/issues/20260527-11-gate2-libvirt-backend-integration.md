# Gate 2: LibvirtBackend + Integration Tests
**Created**: 2026-05-27
**Status**: In Progress

## Related Issues
- **Orchestrator**: `20260527-5-rangectl-orchestrator.md`
- **Plan**: `20260527-1-vm-testbed-platform-design.md`
- **Testing Strategy**: `20260527-4-testing-strategy.md`

## Goal
Implement the real LibvirtBackend that talks to libvirt/QEMU, then write and pass integration tests for Topo 1 through Topo 6 on the EC2 c5.metal instance.

## What Needs to Be Built

### LibvirtBackend (rangectl/libvirt_backend.py)
Implements the Backend protocol using libvirt-python, subprocess (for bridge/ip commands), and paramiko (for SSH/SFTP):

- `create_vm(spec)` — generate libvirt XML, define + don't start yet
- `start(vm_id)` — virsh start
- `stop(vm_id)` — virsh shutdown (graceful) then destroy (force) if needed
- `destroy(vm_id)` — virsh destroy + undefine, delete overlay
- `create_bridge(name)` — ip link add type bridge, ip link set up
- `delete_bridge(name)` — ip link set down, ip link delete
- `attach_interface(vm_id, bridge, mac)` — virsh attach-interface or XML
- `create_overlay(base_image, overlay_path)` — qemu-img create -f qcow2 -F qcow2 -b base overlay
- `exec(vm_id, cmd)` — SSH via paramiko to node's mgmt IP
- `upload(vm_id, src, dst)` — SFTP via paramiko
- `snapshot(vm_id, name)` — virsh snapshot-create-as
- `restore(vm_id, snapshot_id)` — virsh snapshot-revert
- `host_resources()` — read /proc/cpuinfo, /proc/meminfo, df

### Cloud-init seed ISO generation
For each node, generate a cloud-init seed ISO that injects:
- SSH public key (per-topology keypair)
- Network config (mgmt IP via netplan)
- Hostname

### SSH keypair per topology
Generate ed25519 keypair, inject public key via cloud-init, use private key for paramiko connections.

## Test Topologies

### Topo 1: Two Ubuntu VMs (Phase 1-2)
```
ubuntu-a ---- ubuntu-b
         10.0.1.0/24
mgmt: rangectl-mgmt-test1, host .254, a .1, b .2
```

### Topo 2: Two Ubuntu + VyOS Router (Phase 2-3)
Requires VyOS qcow2 — build from ISO first.

### Topo 3: Services + DependencySet (Phase 4-5)
```
attacker ---- vyos-router ---- web-server
```
With nginx installed, DependencySet applied, service ready check.

### Topo 4: Diamond Dependency + Snapshot (Phase 3, 6)
4 nodes, diamond DAG, snapshot/restore.

### Topo 5: Link Toggle (Phase 6)
Link up/down verification.

### Topo 6: Multi-Topology Isolation (Phase 6)
Two topologies simultaneously, verify isolation.

## Success Criteria
- [x] LibvirtBackend implemented (rangectl/libvirt_backend.py)
- [x] cloud-init seed-ISO helper (rangectl/cloudinit.py)
- [x] Engine refactored: pre-creates bridges, pre-allocates mgmt IPs, builds seed ISOs before create_vm, assigns host IP on mgmt bridge
- [x] Topo 1 integration test passes on EC2 (68s end-to-end)
- [x] All unit tests still pass (117/117)
- [ ] Topo 2-6 (deferred; needs VyOS qcow2 build, services, snapshots, link toggle, multi-topo)
- [ ] Committed

## Gate 2 Output (Topo 1)

```
tests/integration/test_topo1.py::test_topo1_boots_and_pings PASSED
========================= 1 passed in 68.49s =========================
```

End-to-end timeline:
- 0s   deploy() starts
- 30s  both VMs SSH-ready (cloud-init complete, static mgmt IPs configured)
- 31s  hostname checks pass
- 32s  ping a->b OK (3/3, ~0.5ms RTT via Linux bridge)
- 34s  ping b->a OK
- 48s  graceful shutdown of VM 1
- 68s  destroy + bridge cleanup complete

Post-test verification: `virsh list --all` empty, no leftover `rl-*` or `rlmgt-*` bridges.

## Key Implementation Notes

- **Bridge naming**: Linux IFNAMSIZ caps ifnames at 15 chars. `mgmt_bridge_name()` and `bridge_name()` now produce `rlmgt-{hash6}` and `rl-{hash6}-{idx}` from a sha1 of the topology name.
- **Overlay/seed paths**: Stored under `/var/lib/libvirt/images/rangectl/` so the stock AppArmor profile (which whitelists `/var/lib/libvirt/images/**`) lets qemu open them. Engine falls back to `~/.rangectl/` when libvirt images dir isn't writable (unit tests).
- **Interface wiring**: All interfaces (mgmt + topology links) inlined into domain XML at create_vm time. `attach_interface` is a no-op for LibvirtBackend (kept for unit-test back-end protocol compatibility and future hot-attach scenarios).
- **Cloud-init network-config**: Static IPs configured via netplan v2, matching interfaces by MAC. Deterministic MACs derived from `sha1(topology/node/suffix)`.
- **Host-side mgmt IP**: `assign_host_ip()` adds `192.168.X.254/24` to the mgmt bridge so the EC2 host can SSH directly to VMs without any router.
- **SSH keypair per topology**: Generated lazily by `LibvirtBackend.ssh_pubkey(topology_name)`, stored in `~/.rangectl/keys/{topo}/`. Paramiko uses the private key for all exec/upload calls.
- **Sudo required**: Integration tests run as root via `sudo .../pytest` (needed for `ip link add type bridge`).

## Files Added / Changed

- new: `rangectl/cloudinit.py` — `create_seed_iso()` via cloud-localds
- new: `rangectl/libvirt_backend.py` — Backend impl over virsh + qemu-img + ip + paramiko
- new: `tests/integration/conftest.py`, `tests/integration/__init__.py`
- new: `tests/integration/test_topo1.py`
- new: `pyproject.toml` — package metadata so `pip install -e .` works on EC2
- mod: `rangectl/engine.py` — pre-create bridges, pre-allocate IPs, seed-ISO + ssh-key per topology, expanded VMSpec
- mod: `rangectl/types.py` — VMSpec gains seed_iso_path/mgmt_ip/topology_name/ssh_user; InterfaceSpec gains bridge/mac
- mod: `rangectl/backend.py` — Backend protocol gains `ssh_pubkey()` and `assign_host_ip()`
- mod: `rangectl/networking.py` — hashed bridge naming for IFNAMSIZ compliance
- mod: `tests/unit/conftest.py` — MockBackend implements new methods
- mod: `tests/unit/test_networking.py`, `test_deploy.py`, `test_state.py` — adjusted for new naming

## Resolution

Gate 2 Phase 1 (Topo 1) complete and committed. Topo 2-6 deferred to follow-on issues.
