from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol

from rangectl.types import ExecResult, VMSpec


class Backend(Protocol):

    def create_vm(self, spec: VMSpec) -> str: ...

    def start(self, vm_id: str) -> None: ...

    def stop(self, vm_id: str) -> None: ...

    def destroy(self, vm_id: str) -> None: ...

    def snapshot(self, vm_id: str, name: str) -> str: ...

    def restore(self, vm_id: str, snapshot_id: str) -> None: ...

    def exec(self, vm_id: str, cmd: str) -> ExecResult: ...

    def upload(self, vm_id: str, src: str, dst: str) -> None: ...

    def create_bridge(self, name: str) -> str: ...

    def delete_bridge(self, name: str) -> None: ...

    def attach_interface(self, vm_id: str, bridge: str, mac: str) -> None: ...

    # L2 plumbing (Phase 20). A veth pair joins two bridges (you cannot
    # enslave a bridge to a bridge); per-port flags turn a bridge port into a
    # hub port (learning off, flood on).
    def create_veth_pair(self, name_a: str, name_b: str,
                         bridge_a: str, bridge_b: str) -> None: ...

    def delete_device(self, name: str) -> None: ...

    def set_port_flags(self, port: str, *, learning: bool,
                       flood: bool) -> None: ...

    # tc runner for link impairment (Phase 19) — commands are pre-built,
    # netns-prefixed argv lists.
    def run_tc(self, cmds: list[list[str]]) -> None: ...

    def create_overlay(self, base_image: str, overlay_path: str) -> str: ...

    def host_resources(self) -> HostResources: ...

    # Optional capability: per-topology SSH keypair management.
    # Returns the public key (one line OpenSSH format) for cloud-init injection.
    def ssh_pubkey(self, topology_name: str) -> str: ...

    # Optional capability: assign a host-side IP to a bridge so the host can
    # reach VMs on that subnet.
    def assign_host_ip(self, bridge: str, ip: str, cidr: str) -> None: ...


@dataclass
class HostResources:
    total_vcpu: int
    total_memory_mb: int
    total_disk_mb: int
    available_vcpu: int
    available_memory_mb: int
    available_disk_mb: int
