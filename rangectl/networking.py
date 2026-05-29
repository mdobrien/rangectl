from __future__ import annotations
import hashlib
import ipaddress

HOST_OFFSET = 254  # host always at .254 within a /24 mgmt subnet
IFNAME_MAX = 15  # Linux IFNAMSIZ - 1


def _net(subnet: str) -> ipaddress.IPv4Network:
    return ipaddress.IPv4Network(subnet, strict=False)


def _short_topo(topology_name: str) -> str:
    """6-char stable hash of the topology name, for use in Linux ifnames."""
    return hashlib.sha1(topology_name.encode()).hexdigest()[:6]


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
    # Linux ifnames cap at 15 chars; hash the topology name so any topology
    # name fits regardless of length.
    return f"rl-{_short_topo(topology_name)}-{index}"


def mgmt_bridge_name(topology_name: str) -> str:
    return f"rlmgt-{_short_topo(topology_name)}"


# --- netns-scoped names (v2) ---------------------------------------------
# Inside a range's network namespace, bridge names are scoped to the namespace,
# so collisions across ranges are impossible and no hashing is needed. These
# clean names replace the hashed ones once the engine is wired for per-range
# namespaces (Phase 11). The hashed helpers above remain for backward compat.

def ns_bridge_name(index: int) -> str:
    return f"data-{index}"


def ns_mgmt_bridge_name() -> str:
    return "mgmt-br"
