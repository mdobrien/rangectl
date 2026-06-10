from rangectl.topology import Range, Link, LiveNode, Node, Topology
from rangectl.capture import Capture
from rangectl.cgroup import Resources
from rangectl.dependencies import DependencySet
from rangectl.drivers import (
    ContainerDriver,
    LinuxDriver,
    OSDriver,
    VyOSDriver,
    WindowsDriver,
)
from rangectl.images import ImageBuilder, ImageRegistry
from rangectl.readiness import command_succeeds, ping, port_open, process_running
from rangectl.types import ExecResult, InjectMethod, OSType, RangeNotRunning
from rangectl.state import StateDB

__all__ = [
    "Topology",
    "Node",
    "Link",
    "Range",
    "LiveNode",
    "Capture",
    "Resources",
    "DependencySet",
    "OSDriver",
    "LinuxDriver",
    "VyOSDriver",
    "ContainerDriver",
    "WindowsDriver",
    "ImageBuilder",
    "ImageRegistry",
    "ExecResult",
    "InjectMethod",
    "OSType",
    "RangeNotRunning",
    "StateDB",
    "port_open",
    "ping",
    "process_running",
    "command_succeeds",
    "list_topologies",
]


def list_topologies() -> list[dict]:
    db = StateDB()
    try:
        return db.list_topologies()
    finally:
        db.close()
