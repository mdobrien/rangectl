from __future__ import annotations

from rangectl.readiness import command_succeeds, ping, port_open, process_running
from rangectl.types import ReadinessProbe


def test_port_open_returns_probe() -> None:
    p = port_open(8080)
    assert isinstance(p, ReadinessProbe)
    assert p.probe_type == "port"
    assert p.target == 8080


def test_port_open_custom_timeout() -> None:
    p = port_open(22, timeout=60)
    assert p.timeout == 60


def test_ping_returns_probe() -> None:
    p = ping()
    assert isinstance(p, ReadinessProbe)
    assert p.probe_type == "ping"
    assert p.target is None


def test_process_running_returns_probe() -> None:
    p = process_running("sshd")
    assert isinstance(p, ReadinessProbe)
    assert p.probe_type == "process"
    assert p.target == "sshd"


def test_command_succeeds_returns_probe() -> None:
    p = command_succeeds("systemctl is-active nginx")
    assert isinstance(p, ReadinessProbe)
    assert p.probe_type == "command"
    assert p.target == "systemctl is-active nginx"
