from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class ResourceError(Exception):
    pass


class CycleError(Exception):
    pass


class NodeState(Enum):
    DEFINED = "defined"
    PROVISIONING = "provisioning"
    READY = "ready"
    LINKED = "linked"
    RUNNING = "running"
    DESTROYING = "destroying"
    DESTROYED = "destroyed"
    FAILED = "failed"


class InjectMethod(Enum):
    PRE_BAKED = "pre-baked"
    CLOUD_INIT = "cloud-init"
    CLOUDBASE_INIT = "cloudbase-init"
    GUEST_AGENT = "guest-agent"


class OSType(Enum):
    LINUX = "linux"
    WINDOWS = "windows"


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


@dataclass
class InterfaceSpec:
    node_name: str
    interface_name: str
    ip: str | None = None
    cidr: str | None = None

    def __getitem__(self, ip_cidr: str) -> InterfaceSpec:
        ip, cidr = ip_cidr.split("/", 1)
        return InterfaceSpec(
            node_name=self.node_name,
            interface_name=self.interface_name,
            ip=ip,
            cidr=cidr,
        )


@dataclass
class ServiceSpec:
    name: str
    enabled: bool = False
    start_cmd: str | None = None
    ready_when: ReadinessProbe | None = None


@dataclass
class InstallSpec:
    name: str
    src: str
    install_cmd: str
    verify_cmd: str | None = None


@dataclass
class ReadinessProbe:
    probe_type: str
    target: str | int | None = None
    timeout: int = 300
    interval: int = 5


@dataclass
class VMSpec:
    name: str
    image: str
    vcpu: int
    memory: int  # MB
    os_type: OSType
    interfaces: list[InterfaceSpec] = field(default_factory=list)
    inject: InjectMethod = InjectMethod.PRE_BAKED
    overlay_path: str | None = None
