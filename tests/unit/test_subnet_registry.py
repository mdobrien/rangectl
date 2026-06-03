"""Unit tests for the host-global flock subnet allocator.

The registry is the fix for the parallel-test subnet collision: independent
StateDBs (one per test process) must NOT both grab 192.168.100.0/24. See
scratch/issues/20260602-1-parallel-test-exploration.md.
"""
from __future__ import annotations

import threading

import pytest

from rangectl import subnet_registry as sr


@pytest.fixture
def reg(tmp_path):
    return str(tmp_path / "subnets.json")


def test_allocate_first_is_100(reg):
    assert sr.allocate("a", reg) == "192.168.100.0/24"


def test_allocate_sequential_distinct(reg):
    a = sr.allocate("a", reg)
    b = sr.allocate("b", reg)
    c = sr.allocate("c", reg)
    assert [a, b, c] == [
        "192.168.100.0/24", "192.168.101.0/24", "192.168.102.0/24"]


def test_free_releases_for_reuse(reg):
    a = sr.allocate("a", reg)
    sr.allocate("b", reg)
    sr.free("a", reg)
    c = sr.allocate("c", reg)
    assert c == a  # lowest free /24 reused


def test_reset_clears(reg):
    sr.allocate("a", reg)
    sr.reset(reg)
    assert sr.allocate("b", reg) == "192.168.100.0/24"


def test_pool_exhaustion_raises(reg):
    for i in range(sr.POOL_SIZE):
        sr.allocate(f"t{i}", reg)
    with pytest.raises(RuntimeError, match="exhausted"):
        sr.allocate("overflow", reg)


def test_concurrent_threads_get_distinct_subnets(reg):
    """The collision the fix targets: many concurrent allocators, one shared
    registry → every subnet handed out must be UNIQUE (no two ranges share)."""
    results: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(16)

    def worker(idx: int) -> None:
        barrier.wait()  # maximize contention
        subnet = sr.allocate(f"t{idx}", reg)
        with lock:
            results.append(subnet)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 16
    assert len(set(results)) == 16, f"duplicate subnets handed out: {results}"
