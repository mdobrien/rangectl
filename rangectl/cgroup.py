"""cgroup v2 resource control for ranges (Phase 9).

Each range gets a cgroup at ``/sys/fs/cgroup/rangectl-<name>/`` carrying memory,
CPU, PID, and cpuset limits, plus the freezer for atomic pause/resume. The
supervisor writes its own PID into the cgroup before ``unshare`` so libvirtd and
every QEMU process it spawns are born into it.
"""
from __future__ import annotations
import logging
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

CGROUP_ROOT = Path("/sys/fs/cgroup")
CPU_PERIOD = 100000  # microseconds per scheduling period
DRAIN_TIMEOUT = 5.0  # seconds to wait for cgroup.procs to empty after kill


@dataclass
class Resources:
    memory: str | None = None   # e.g. "32G"
    cpus: int | None = None
    pids: int | None = None
    cpuset: str | None = None   # e.g. "0-7"


def _to_bytes(value: str) -> int:
    """Convert a human memory size ("32G", "512M", "1024K", "1000000") to bytes."""
    units = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}
    value = value.strip()
    suffix = value[-1].upper()
    if suffix in units:
        return int(float(value[:-1]) * units[suffix])
    return int(value)


def _cgroup_path(range_name: str) -> Path:
    return CGROUP_ROOT / f"rangectl-{range_name}"


def create_cgroup(range_name: str, resources: Resources) -> str:
    """Create the range cgroup, apply the requested limits, return its path."""
    cg = _cgroup_path(range_name)
    log.info("create_cgroup: %s (%s)", cg, resources)
    cg.mkdir(parents=True, exist_ok=True)

    if resources.memory is not None:
        (cg / "memory.max").write_text(str(_to_bytes(resources.memory)))
    if resources.cpus is not None:
        (cg / "cpu.max").write_text(f"{resources.cpus * CPU_PERIOD} {CPU_PERIOD}")
    if resources.pids is not None:
        (cg / "pids.max").write_text(str(resources.pids))
    if resources.cpuset is not None:
        (cg / "cpuset.cpus").write_text(resources.cpuset)

    return str(cg)


def destroy_cgroup(range_name: str) -> None:
    """Remove the range cgroup. rmdir only succeeds on an empty cgroup, so first
    kill any surviving members (atomically, via ``cgroup.kill``) and wait for
    ``cgroup.procs`` to drain. Absent control files (e.g. the unit-test tmp dir)
    or an absent cgroup are no-ops."""
    cg = _cgroup_path(range_name)
    log.info("destroy_cgroup: %s", cg)
    _kill_and_drain(cg)
    try:
        cg.rmdir()
    except FileNotFoundError:
        pass


def _kill_and_drain(cg: Path) -> None:
    """SIGKILL every process in the cgroup (and descendants) and wait for the
    cgroup to empty, so the subsequent rmdir does not hit EBUSY."""
    kill_file = cg / "cgroup.kill"
    if not kill_file.exists():
        # No cgroup.kill (cgroup gone, or a plain tmp dir in tests): nothing to
        # kill. Treat as already drained.
        return
    kill_file.write_text("1")

    procs = cg / "cgroup.procs"
    deadline = time.time() + DRAIN_TIMEOUT
    while time.time() < deadline:
        try:
            if not procs.read_text().strip():
                return
        except FileNotFoundError:
            return
        time.sleep(0.1)
    log.warning("cgroup %s did not drain within %ss; rmdir may fail",
                cg, DRAIN_TIMEOUT)


def freeze(range_name: str) -> None:
    log.info("freeze: %s", range_name)
    (_cgroup_path(range_name) / "cgroup.freeze").write_text("1")


def thaw(range_name: str) -> None:
    log.info("thaw: %s", range_name)
    (_cgroup_path(range_name) / "cgroup.freeze").write_text("0")


def is_frozen(range_name: str) -> bool:
    """True if the range's cgroup freezer is engaged. False if no cgroup exists
    (range never had resource limits, or was deployed without freeze support)."""
    try:
        return (_cgroup_path(range_name) / "cgroup.freeze").read_text().strip() == "1"
    except OSError:
        return False


def write_pid(cgroup_path: str, pid: int) -> None:
    """Place ``pid`` into the cgroup so all of its descendants inherit it."""
    log.info("write_pid: %s -> %s", pid, cgroup_path)
    (Path(cgroup_path) / "cgroup.procs").write_text(str(pid))
