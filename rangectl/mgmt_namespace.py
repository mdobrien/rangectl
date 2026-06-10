"""Persistent management namespace (Phase 16).

A single host-global netns, ``rangectl-mgmt``, is interposed between the host
and every range namespace. After this layer is in place the host carries only
**4 static operations** — created once, verified-and-healed before every deploy:

  1. veth ``veth-mgmt-host`` @ ``10.254.0.1/30`` (the transit link)
  2. route ``<pool aggregate>`` via ``10.254.0.2`` (reach every range mgmt subnet)
  3. FORWARD ACCEPT for the transit veth (+ established return)
  4. MASQUERADE ``-s <transit>`` out the host uplink (final egress NAT)

All per-range churn — the ``mgh<hash>``/``mgp<hash>`` veth pair, the ``.254``
gateway address, per-range FORWARD/ISOLATION rules, and the ``RANGE-<name>`` NAT
chain — happens INSIDE ``rangectl-mgmt`` (see ``netns.py`` / ``internet.py``).

Design (``scratch/issues/20260609-1-phase16-mgmt-ns-design.md``):
  * **D1 = Option C**: lazy ``ensure_mgmt_ns()`` before every namespace deploy,
    flock-guarded, verifying the full invariant and healing missing pieces. The
    kernel is the source of truth — no config flag says "it's set up".
  * **D3b**: a host route/address overlapping the transit /30 or pool aggregate
    that rangectl did not create is a HARD ABORT, never a warning.
  * **D5**: the mgmt-ns is never auto-destroyed — it is host infrastructure.

Egress NAT note (deviation from the spec's literal ``MASQUERADE -s 10.255/16``):
the host MASQUERADEs the **transit** subnet, not the pool aggregate. A range
with ``internet=full`` gets a ``RANGE-<name>`` chain in the mgmt-ns that
MASQUERADEs its traffic to ``10.254.0.2``; the host then MASQUERADEs that
transit source out the uplink. A range with ``internet=none`` has no mgmt-ns
chain, so its packets reach the host with their original ``10.255.x`` source,
which the host MASQUERADE does NOT match — so they never reach the internet.
This preserves today's "no NAT chain == no internet" gating for free, and keeps
the per-range NAT jump inside the mgmt-ns where ``status``/teardown can see it.
"""
from __future__ import annotations
import contextlib
import fcntl
import ipaddress
import json
import logging
import os
import subprocess
from pathlib import Path

from rangectl import internet, netns
from rangectl.networking import MGMT_NS
from rangectl.subnet_registry import pool_aggregate

log = logging.getLogger(__name__)

VETH_HOST = "veth-mgmt-host"
VETH_NS = "veth-mgmt-ns"

TRANSIT_ENV_VAR = "RANGECTL_MGMT_TRANSIT"
DEFAULT_TRANSIT = "10.254.0.0/30"

