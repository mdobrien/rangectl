"""Host-global mgmt-subnet allocator (flock-guarded file).

The mgmt `/24` pool is the one resource that must be allocated **per host**, not
per StateDB: independent ranges (and independent test processes, each with its
own temp StateDB) all share the same physical host network, so two ranges
handing themselves the same `10.255.1.0/24` collide on host routes and guest
IPs (see scratch/issues/20260602-1-parallel-test-exploration.md).

This module keeps the authoritative taken-set in a single small JSON file,
guarded by an exclusive ``flock`` so concurrent processes AND threads serialize.
``StateDB`` delegates allocation here and mirrors the result into its own
``mgmt_subnets`` table for local inspection/persistence.

Registry-path resolution (first match wins):
  1. explicit ``path`` argument
  2. ``RANGECTL_SUBNET_REGISTRY`` env var
  3. ``~/.rangectl/mgmt_subnets.json`` (production default)

Pool resolution (first match wins) — the /24s ranges draw from:
  1. explicit ``pool`` argument
  2. ``RANGECTL_MGMT_POOL`` env var (a CIDR, e.g. ``10.200.0.0/16``)
  3. ``10.255.0.0/16`` (production default)

The pool aggregate must summarize to ONE host route (Phase 16), so it is a
single CIDR carved into /24s with the network-edge (.0) and broadcast-edge
(.255) /24s dropped. The default yields ``10.255.1.0/24 .. 10.255.254.0/24``
(254 subnets).
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

POOL_PREFIX = 24
DEFAULT_POOL = ipaddress.IPv4Network("10.255.0.0/16")
POOL_ENV_VAR = "RANGECTL_MGMT_POOL"

ENV_VAR = "RANGECTL_SUBNET_REGISTRY"
DEFAULT_PATH = "~/.rangectl/mgmt_subnets.json"


def registry_path(explicit: str | Path | None = None) -> Path:
    """Resolve the registry file path (argument > env var > default)."""
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get(ENV_VAR)
    return Path(env) if env else Path(DEFAULT_PATH).expanduser()


def _resolve_pool(explicit: str | None = None) -> ipaddress.IPv4Network:
    """Resolve the pool aggregate CIDR (argument > env var > default).

    Validates that the value parses and is at least /24-sized (so it can be
    carved into /24 subnets); raises ValueError with a clear message otherwise.
    """
    raw = explicit if explicit is not None else os.environ.get(POOL_ENV_VAR)
    if raw is None:
        return DEFAULT_POOL
    try:
        net = ipaddress.IPv4Network(raw, strict=False)
    except ValueError as e:
        raise ValueError(
            f"{POOL_ENV_VAR}={raw!r} is not a valid IPv4 CIDR: {e}"
        ) from e
    if net.prefixlen > POOL_PREFIX:
        raise ValueError(
            f"{POOL_ENV_VAR}={raw!r} (/{net.prefixlen}) must be at least /24 "
            "so it can be carved into /24 mgmt subnets"
        )
    return net


def pool_aggregate(explicit: str | None = None) -> str:
    """Return the resolved pool aggregate as a CIDR string (the host route)."""
    return str(_resolve_pool(explicit))


def pool_subnets(explicit: str | None = None) -> list[str]:
    """Return the allocatable /24s in the pool, in order.

    Drops the network-edge (.0) and broadcast-edge (.255) /24s when the
    aggregate is larger than a single /24, so the default /16 yields 254
    subnets (``10.255.1.0/24 .. 10.255.254.0/24``).
    """
    agg = _resolve_pool(explicit)
    nets = list(agg.subnets(new_prefix=POOL_PREFIX))
    if len(nets) > 2:
        nets = nets[1:-1]
    return [str(n) for n in nets]


# Default pool capacity (no override). Exposed for callers/tests that want the
# nominal size without recomputing.
POOL_SIZE = len(pool_subnets())


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


def allocate(topology_name: str, path: str | Path | None = None,
             pool: str | None = None) -> str:
    """Reserve and return the first free `/24` for ``topology_name``."""
    p = registry_path(path)
    with _locked(p) as fd:
        taken = _read(fd)  # {subnet: topology_name}
        for candidate in pool_subnets(pool):
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
