"""cgroup v2 resource control for ranges (Phase 9).

Each range gets a cgroup at ``/sys/fs/cgroup/rangectl-<name>/`` carrying memory,
CPU, PID, and cpuset limits, plus the freezer for atomic pause/resume. The
supervisor writes its own PID into the cgroup before ``unshare`` so libvirtd and
every QEMU process it spawns are born into it.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

CGROUP_ROOT = Path("/sys/fs/cgroup")
CPU_PERIOD = 100000  # microseconds per scheduling period


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
    """Remove the range cgroup. A cgroup directory is removed with rmdir once
    empty of processes; absent is a no-op."""
    cg = _cgroup_path(range_name)
    log.info("destroy_cgroup: %s", cg)
    try:
        cg.rmdir()
    except FileNotFoundError:
        pass


def freeze(range_name: str) -> None:
    log.info("freeze: %s", range_name)
    (_cgroup_path(range_name) / "cgroup.freeze").write_text("1")


def thaw(range_name: str) -> None:
    log.info("thaw: %s", range_name)
    (_cgroup_path(range_name) / "cgroup.freeze").write_text("0")


def write_pid(cgroup_path: str, pid: int) -> None:
    """Place ``pid`` into the cgroup so all of its descendants inherit it."""
    log.info("write_pid: %s -> %s", pid, cgroup_path)
    (Path(cgroup_path) / "cgroup.procs").write_text(str(pid))
