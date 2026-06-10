"""Per-range internet access policy via iptables (Phase 12 / Phase 16).

Internet access is controlled with a dedicated NAT chain per range,
``RANGE-<name>``. Each range's mgmt veth (``mgh<hash>``) is the choke point —
all range traffic flows through it — so enabling internet is a matter of
MASQUERADE-ing that traffic out the namespace's default-route interface and
allowing it to be forwarded.

Since Phase 16 every per-range chain/rule lives INSIDE the persistent
management namespace (``rangectl-mgmt``), not on the host: callers pass
``netns=MGMT_NS`` and every iptables/ip command is prefixed with
``ip netns exec rangectl-mgmt``. Inside the mgmt-ns the default route egresses
``veth-mgmt-ns`` toward the host, where a single static MASQUERADE performs the
real egress NAT. A range with ``internet=none`` simply has no chain, so its
packets are never MASQUERADEd to the transit source the host NATs — preserving
the "no chain == no internet" gating for free.

Per-range chains keep ranges independent: tearing one down flushes and deletes
only its own ``RANGE-<name>`` chain, never touching another range's rules.

| Policy | Behavior |
|--------|----------|
| none   | No outbound internet. VMs still reach each other + the host. Default. |
| full   | MASQUERADE all range traffic out toward the host uplink. |
"""
from __future__ import annotations
import logging
import subprocess

log = logging.getLogger(__name__)


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    log.debug("RUN: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
    return result


def _ns_prefix(netns: str | None) -> list[str]:
    """``ip netns exec <netns>`` prefix, or empty for host-namespace commands."""
    return ["ip", "netns", "exec", netns] if netns else []


def chain_name(range_name: str) -> str:
    """The per-range NAT chain name."""
    return f"RANGE-{range_name}"


def detect_outbound_iface(netns: str | None = None) -> str | None:
    """The interface carrying the default route — where traffic egresses.

    With ``netns`` set, inspects that namespace's table (in ``rangectl-mgmt``
    this returns ``veth-mgmt-ns``). Returns None when there's no default route.
    """
    r = _run([*_ns_prefix(netns), "ip", "-o", "-4", "route", "show", "default"],
             check=False)
    parts = r.stdout.split()
    if "dev" in parts:
        return parts[parts.index("dev") + 1]
    return None


def _forward_rules(veth_host: str) -> list[list[str]]:
    """FORWARD rules that let range traffic egress and return through the veth."""
    return [
        ["-i", veth_host, "-j", "ACCEPT"],
        ["-o", veth_host, "-m", "state", "--state",
         "RELATED,ESTABLISHED", "-j", "ACCEPT"],
    ]


def _ensure(prefix: list[str], table_args: list[str], chain: str,
            rule: list[str]) -> None:
    """Append ``rule`` to ``chain`` only if it isn't already present (idempotent)."""
    if _run([*prefix, "iptables", *table_args, "-C", chain, *rule],
            check=False).returncode == 0:
        return
    _run([*prefix, "iptables", *table_args, "-A", chain, *rule])


def enable_internet(range_name: str, mgmt_subnet: str, veth_host: str,
                    outbound_iface: str | None = None,
                    netns: str | None = None) -> str:
    """Grant the range outbound internet via MASQUERADE out the namespace uplink.

    Creates (idempotently) the ``RANGE-<name>`` NAT chain, MASQUERADEs the
    range's traffic out ``outbound_iface``, jumps to the chain from POSTROUTING
    for the range's mgmt subnet, and allows the veth to be forwarded. When
    ``netns`` is set every command runs inside that namespace (Phase 16:
    ``rangectl-mgmt``). Returns the outbound interface used.
    """
    prefix = _ns_prefix(netns)
    outbound_iface = outbound_iface or detect_outbound_iface(netns)
    if not outbound_iface:
        raise RuntimeError(
            "cannot enable internet: no default-route interface found"
        )
    chain = chain_name(range_name)
    log.info("enable_internet: range=%s subnet=%s veth=%s out=%s chain=%s ns=%s",
             range_name, mgmt_subnet, veth_host, outbound_iface, chain, netns)

    # Per-range NAT chain (tolerate "chain already exists").
    _run([*prefix, "iptables", "-t", "nat", "-N", chain], check=False)
    _ensure(prefix, ["-t", "nat"], chain, ["-o", outbound_iface, "-j", "MASQUERADE"])
    _ensure(prefix, ["-t", "nat"], "POSTROUTING", ["-s", mgmt_subnet, "-j", chain])

    for rule in _forward_rules(veth_host):
        _ensure(prefix, [], "FORWARD", rule)
    return outbound_iface


def disable_internet(range_name: str, mgmt_subnet: str, veth_host: str,
                     netns: str | None = None) -> None:
    """Revoke outbound internet for the range and remove only its own rules.

    Deletes the FORWARD allowances, the POSTROUTING jump, and flushes + deletes
    the ``RANGE-<name>`` chain. Every step tolerates an already-absent rule so
    this is safe to call on teardown regardless of prior state. When ``netns``
    is set every command runs inside that namespace (Phase 16).
    """
    prefix = _ns_prefix(netns)
    chain = chain_name(range_name)
    log.info("disable_internet: range=%s subnet=%s veth=%s chain=%s ns=%s",
             range_name, mgmt_subnet, veth_host, chain, netns)

    for rule in _forward_rules(veth_host):
        _run([*prefix, "iptables", "-D", "FORWARD", *rule], check=False)
    _run([*prefix, "iptables", "-t", "nat", "-D", "POSTROUTING",
          "-s", mgmt_subnet, "-j", chain], check=False)
    _run([*prefix, "iptables", "-t", "nat", "-F", chain], check=False)
    _run([*prefix, "iptables", "-t", "nat", "-X", chain], check=False)
