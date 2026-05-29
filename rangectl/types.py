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


VALID_TRANSITIONS: dict["NodeState", set["NodeState"]] = {
    NodeState.DEFINED: {NodeState.PROVISIONING, NodeState.FAILED},
    NodeState.PROVISIONING: {NodeState.READY, NodeState.FAILED},
    NodeState.READY: {NodeState.LINKED, NodeState.FAILED},
    NodeState.LINKED: {NodeState.RUNNING, NodeState.FAILED},
    NodeState.RUNNING: {NodeState.DESTROYING, NodeState.FAILED},
    NodeState.DESTROYING: {NodeState.DESTROYED, NodeState.FAILED},
    NodeState.DESTROYED: set(),
    NodeState.FAILED: set(),
}


class InvalidTransitionError(Exception):
    pass


def transition_node_state(current: "NodeState", target: "NodeState") -> "NodeState":
    """Validate the transition and return the new state, or raise."""
    if target not in VALID_TRANSITIONS.get(current, set()):
        raise InvalidTransitionError(
            f"cannot transition {current.value} -> {target.value}"
        )
    return target


class InjectMethod(Enum):
    PRE_BAKED = "pre-baked"
    CLOUD_INIT = "cloud-init"
    CLOUDBASE_INIT = "cloudbase-init"
    GUEST_AGENT = "guest-agent"


class OSType(Enum):
    LINUX = "linux"
    WINDOWS = "windows"
    VYOS = "vyos"


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
    bridge: str | None = None
    mac: str | None = None

    def __getitem__(self, ip_cidr: str) -> InterfaceSpec:
        ip, cidr = ip_cidr.split("/", 1)
        return InterfaceSpec(
            node_name=self.node_name,
            interface_name=self.interface_name,
            ip=ip,
            cidr=cidr,
            bridge=self.bridge,
            mac=self.mac,
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
    seed_iso_path: str | None = None
    mgmt_ip: str | None = None
    topology_name: str | None = None
    ssh_user: str = "ubuntu"
    ssh_password: str | None = None
