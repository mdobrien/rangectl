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
        self.status_result = "running"
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

    def status(self, vm_id: str) -> str:
        self._record("status", vm_id)
        return self.status_result

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

    def _find_tap_for_mac(self, vm_id: str, mac: str) -> str | None:
        self._record("_find_tap_for_mac", vm_id, mac)
        return f"tap-{vm_id}"

    def run_tc(self, cmds: list[list[str]]) -> None:
        self._record("run_tc", cmds)

    def tc_cmds(self) -> list[list[str]]:
        """Flattened list of every tc command passed to run_tc (test helper)."""
        out: list[list[str]] = []
        for (args, _kw) in self.calls_of("run_tc"):
            out.extend(args[0])
        return out

    def tc_taps(self) -> set[str]:
        """Set of TAP device names that appeared in any tc command (test helper)."""
        taps: set[str] = set()
        for cmd in self.tc_cmds():
            if "dev" in cmd:
                taps.add(cmd[cmd.index("dev") + 1])
        return taps

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


@pytest.fixture(autouse=True)
def _isolate_state_roots(tmp_path, monkeypatch):
    """Keep unit tests off the real global seed/overlay dirs.

    The engine writes seed ISOs + overlays under engine.SEED_ROOT/OVERLAY_ROOT —
    real paths (/var/lib/libvirt/images/rangectl or ~/.rangectl) keyed only by
    range name. Without isolation a deploy/destroy test touches global state, so
    concurrent runs (or two agents on one box) collide on the same path — e.g.
    FileExistsError on .../seeds/<range>. Redirect both roots to a per-test
    tmp_path. The engine reads them as module globals at call time, so patching
    the module attrs is sufficient. Tests that set their own roots still win:
    they share this function-scoped monkeypatch and run after the autouse fixture,
    so their setattr is applied last.
    """
    from rangectl import engine as engine_mod
    monkeypatch.setattr(engine_mod, "SEED_ROOT", tmp_path / "seeds")
    monkeypatch.setattr(engine_mod, "OVERLAY_ROOT", tmp_path / "overlays")
    # Subnet allocation is host-global (flock registry). Point it at a per-test
    # file so unit tests stay hermetic — otherwise every test would share the
    # real ~/.rangectl registry and allocate non-deterministic /24s.
    monkeypatch.setenv("RANGECTL_SUBNET_REGISTRY", str(tmp_path / "subnets.json"))


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
