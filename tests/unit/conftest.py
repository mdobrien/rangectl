from __future__ import annotations
from collections import defaultdict
from typing import Any

import pytest

from rangectl.backend import HostResources
from rangectl.state import StateDB
from rangectl.types import ExecResult, VMSpec


class MockBackend:
    """In-memory backend that records calls and returns canned responses.

    Implements the Backend protocol for unit tests. Use ``calls`` to inspect
    ordered call history. Override ``host_resources_result`` to drive
    resource-validation tests, or ``exec_results``/``exec_default`` to script
    command output.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.vms: dict[str, VMSpec] = {}
        self.bridges: set[str] = set()
        self.snapshots: dict[str, dict[str, str]] = defaultdict(dict)
        self.overlays: dict[str, str] = {}
        self.exec_results: dict[tuple[str, str], ExecResult] = {}
        self.exec_default = ExecResult(exit_code=0, stdout="", stderr="")
        self.host_resources_result = HostResources(
            total_vcpu=16,
            total_memory_mb=32768,
            total_disk_mb=500_000,
            available_vcpu=16,
            available_memory_mb=32768,
            available_disk_mb=500_000,
        )
        self._next_vm_id = 0
        self._next_snap_id = 0

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def create_vm(self, spec: VMSpec) -> str:
        self._record("create_vm", spec)
        self._next_vm_id += 1
        vm_id = f"vm-{self._next_vm_id}"
        self.vms[vm_id] = spec
        return vm_id

    def start(self, vm_id: str) -> None:
        self._record("start", vm_id)

    def stop(self, vm_id: str) -> None:
        self._record("stop", vm_id)

    def destroy(self, vm_id: str) -> None:
        self._record("destroy", vm_id)
        self.vms.pop(vm_id, None)

    def snapshot(self, vm_id: str, name: str) -> str:
        self._record("snapshot", vm_id, name)
        self._next_snap_id += 1
        snap_id = f"snap-{self._next_snap_id}"
        self.snapshots[vm_id][name] = snap_id
        return snap_id

    def restore(self, vm_id: str, snapshot_id: str) -> None:
        self._record("restore", vm_id, snapshot_id)

    def exec(self, vm_id: str, cmd: str) -> ExecResult:
        self._record("exec", vm_id, cmd)
        return self.exec_results.get((vm_id, cmd), self.exec_default)

    def upload(self, vm_id: str, src: str, dst: str) -> None:
        self._record("upload", vm_id, src, dst)

    def create_bridge(self, name: str) -> str:
        self._record("create_bridge", name)
        self.bridges.add(name)
        return name

    def delete_bridge(self, name: str) -> None:
        self._record("delete_bridge", name)
        self.bridges.discard(name)

    def attach_interface(self, vm_id: str, bridge: str, mac: str) -> None:
        self._record("attach_interface", vm_id, bridge, mac)

    def create_overlay(self, base_image: str, overlay_path: str) -> str:
        self._record("create_overlay", base_image, overlay_path)
        self.overlays[overlay_path] = base_image
        return overlay_path

    def host_resources(self) -> HostResources:
        self._record("host_resources")
        return self.host_resources_result

    def ssh_pubkey(self, topology_name: str) -> str:
        self._record("ssh_pubkey", topology_name)
        return f"ssh-ed25519 AAAAMOCK rangectl-{topology_name}"

    def assign_host_ip(self, bridge: str, ip: str, cidr: str) -> None:
        self._record("assign_host_ip", bridge, ip, cidr)

    def calls_of(self, name: str) -> list[tuple[tuple, dict]]:
        return [(a, k) for (n, a, k) in self.calls if n == name]


@pytest.fixture
def backend() -> MockBackend:
    return MockBackend()


@pytest.fixture
def db() -> StateDB:
    state = StateDB(db_path=":memory:")
    try:
        yield state
    finally:
        state.close()


@pytest.fixture
def exec_result() -> ExecResult:
    return ExecResult(exit_code=0, stdout="ok", stderr="")
