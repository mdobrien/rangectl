"""Host-global mgmt-subnet allocator (flock-guarded file).

The mgmt `/24` pool is the one resource that must be allocated **per host**, not
per StateDB: independent ranges (and independent test processes, each with its
own temp StateDB) all share the same physical host network, so two ranges
handing themselves the same `192.168.100.0/24` collide on host routes and guest
IPs (see scratch/issues/20260602-1-parallel-test-exploration.md).

This module keeps the authoritative taken-set in a single small JSON file,
guarded by an exclusive ``flock`` so concurrent processes AND threads serialize.
``StateDB`` delegates allocation here and mirrors the result into its own
``mgmt_subnets`` table for local inspection/persistence.

Path resolution (first match wins):
  1. explicit ``path`` argument
  2. ``RANGECTL_SUBNET_REGISTRY`` env var
  3. ``~/.rangectl/mgmt_subnets.json`` (production default)
"""
from __future__ import annotations
import fcntl
import ipaddress
import json
import logging
import os
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)

POOL_BASE = ipaddress.IPv4Network("192.168.100.0/24")
POOL_PREFIX = 24
POOL_SIZE = 100  # 192.168.100.0/24 .. 192.168.199.0/24

ENV_VAR = "RANGECTL_SUBNET_REGISTRY"
DEFAULT_PATH = "~/.rangectl/mgmt_subnets.json"


def registry_path(explicit: str | Path | None = None) -> Path:
    """Resolve the registry file path (argument > env var > default)."""
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get(ENV_VAR)
    return Path(env) if env else Path(DEFAULT_PATH).expanduser()


@contextmanager
def _locked(path: Path):
    """Open (creating) the registry file and hold an exclusive flock.

    The lock is tied to the open file description, so two separate ``open``s —
    even within one process (parallel threads) — mutually exclude. That covers
    both the cross-process (pytest -n / per-file harness) and the in-process
    thread (parallel multi-range) cases.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read(fd: int) -> dict[str, str]:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    while True:
        block = os.read(fd, 65536)
        if not block:
            break
        chunks.append(block)
    raw = b"".join(chunks).decode() or "{}"
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        log.warning("subnet registry %s corrupt; treating as empty", fd)
        return {}


def _write(fd: int, mapping: dict[str, str]) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, json.dumps(mapping).encode())
    os.fsync(fd)


def allocate(topology_name: str, path: str | Path | None = None) -> str:
    """Reserve and return the first free `/24` for ``topology_name``."""
    p = registry_path(path)
    with _locked(p) as fd:
        taken = _read(fd)  # {subnet: topology_name}
        base = int(POOL_BASE.network_address)
        for i in range(POOL_SIZE):
            net = ipaddress.IPv4Network((base + i * 256, POOL_PREFIX))
            candidate = f"{net.network_address}/{POOL_PREFIX}"
            if candidate not in taken:
                taken[candidate] = topology_name
                _write(fd, taken)
                log.info("allocated mgmt subnet %s -> %s (registry=%s)",
                         candidate, topology_name, p)
                return candidate
        raise RuntimeError("mgmt subnet pool exhausted")


def free(topology_name: str, path: str | Path | None = None) -> None:
    """Release every subnet held by ``topology_name``."""
    p = registry_path(path)
    with _locked(p) as fd:
        taken = _read(fd)
        remaining = {s: t for s, t in taken.items() if t != topology_name}
        if len(remaining) != len(taken):
            _write(fd, remaining)
            log.info("freed mgmt subnet(s) for %s (registry=%s)",
                     topology_name, p)


def reset(path: str | Path | None = None) -> None:
    """Clear the registry (pre-run cleanup for a fresh concurrent batch)."""
    p = registry_path(path)
    with _locked(p) as fd:
        _write(fd, {})
