"""Unit tests for the host-global flock subnet allocator.

The registry is the fix for the parallel-test subnet collision: independent
StateDBs (one per test process) must NOT both grab the same /24. See
scratch/issues/20260602-1-parallel-test-exploration.md.

Pool migrated to 10.255.0.0/16 (Phase 16a) so the whole pool summarizes to one
host route. See scratch/issues/20260609-1-phase16-mgmt-ns-design.md (D3a).
"""
from __future__ import annotations

import threading

import pytest

from rangectl import subnet_registry as sr


@pytest.fixture
def reg(tmp_path):
    return str(tmp_path / "subnets.json")


def test_allocate_first_is_10_255_1(reg):
    assert sr.allocate("a", reg) == "10.255.1.0/24"


def test_allocate_sequential_distinct(reg):
    a = sr.allocate("a", reg)
    b = sr.allocate("b", reg)
    c = sr.allocate("c", reg)
    assert [a, b, c] == [
        "10.255.1.0/24", "10.255.2.0/24", "10.255.3.0/24"]


def test_free_releases_for_reuse(reg):
    a = sr.allocate("a", reg)
    sr.allocate("b", reg)
    sr.free("a", reg)
    c = sr.allocate("c", reg)
    assert c == a  # lowest free /24 reused


def test_reset_clears(reg):
    sr.allocate("a", reg)
    sr.reset(reg)
    assert sr.allocate("b", reg) == "10.255.1.0/24"


def test_default_pool_capacity_is_254(reg):
    subnets = sr.pool_subnets()
    assert len(subnets) == 254
    assert subnets[0] == "10.255.1.0/24"
    assert subnets[-1] == "10.255.254.0/24"


def test_default_aggregate_is_10_255_0_0_16():
    assert sr.pool_aggregate() == "10.255.0.0/16"


def test_pool_exhaustion_raises(reg):
    for i in range(len(sr.pool_subnets())):
        sr.allocate(f"t{i}", reg)
    with pytest.raises(RuntimeError, match="exhausted"):
        sr.allocate("overflow", reg)


def test_env_override_pool(reg, monkeypatch):
    monkeypatch.setenv(sr.POOL_ENV_VAR, "10.200.0.0/16")
    assert sr.pool_aggregate() == "10.200.0.0/16"
    assert sr.allocate("a", reg) == "10.200.1.0/24"
    assert sr.pool_subnets()[-1] == "10.200.254.0/24"


def test_env_override_smaller_block(reg, monkeypatch):
    # A /20 yields 16 /24s; edge /24s (.0 and .15) dropped -> 14 usable.
    monkeypatch.setenv(sr.POOL_ENV_VAR, "10.42.0.0/20")
    subnets = sr.pool_subnets()
    assert subnets[0] == "10.42.1.0/24"
    assert subnets[-1] == "10.42.14.0/24"
    assert len(subnets) == 14


def test_env_override_bad_cidr_raises(monkeypatch):
    monkeypatch.setenv(sr.POOL_ENV_VAR, "not-a-cidr")
    with pytest.raises(ValueError, match="RANGECTL_MGMT_POOL"):
        sr.pool_subnets()


def test_env_override_too_small_prefix_raises(monkeypatch):
    # A /25 cannot be carved into /24s.
    monkeypatch.setenv(sr.POOL_ENV_VAR, "10.9.0.0/25")
    with pytest.raises(ValueError, match="at least /24"):
        sr.pool_subnets()


def test_explicit_pool_beats_env(reg, monkeypatch):
    monkeypatch.setenv(sr.POOL_ENV_VAR, "10.200.0.0/16")
    assert sr.pool_aggregate("10.111.0.0/16") == "10.111.0.0/16"
    assert sr.allocate("a", reg, pool="10.111.0.0/16") == "10.111.1.0/24"


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
