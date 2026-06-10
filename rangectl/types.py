from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class ResourceError(Exception):
    pass


class CycleError(Exception):
    pass


class RangeNotRunning(Exception):
    """Raised by Range.connect() when a range's persisted state exists but the
    range is not actually running (dead libvirtd, missing netns/socket, or no
    such topology in the state DB)."""

    def __init__(self, name: str, reason: str = "") -> None:
        self.name = name
        msg = f"range '{name}' is not running"
        if reason:
            msg += f": {reason}"
        super().__init__(msg)


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
    CONTAINER = "container"
    # L2 device types (Phase 20) — boot-free nodes whose "body" is a Linux
    # bridge inside the range netns. Stored in the nodes table os_type column.
    SWITCH = "switch"
    HUB = "hub"

    @classmethod
    def register(cls, name: str, driver_cls) -> None:
        """Register a custom OS driver so nodes of this type get OS-specific
        behaviour (route, sysctl, ...). ``name`` is the os_type string used at
        node declaration / in the state DB."""
        from rangectl.drivers import register_driver
        register_driver(name, driver_cls)


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


def _validate_vid(vid: Any) -> None:
    """802.1Q VIDs are 1-4094 (0 and 4095 are reserved by the standard)."""
    if not isinstance(vid, int) or isinstance(vid, bool) or not 1 <= vid <= 4094:
        raise ValueError(f"invalid VLAN VID {vid!r}: must be an integer 1-4094")


@dataclass
class PortSpec(InterfaceSpec):
    """A port on an L2 device (switch/hub) — Phase 25.

    On a ``vlan_aware=True`` switch a port is configured as either an
    **access** port (one VLAN, untagged on the wire, PVID on ingress) or a
    **trunk** port (multiple tagged VLANs; ``native=`` maps untagged frames
    to that VID). A port is access XOR trunk. Unconfigured ports keep the
    kernel bridge default: PVID 1, untagged.
    """
    l2_node: Any = field(default=None, repr=False, compare=False)
    # {"mode": "access"|"trunk", "vids": [int, ...], "native": int|None}
    vlan: dict | None = None

    def _require_vlan_aware(self, method: str) -> None:
        node = self.l2_node
        if node is None or not getattr(node, "vlan_aware", False):
            kind = node.os_type.value if node is not None else "device"
            raise ValueError(
                f"{self.node_name}.{self.interface_name}.{method}(): "
                f"{kind} '{self.node_name}' is not vlan-aware; declare it "
                f"with switch('{self.node_name}', vlan_aware=True)"
            )

    def _reject_reconfigure(self) -> None:
        if self.vlan is not None:
            raise ValueError(
                f"{self.node_name}.{self.interface_name} is already "
                f"configured as {self.vlan['mode']} "
                f"(vids={self.vlan['vids']}); a port is access XOR trunk"
            )

    def access(self, vid: int) -> PortSpec:
        """Make this an access port: untagged member of ``vid``, PVID set."""
        self._require_vlan_aware("access")
        _validate_vid(vid)
        self._reject_reconfigure()
        self.vlan = {"mode": "access", "vids": [int(vid)], "native": None}
        return self

    def trunk(self, *vids: int, native: int | None = None) -> PortSpec:
        """Make this a trunk port carrying ``vids`` tagged; untagged frames
        map to ``native`` if given (otherwise they are dropped)."""
        self._require_vlan_aware("trunk")
        if not vids:
            raise ValueError(
                f"{self.node_name}.{self.interface_name}.trunk() requires "
                "at least one VID"
            )
        for v in vids:
            _validate_vid(v)
        if native is not None:
            _validate_vid(native)
        self._reject_reconfigure()
        self.vlan = {"mode": "trunk", "vids": [int(v) for v in vids],
                     "native": native}
        return self


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
