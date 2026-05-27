from __future__ import annotations

import pytest

from rangectl import Topology
from rangectl.backend import HostResources
from rangectl.engine import Engine
from rangectl.types import ResourceError


def _topo_two_nodes() -> Topology:
    t = Topology("t1")
    t.node("a", image="ubuntu", vcpu=2, memory=2048)
    t.node("b", image="ubuntu", vcpu=4, memory=4096)
    return t


def test_validate_resources_sufficient(backend, db):
    backend.host_resources_result = HostResources(
        total_vcpu=16, total_memory_mb=32768, total_disk_mb=500_000,
        available_vcpu=16, available_memory_mb=32768, available_disk_mb=500_000,
    )
    engine = Engine(backend, db)
    engine.validate_resources(_topo_two_nodes())  # no raise


def test_validate_resources_insufficient_vcpu(backend, db):
    backend.host_resources_result = HostResources(
        total_vcpu=4, total_memory_mb=32768, total_disk_mb=500_000,
        available_vcpu=4, available_memory_mb=32768, available_disk_mb=500_000,
    )
    engine = Engine(backend, db)
    with pytest.raises(ResourceError, match="vcpu"):
        engine.validate_resources(_topo_two_nodes())


def test_validate_resources_insufficient_memory(backend, db):
    backend.host_resources_result = HostResources(
        total_vcpu=16, total_memory_mb=4096, total_disk_mb=500_000,
        available_vcpu=16, available_memory_mb=4096, available_disk_mb=500_000,
    )
    engine = Engine(backend, db)
    with pytest.raises(ResourceError, match="memory"):
        engine.validate_resources(_topo_two_nodes())


def test_validate_resources_exactly_enough(backend, db):
    backend.host_resources_result = HostResources(
        total_vcpu=6, total_memory_mb=6144, total_disk_mb=500_000,
        available_vcpu=6, available_memory_mb=6144, available_disk_mb=500_000,
    )
    engine = Engine(backend, db)
    engine.validate_resources(_topo_two_nodes())  # no raise
