"""Unit tests for Phase 9 — rangectl.cgroup.

cgroup v2 resource limits, freeze/thaw, and PID placement. Tests redirect the
cgroup root to a tmp directory and assert real file contents, rather than
mocking every write.
"""
from __future__ import annotations
from pathlib import Path

import pytest

from rangectl import cgroup
from rangectl.cgroup import Resources


@pytest.fixture
def cgroot(tmp_path, monkeypatch):
    monkeypatch.setattr(cgroup, "CGROUP_ROOT", tmp_path)
    return tmp_path


# --- Resources dataclass ---------------------------------------------------

def test_resources_defaults_all_none():
    r = Resources()
    assert r.memory is None and r.cpus is None
    assert r.pids is None and r.cpuset is None


def test_resources_holds_values():
    r = Resources(memory="32G", cpus=8, pids=500, cpuset="0-7")
    assert r.memory == "32G"
    assert r.cpus == 8
    assert r.pids == 500
    assert r.cpuset == "0-7"


# --- create_cgroup ---------------------------------------------------------

def test_create_cgroup_returns_path(cgroot):
    path = cgroup.create_cgroup("lab1", Resources())
    assert path == str(cgroot / "rangectl-lab1")
    assert Path(path).is_dir()


def test_create_cgroup_writes_memory_limit_in_bytes(cgroot):
    cgroup.create_cgroup("lab1", Resources(memory="32G"))
    mem = (cgroot / "rangectl-lab1" / "memory.max").read_text()
    assert mem == str(32 * 1024 ** 3)


def test_create_cgroup_memory_suffixes(cgroot):
    cgroup.create_cgroup("m", Resources(memory="512M"))
    assert (cgroot / "rangectl-m" / "memory.max").read_text() == str(512 * 1024 ** 2)
    cgroup.create_cgroup("k", Resources(memory="1024K"))
    assert (cgroot / "rangectl-k" / "memory.max").read_text() == str(1024 * 1024)


def test_create_cgroup_writes_cpu_quota(cgroot):
    cgroup.create_cgroup("lab1", Resources(cpus=8))
    # cgroup v2 cpu.max format: "<quota> <period>"; 8 cores over a 100000us period.
    assert (cgroot / "rangectl-lab1" / "cpu.max").read_text() == "800000 100000"


def test_create_cgroup_writes_pids_and_cpuset(cgroot):
    cgroup.create_cgroup("lab1", Resources(pids=500, cpuset="0-7"))
    assert (cgroot / "rangectl-lab1" / "pids.max").read_text() == "500"
    assert (cgroot / "rangectl-lab1" / "cpuset.cpus").read_text() == "0-7"


def test_create_cgroup_skips_unset_limits(cgroot):
    cgroup.create_cgroup("lab1", Resources(memory="1G"))
    cg = cgroot / "rangectl-lab1"
    assert (cg / "memory.max").exists()
    # Limits not requested are not written.
    assert not (cg / "cpu.max").exists()
    assert not (cg / "pids.max").exists()
    assert not (cg / "cpuset.cpus").exists()


# --- freeze / thaw ---------------------------------------------------------

def test_freeze_writes_one(cgroot):
    cgroup.create_cgroup("lab1", Resources())
    cgroup.freeze("lab1")
    assert (cgroot / "rangectl-lab1" / "cgroup.freeze").read_text() == "1"


def test_thaw_writes_zero(cgroot):
    cgroup.create_cgroup("lab1", Resources())
    cgroup.freeze("lab1")
    cgroup.thaw("lab1")
    assert (cgroot / "rangectl-lab1" / "cgroup.freeze").read_text() == "0"


# --- write_pid -------------------------------------------------------------

def test_write_pid_appends_to_procs(cgroot):
    path = cgroup.create_cgroup("lab1", Resources())
    cgroup.write_pid(path, 4242)
    assert (Path(path) / "cgroup.procs").read_text() == "4242"


# --- destroy_cgroup --------------------------------------------------------

def test_destroy_cgroup_removes_dir(cgroot):
    cgroup.create_cgroup("lab1", Resources())
    cgroup.destroy_cgroup("lab1")
    assert not (cgroot / "rangectl-lab1").exists()


def test_destroy_cgroup_missing_is_noop(cgroot):
    # Destroying a range that was never created must not raise.
    cgroup.destroy_cgroup("never")