LOCK_PATH = "~/.rangectl/mgmt_ns.lock"
DEFAULT_RANGE_DIR = "/ranges"


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    log.debug("RUN: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
    return result


def _exec(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run ``cmd`` inside the mgmt-ns."""
    return _run(["ip", "netns", "exec", MGMT_NS, *cmd], check=check)


# --- configuration --------------------------------------------------------

def resolve_transit(explicit: str | None = None) -> ipaddress.IPv4Network:
    """Resolve the host<->mgmt-ns transit subnet (argument > env > default).

    Must be at least a /30 (two usable hosts: ``.1`` host, ``.2`` mgmt-ns).
    Raises ValueError naming ``RANGECTL_MGMT_TRANSIT`` on a bad value.
    """
    raw = explicit if explicit is not None else os.environ.get(TRANSIT_ENV_VAR)
    if raw is None:
        raw = DEFAULT_TRANSIT
    try:
        net = ipaddress.IPv4Network(raw, strict=False)
    except ValueError as e:
        raise ValueError(
            f"{TRANSIT_ENV_VAR}={raw!r} is not a valid IPv4 CIDR: {e}"
        ) from e
    if net.prefixlen > 30:
        raise ValueError(
            f"{TRANSIT_ENV_VAR}={raw!r} (/{net.prefixlen}) must be at least a "
            "/30 so it has a host (.1) and a mgmt-ns (.2) address"
        )
    return net


def _transit_ips(transit: ipaddress.IPv4Network) -> tuple[str, str, int]:
    """(host_ip, ns_ip, prefixlen) for the transit subnet."""
    return (str(transit.network_address + 1),
            str(transit.network_address + 2),
            transit.prefixlen)


@contextlib.contextmanager
def _flock():
    """Hold an exclusive flock so concurrent deploys serialize on ensure."""
    path = Path(LOCK_PATH).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# --- host-state probes ----------------------------------------------------

def _live_netns(name: str) -> bool:
    """True if the named netns currently exists (its /run/netns handle is present)."""
    return Path(f"/run/netns/{name}").exists()


def _ns_exists() -> bool:
    return _live_netns(MGMT_NS)


def _link_up(dev: str, netns_name: str | None = None) -> bool:
    pre = ["ip", "netns", "exec", netns_name] if netns_name else []
    r = _run([*pre, "ip", "-o", "link", "show", dev], check=False)
    if r.returncode != 0:
        return False
    # `state UP` or, for veths whose peer is down, `LOWERLAYERDOWN`/`UP` flag.
    return "state UP" in r.stdout or "UP" in r.stdout.split("<", 1)[-1].split(">", 1)[0]


def _link_exists(dev: str, netns_name: str | None = None) -> bool:
    pre = ["ip", "netns", "exec", netns_name] if netns_name else []
    return _run([*pre, "ip", "-o", "link", "show", dev], check=False).returncode == 0


def _has_addr(dev: str, addr: str, netns_name: str | None = None) -> bool:
    pre = ["ip", "netns", "exec", netns_name] if netns_name else []
    r = _run([*pre, "ip", "-o", "-4", "addr", "show", "dev", dev], check=False)
    return addr in r.stdout


def _has_route(dest: str, netns_name: str | None = None) -> bool:
    pre = ["ip", "netns", "exec", netns_name] if netns_name else []
    r = _run([*pre, "ip", "route", "show", dest], check=False)
    return bool(r.stdout.strip())


def _ip_forward(netns_name: str | None = None) -> bool:
    pre = ["ip", "netns", "exec", netns_name] if netns_name else []
    r = _run([*pre, "cat", "/proc/sys/net/ipv4/ip_forward"], check=False)
    return r.stdout.strip() == "1"


def _iptables_present(args: list[str], netns_name: str | None = None) -> bool:
    pre = ["ip", "netns", "exec", netns_name] if netns_name else []
    return _run([*pre, "iptables", "-C", *args], check=False).returncode == 0


# --- overlap abort (D3b) --------------------------------------------------

def _parse_routes() -> list[tuple[str, str]]:
    """[(dest_cidr, dev)] from the host routing table; skips ``default``."""
    out = _run(["ip", "route", "show"], check=False).stdout
    rows: list[tuple[str, str]] = []
    for line in out.splitlines():
        parts = line.split()
        if not parts or parts[0] == "default":
            continue
        dev = parts[parts.index("dev") + 1] if "dev" in parts else ""
        rows.append((parts[0], dev))
    return rows


def _parse_addrs() -> list[tuple[str, str]]:
    """[(cidr, dev)] for every host IPv4 address."""
    out = _run(["ip", "-o", "-4", "addr", "show"], check=False).stdout
    rows: list[tuple[str, str]] = []
    for line in out.splitlines():
        parts = line.split()
        # "<idx>: <dev> inet <cidr> ..."
        if len(parts) >= 4 and parts[2] == "inet":
            rows.append((parts[3], parts[1]))
    return rows


def _check_overlap(transit: ipaddress.IPv4Network,
                   aggregate: ipaddress.IPv4Network) -> None:
    """Abort if a host route/address rangectl did not create overlaps our subnets.

    Anything on ``veth-mgmt-host`` is ours (the transit link). The single
    ``<aggregate> via <ns-ip>`` route we install is ours. Anything else that
    overlaps the transit /30 or the pool aggregate is a foreign conflict.
    """
    targets = (transit, aggregate)

    def _conflicts(cidr: str) -> ipaddress.IPv4Network | None:
        try:
            net = ipaddress.IPv4Network(cidr, strict=False)
        except ValueError:
            return None
        for t in targets:
            if net.overlaps(t):
                return t
        return None

    for cidr, dev in _parse_addrs():
        if dev == VETH_HOST:
            continue
        hit = _conflicts(cidr)
        if hit is not None:
            raise RuntimeError(
                f"host address {cidr} on {dev!r} overlaps rangectl's {hit} — "
                f"remap via {TRANSIT_ENV_VAR} (transit) or RANGECTL_MGMT_POOL "
                "(pool aggregate)"
            )
    for cidr, dev in _parse_routes():
        if dev == VETH_HOST:
            continue
        # The aggregate route we install ourselves is not a conflict.
        if cidr == str(aggregate):
            continue
        hit = _conflicts(cidr)
        if hit is not None:
            raise RuntimeError(
                f"host route {cidr} (dev {dev or '?'}) overlaps rangectl's "
                f"{hit} — remap via {TRANSIT_ENV_VAR} (transit) or "
                "RANGECTL_MGMT_POOL (pool aggregate)"
            )


# --- ensure / heal --------------------------------------------------------

def ensure_mgmt_ns(transit: str | None = None, pool: str | None = None,
                   range_dir: str = DEFAULT_RANGE_DIR) -> None:
    """Create-or-heal the persistent mgmt-ns and its host-side static ops.

    Idempotent and flock-guarded. Verifies the full invariant (D2) and heals
    each missing piece individually. If the namespace itself was missing while
    ranges are still running, every running range is reconnected — making the
    kill/heal recovery path the same code as ordinary healing.
    """
    transit_net = resolve_transit(transit)
    aggregate = ipaddress.IPv4Network(pool_aggregate(pool))
    host_ip, ns_ip, prefix = _transit_ips(transit_net)
    uplink = internet.detect_outbound_iface()

    with _flock():
        _check_overlap(transit_net, aggregate)

        ns_created = not _ns_exists()
        if ns_created:
            log.info("ensure_mgmt_ns: creating namespace %s", MGMT_NS)
            _run(["ip", "netns", "add", MGMT_NS])

        # veth pair host<->mgmt-ns.
        if not _link_exists(VETH_HOST):
            # Stale ns-side peer (ns recreated) — recreate the whole pair.
            _exec(["ip", "link", "del", VETH_NS], check=False)
            _run(["ip", "link", "add", VETH_HOST, "type", "veth",
                  "peer", "name", VETH_NS])
            _run(["ip", "link", "set", VETH_NS, "netns", MGMT_NS])

        # Host side: address, up, route to the pool aggregate.
        if not _has_addr(VETH_HOST, f"{host_ip}/{prefix}"):
            _run(["ip", "addr", "add", f"{host_ip}/{prefix}", "dev", VETH_HOST],
                 check=False)
        _run(["ip", "link", "set", VETH_HOST, "up"])
        _run(["ip", "route", "replace", str(aggregate), "via", ns_ip], check=False)

        # Host iptables: forward the transit veth + MASQUERADE the transit src.
        _ensure_host_iptables(transit_net, uplink)
        _run(["sysctl", "-w", "net.ipv4.ip_forward=1"], check=False)

        # Mgmt-ns side: address, up, lo, default route, forwarding.
        if not _has_addr(VETH_NS, f"{ns_ip}/{prefix}", MGMT_NS):
            _exec(["ip", "addr", "add", f"{ns_ip}/{prefix}", "dev", VETH_NS],
                  check=False)
        _exec(["ip", "link", "set", VETH_NS, "up"])
        _exec(["ip", "link", "set", "lo", "up"])
        if not _has_route("default", MGMT_NS):
            _exec(["ip", "route", "add", "default", "via", host_ip], check=False)
        _exec(["sysctl", "-w", "net.ipv4.ip_forward=1"], check=False)

        if ns_created:
            _reconnect_running_ranges(range_dir)


def _ensure_host_iptables(transit: ipaddress.IPv4Network,
                          uplink: str | None) -> None:
    """The host's static FORWARD + MASQUERADE ops (idempotent)."""
    fwd = [
        ["-i", VETH_HOST, "-j", "ACCEPT"],
        ["-o", VETH_HOST, "-m", "state", "--state",
         "RELATED,ESTABLISHED", "-j", "ACCEPT"],
    ]
    for rule in fwd:
        if _run(["iptables", "-C", "FORWARD", *rule], check=False).returncode != 0:
            _run(["iptables", "-A", "FORWARD", *rule], check=False)
    if uplink:
        masq = ["-t", "nat", "POSTROUTING", "-s", str(transit),
                "-o", uplink, "-j", "MASQUERADE"]
        if _run(["iptables", *masq[:1], "-C", *masq[1:]], check=False).returncode != 0:
            _run(["iptables", "-t", "nat", "-A", "POSTROUTING",
                  "-s", str(transit), "-o", uplink, "-j", "MASQUERADE"],
                 check=False)


def _reconnect_running_ranges(range_dir: str) -> None:
    """Re-wire every still-running range into a freshly (re)created mgmt-ns.

    Source of truth is ``<range_dir>/<name>/range.json`` (host-global, the same
    state ``supervisor.destroy_range`` reads), not the per-user StateDB.
    """
    root = Path(range_dir)
    if not root.is_dir():
        return
    for state_file in root.glob("*/range.json"):
        try:
            state = json.loads(state_file.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        netns_name = state.get("netns_name")
        subnet = state.get("subnet")
        if not netns_name or not subnet:
            continue
        # Only reconnect ranges whose own namespace still exists.
        if not _live_netns(netns_name):
            continue
        name = state_file.parent.name
        log.info("ensure_mgmt_ns: reconnecting running range %s (%s)",
                 name, subnet)
        try:
            connect_range(name, subnet)
        except Exception as exc:  # one bad range must not block the rest
            log.warning("reconnect of range %s failed: %s", name, exc)


# --- per-range connect / disconnect ---------------------------------------

def connect_range(range_name: str, mgmt_subnet: str) -> netns.MgmtNetwork:
    """Wire ``range_name`` into the mgmt-ns: veth pair, ``.254`` gateway, route,
    and per-range FORWARD/isolation rules — all inside ``rangectl-mgmt``."""
    netns_name = f"rangectl-{range_name}"
    return netns.create_mgmt_network(netns_name, mgmt_subnet, range_name)


def disconnect_range(range_name: str, mgmt_subnet: str, veth_host: str,
                     veth_ns: str = "", host_ip: str = "") -> None:
    """Tear down a range's mgmt-ns wiring: drop its internet rules (H5: always,
    idempotent) then remove its veth + per-range FORWARD rules."""
    internet.disable_internet(range_name, mgmt_subnet, veth_host, netns=MGMT_NS)
    netns.destroy_mgmt_network(netns.MgmtNetwork(
        bridge_name=netns.MGMT_BRIDGE,
        veth_host=veth_host,
        veth_ns=veth_ns,
        host_ip=host_ip,
        subnet=mgmt_subnet,
    ))


# --- reset / status -------------------------------------------------------

def destroy_mgmt_ns() -> None:
    """Delete the mgmt-ns (host route/veth go with it). Reset/tests ONLY — never
    called automatically (D5)."""
    log.info("destroy_mgmt_ns: removing %s", MGMT_NS)
    _run(["ip", "netns", "del", MGMT_NS], check=False)
    _run(["ip", "link", "del", VETH_HOST], check=False)
    aggregate = pool_aggregate()
    _run(["ip", "route", "del", aggregate], check=False)


def status(transit: str | None = None, pool: str | None = None) -> dict:
    """Read-only snapshot of the invariant (for ``rangectl mgmt-ns status`` and
    tests). Every value is a bool except the names/subnets."""
    transit_net = resolve_transit(transit)
    aggregate = pool_aggregate(pool)
    host_ip, ns_ip, prefix = _transit_ips(transit_net)
    ns_up = _ns_exists()
    return {
        "namespace": MGMT_NS,
        "transit": str(transit_net),
        "aggregate": aggregate,
        "ns_exists": ns_up,
        "veth_host_up": _link_up(VETH_HOST),
        "veth_ns_up": _link_up(VETH_NS, MGMT_NS) if ns_up else False,
        "host_addr": _has_addr(VETH_HOST, f"{host_ip}/{prefix}"),
        "host_route": _has_route(aggregate),
        "host_forward": _iptables_present(["FORWARD", "-i", VETH_HOST,
                                           "-j", "ACCEPT"]),
        "host_ip_forward": _ip_forward(),
        "ns_addr": _has_addr(VETH_NS, f"{ns_ip}/{prefix}", MGMT_NS) if ns_up else False,
        "ns_default_route": _has_route("default", MGMT_NS) if ns_up else False,
        "ns_ip_forward": _ip_forward(MGMT_NS) if ns_up else False,
    }
