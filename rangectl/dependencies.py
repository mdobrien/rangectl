from __future__ import annotations
import logging
from typing import Callable

from rangectl.types import InstallSpec, OSType, ServiceSpec, ReadinessProbe

log = logging.getLogger(__name__)


class DependencyMixin:
    """Shared dependency methods for Node and DependencySet."""

    def __init__(self) -> None:
        self._packages: list[str] = []
        self._powershell_commands: list[str] = []
        self._installs: list[InstallSpec] = []
        self._configure_fns: list[Callable] = []
        self._services: list[ServiceSpec] = []
        self._files: list[tuple[str, str]] = []  # (dst, src)
        self._users: list[dict] = []
        self._boot_commands: list[str] = []

    def packages(self, packages: list[str]) -> None:
        log.info("Registering packages: %s", packages)
        self._packages.extend(packages)

    def powershell(self, command: str) -> None:
        log.info("Registering powershell command: %s", command)
        self._powershell_commands.append(command)

    def install(
        self,
        name: str,
        src: str,
        install_cmd: str,
        verify_cmd: str | None = None,
    ) -> None:
        log.info("Registering custom install: %s", name)
        self._installs.append(InstallSpec(name, src, install_cmd, verify_cmd))

    def file(self, dst: str, src: str) -> None:
        log.info("Registering file: %s -> %s", src, dst)
        self._files.append((dst, src))

    def user(self, name: str, ssh_key: str | None = None, password: str | None = None) -> None:
        log.info("Registering user: %s", name)
        self._users.append({"name": name, "ssh_key": ssh_key, "password": password})

    def run_on_boot(self, command: str) -> None:
        log.info("Registering boot command: %s", command)
        self._boot_commands.append(command)

    def service(
        self,
        name: str,
        enabled: bool = False,
        start_cmd: str | None = None,
        ready_when: ReadinessProbe | None = None,
    ) -> None:
        log.info("Registering service: %s", name)
        self._services.append(ServiceSpec(name, enabled, start_cmd, ready_when))

    def configure(self, fn: Callable) -> Callable:
        log.info("Registering configure function: %s", fn.__name__)
        self._configure_fns.append(fn)
        return fn

    def apply(self, dep_set: DependencySet) -> None:
        log.info("Applying dependency set: %s", dep_set.name)
        self._packages.extend(dep_set._packages)
        self._powershell_commands.extend(dep_set._powershell_commands)
        self._installs.extend(dep_set._installs)
        self._configure_fns.extend(dep_set._configure_fns)
        self._services.extend(dep_set._services)
        self._files.extend(dep_set._files)
        self._users.extend(dep_set._users)
        self._boot_commands.extend(dep_set._boot_commands)


class DependencySet(DependencyMixin):

    def __init__(self, name: str, os: OSType = OSType.LINUX) -> None:
        super().__init__()
        self.name = name
        self.os = os
