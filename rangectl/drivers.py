"""OS driver abstraction — per-OS translation of high-level node operations.

A driver receives an already-authenticated transport (the node's backend +
vm_id) and turns generic requests (route, sysctl, install packages) into the
commands a particular OS understands. The backend hides the transport detail:
SSH/SFTP for Linux/VyOS, ``docker exec``/``docker cp`` for containers — so every
real driver shares the same exec/put plumbing and differs only in the command
strings it emits.
"""
from __future__ import annotations

from typing import Any

from rangectl.types import ExecResult, OSType


class OSDriver:
    """Base class. ``put`` and ``exec`` are required; everything else is
    optional and raises ``NotImplementedError`` until a subclass implements it
    for its OS. Extensible for new platforms via ``OSType.register()``."""

    def __init__(self, backend: Any = None, vm_id: str | None = None) -> None:
        self._backend = backend
        self._vm_id = vm_id

    # Required — subclasses must implement.
    def put(self, src: str, dst: str) -> None:
        raise NotImplementedError

    def put_dir(self, src: str, dst: str) -> None:
        raise NotImplementedError

    def exec(self, cmd: str) -> ExecResult:
        raise NotImplementedError

    # Optional — override per OS.
    def add_route(self, dest: str, via: str) -> ExecResult:
        raise NotImplementedError

    def set_sysctl(self, key: str, value: Any) -> ExecResult:
        raise NotImplementedError

    def install_packages(self, packages: list[str]) -> ExecResult:
        raise NotImplementedError

    def enable_service(self, name: str) -> ExecResult:
        raise NotImplementedError

    def add_user(self, name: str, ssh_key: str | None = None) -> None:
        raise NotImplementedError

    def firewall_allow(self, port: int, proto: str = "tcp") -> ExecResult:
        raise NotImplementedError


class _BackendDriver(OSDriver):
    """Shared transport for drivers backed by a rangectl Backend. exec/put
    delegate to ``backend.exec``/``backend.upload`` against the node's vm_id."""

    def exec(self, cmd: str) -> ExecResult:
        if self._backend is None or self._vm_id is None:
            raise RuntimeError("driver not bound to a backend; cannot exec")
        return self._backend.exec(self._vm_id, cmd)

    def put(self, src: str, dst: str) -> None:
        if self._backend is None or self._vm_id is None:
            raise RuntimeError("driver not bound to a backend; cannot put")
        self._backend.upload(self._vm_id, src, dst)

    def put_dir(self, src: str, dst: str) -> None:
        # Backends upload recursively for directories; same call as a file.
        self.put(src, dst)


class LinuxDriver(_BackendDriver):
    """Ubuntu, Debian, CentOS, Kali — SSH/SFTP + standard Linux commands.

    Cloud images run as the unprivileged ``ubuntu`` user, so config ops use
    sudo (matches the engine's dependency-injection behaviour)."""

    def add_route(self, dest: str, via: str) -> ExecResult:
        return self.exec(f"sudo ip route add {dest} via {via}")

    def set_sysctl(self, key: str, value: Any) -> ExecResult:
        return self.exec(f"sudo sysctl -w {key}={value}")

    def install_packages(self, packages: list[str]) -> ExecResult:
        pkgs = " ".join(packages)
        return self.exec(
            f"sudo DEBIAN_FRONTEND=noninteractive apt-get install -y {pkgs}")

    def enable_service(self, name: str) -> ExecResult:
        return self.exec(f"sudo systemctl enable --now {name}")

    def add_user(self, name: str, ssh_key: str | None = None) -> None:
        self.exec(f"sudo useradd -m -s /bin/bash {name}")
        if ssh_key:
            self.exec(
                f"sudo install -d -m 700 /home/{name}/.ssh && "
                f"echo {ssh_key!r} | sudo tee -a /home/{name}/.ssh/authorized_keys")

    def firewall_allow(self, port: int, proto: str = "tcp") -> ExecResult:
        return self.exec(
            f"sudo iptables -A INPUT -p {proto} --dport {port} -j ACCEPT")


class VyOSDriver(_BackendDriver):
    """VyOS routers — SSH after serial-console bootstrap. Operations map to the
    VyOS ``set`` configuration CLI."""

    def add_route(self, dest: str, via: str) -> ExecResult:
        return self.exec(f"set protocols static route {dest} next-hop {via}")

    def enable_service(self, name: str) -> ExecResult:
        return self.exec(f"set service {name}")


class ContainerDriver(_BackendDriver):
    """Docker containers — ``docker exec`` / ``docker cp`` via ContainerBackend.
    No SSH auth needed; exec/put are inherited from the shared transport."""


class WindowsDriver(OSDriver):
    """Skeleton — WinRM/cloudbase-init not implemented yet. Every method raises
    ``NotImplementedError``."""


_DRIVERS: dict[str, type[OSDriver]] = {
    "linux": LinuxDriver,
    "vyos": VyOSDriver,
    "container": ContainerDriver,
    "windows": WindowsDriver,
}


def register_driver(name: str | OSType, driver_cls: type[OSDriver]) -> None:
    """Register a driver for a (possibly custom) OS type. Used by
    ``OSType.register`` so users can plug in new platforms."""
    key = name.value if isinstance(name, OSType) else str(name)
    _DRIVERS[key] = driver_cls


def make_driver(os_type: OSType | str | None, backend: Any = None,
                vm_id: str | None = None) -> OSDriver:
    """Instantiate the driver for ``os_type`` bound to a node's transport.
    Falls back to LinuxDriver for unknown types."""
    if isinstance(os_type, OSType):
        key = os_type.value
    else:
        key = str(os_type or "linux")
    cls = _DRIVERS.get(key, LinuxDriver)
    return cls(backend, vm_id)
