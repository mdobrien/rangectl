from __future__ import annotations

from dataclasses import is_dataclass

from rangectl.backend import HostResources
from rangectl.types import ExecResult, InterfaceSpec


def test_interface_spec_getitem_parses_ip_and_cidr():
    base = InterfaceSpec(node_name="n1", interface_name="eth1")
    sub = base["10.0.1.5/24"]
    assert sub.node_name == "n1"
    assert sub.interface_name == "eth1"
    assert sub.ip == "10.0.1.5"
    assert sub.cidr == "24"


def test_interface_spec_getitem_returns_new_instance():
    base = InterfaceSpec(node_name="n1", interface_name="eth0")
    sub = base["10.0.0.1/30"]
    assert base.ip is None
    assert base.cidr is None
    assert sub is not base


def test_exec_result_dataclass_fields():
    r = ExecResult(exit_code=0, stdout="hi", stderr="")
    assert is_dataclass(r)
    assert r.exit_code == 0
    assert r.stdout == "hi"
    assert r.stderr == ""


def test_host_resources_is_dataclass():
    r = HostResources(
        total_vcpu=1, total_memory_mb=1, total_disk_mb=1,
        available_vcpu=1, available_memory_mb=1, available_disk_mb=1,
    )
    assert is_dataclass(r)
    assert r.total_vcpu == 1
