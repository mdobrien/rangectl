"""Network namespace management for per-range isolation (Phase 8).

Each range runs inside its own named network namespace. Bridges live inside
the namespace, so their names are clean and unscoped — ``mgmt-br``, ``data-0``,
``data-1`` — with no IFNAMSIZ hashing, because collisions across ranges are
structurally impossible.

The management network connects the range's ``mgmt-br`` to the host via a veth
pair. The host-side veth carries the ``.254`` gateway address; VMs reach the
host (and the host reaches VMs) over that L2 segment. A FORWARD ACCEPT rule for
the management CIDR lets the host route traffic into the range when acting as a
gateway for remote dev access.
"""
from __future__ import annotations
import hashlib
import logging
import subprocess
from dataclasses import dataclass

from rangectl.networking import mgmt_host_ip

log = logging.getLogger(__name__)

MGMT_BRIDGE = "mgmt-br"


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    log.debug("RUN: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
    return result


@dataclass
class MgmtNetwork:
    bridge_name: str      # "mgmt-br" (clean name, no hashing)
    veth_host: str        # host-side veth interface
    veth_ns: str          # ns-side veth interface (enslaved to mgmt-br)
    host_ip: str          # .254 on the host side
    subnet: str           # e.g. "10.255.1.0/24"


def _mgmt_veth_names(range_name: str) -> tuple[str, str]:
    """Kernel-legal (<=15 char), host-unique veth names for a range's mgmt link.

    Host-side veth lives in the host namespace alongside every other range's,
    so the name must be globally unique — we hash the range name.
    """
    h = hashlib.sha1(range_name.encode()).hexdigest()[:8]
    return (f"mgh{h}", f"mgp{h}")


def _add_bridge_in_netns(netns_name: str, bridge: str) -> None:
    res = _run(
        ["ip", "netns", "exec", netns_name,
         "ip", "link", "add", bridge, "type", "bridge"],
        check=False,
    )
    if res.returncode != 0 and "exists" not in (res.stderr or "").lower():
        raise RuntimeError(f"ip link add {bridge} failed: {res.stderr}")
    _run(["ip", "netns", "exec", netns_name, "ip", "link", "set", bridge, "up"])


def _iptables_forward_accept(subnet: str) -> None:
    """Insert FORWARD ACCEPT rules for the mgmt CIDR (idempotent)."""
    for direction in ("-s", "-d"):
        rule = [direction, subnet, "-j", "ACCEPT"]
        if _run(["iptables", "-C", "FORWARD", *rule], check=False).returncode == 0:
            continue
        _run(["iptables", "-I", "FORWARD", "1", *rule], check=False)


def _ensure_mgmt_isolation() -> None:
    """Block forwarding *between* ranges' management interfaces.

    ``ip_forward=1`` (needed for internet=full MASQUERADE) otherwise lets the
    host route packets from one range's mgmt subnet to another's, because the
    host carries an on-link ``.254`` address on every range's host-side veth
    (``mgh<hash>``) and legacy mgmt bridge (``rlmgt-<hash>``). The per-subnet
    ACCEPT rules above would then permit the cross-range hop. DROPs for every
    ordered pair of mgmt prefixes — including the cross-scheme ``mgh+ <-> rlmgt+``
    pair when namespace and legacy ranges run concurrently — close that path
    while leaving host<->range and range->internet (``-o <uplink>``) untouched.

    Kept at the very top of FORWARD (delete-then-insert avoids duplicates and
    re-promotes them above the per-subnet ACCEPTs every range adds). They are
    shared, range-agnostic rules, so teardown deliberately leaves them in place."""
    from rangectl.networking import mgmt_isolation_rules
    for rule in mgmt_isolation_rules():
        _run(["iptables", "-D", "FORWARD", *rule], check=False)
        _run(["iptables", "-I", "FORWARD", "1", *rule], check=False)


def create_mgmt_network(netns_name: str, mgmt_subnet: str,
                        range_name: str) -> MgmtNetwork:
    """Create the mgmt bridge in ``netns_name``, link it to the host via a veth
    pair, assign the host gateway IP, and allow forwarding for the subnet."""
    log.info("create_mgmt_network: ns=%s subnet=%s range=%s",
             netns_name, mgmt_subnet, range_name)
    host_ip = mgmt_host_ip(mgmt_subnet)
    prefix = mgmt_subnet.split("/", 1)[1]
    veth_host, veth_ns = _mgmt_veth_names(range_name)

    _add_bridge_in_netns(netns_name, MGMT_BRIDGE)

    _run(["ip", "link", "add", veth_host, "type", "veth",
          "peer", "name", veth_ns])
    _run(["ip", "link", "set", veth_ns, "netns", netns_name])
    _run(["ip", "netns", "exec", netns_name,
          "ip", "link", "set", veth_ns, "master", MGMT_BRIDGE])
    _run(["ip", "netns", "exec", netns_name,
          "ip", "link", "set", veth_ns, "up"])

    _run(["ip", "link", "set", veth_host, "up"])
    _run(["ip", "addr", "add", f"{host_ip}/{prefix}", "dev", veth_host])

    _iptables_forward_accept(mgmt_subnet)
    # Re-assert inter-range isolation last, so its DROP sits above the per-subnet
    # ACCEPTs this range (and every other range) inserts.
    _ensure_mgmt_isolation()

    return MgmtNetwork(
        bridge_name=MGMT_BRIDGE,
        veth_host=veth_host,
        veth_ns=veth_ns,
        host_ip=host_ip,
        subnet=mgmt_subnet,
    )


def destroy_mgmt_network(mgmt: MgmtNetwork) -> None:
    """Tear down the management network. Deleting the host-side veth removes the
    whole pair; the connected route disappears with it."""
    log.info("destroy_mgmt_network: veth_host=%s subnet=%s",
             mgmt.veth_host, mgmt.subnet)
    _run(["ip", "link", "delete", mgmt.veth_host], check=False)
    for direction in ("-s", "-d"):
        _run(["iptables", "-D", "FORWARD", direction, mgmt.subnet,
              "-j", "ACCEPT"], check=False)


def create_data_bridge(netns_name: str, bridge_name: str) -> None:
    """Create a data-plane bridge inside the netns (clean name, idempotent)."""
    log.info("create_data_bridge: ns=%s bridge=%s", netns_name, bridge_name)
    _add_bridge_in_netns(netns_name, bridge_name)


def exec_in_netns(netns_name: str,
                  cmd: list[str]) -> subprocess.CompletedProcess:
    """Run ``cmd`` inside ``netns_name``. Returns the completed process; the
    caller decides how to handle a non-zero exit."""
    return _run(["ip", "netns", "exec", netns_name, *cmd], check=False)
