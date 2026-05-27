from __future__ import annotations
import ipaddress

HOST_OFFSET = 254  # host always at .254 within a /24 mgmt subnet


def _net(subnet: str) -> ipaddress.IPv4Network:
    return ipaddress.IPv4Network(subnet, strict=False)


def allocate_mgmt_ip(subnet: str, index: int) -> str:
    """Return the i-th guest mgmt IP within ``subnet``.

    Guests start at ``.1`` (index 0). The host reserves ``.254`` —
    indexes that would land on it raise ValueError.
    """
    net = _net(subnet)
    offset = index + 1
    if offset >= HOST_OFFSET:
        raise ValueError(
            f"index {index} collides with host IP {HOST_OFFSET} in {subnet}"
        )
    return str(net.network_address + offset)


def mgmt_host_ip(subnet: str) -> str:
    net = _net(subnet)
    return str(net.network_address + HOST_OFFSET)


def bridge_name(topology_name: str, index: int) -> str:
    return f"{topology_name}-br{index}"


def mgmt_bridge_name(topology_name: str) -> str:
    return f"rangectl-mgmt-{topology_name}"
