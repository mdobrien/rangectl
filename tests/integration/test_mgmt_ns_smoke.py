"""Phase 16b — persistent management-namespace smoke (Gate 2, focused).

Proves the structural cutover end-to-end on real VMs:

  a) a 2-node range is reachable host -> rangectl-mgmt -> range-ns -> VM;
  b) the deploy touches the HOST with ONLY the 4 static ops — no per-range
     veth / NAT chain / subnet rule leaks onto the host;
  c) runtime internet-enable reaches the internet (apt-get update) and teardown
     leaves NO stale NAT jump inside the mgmt-ns (the H5 fix);
  d) deleting rangectl-mgmt while a range runs is healed by ensure_mgmt_ns(),
     and the VM is reachable again (kill/heal recovery == ordinary heal).

Run on EC2 (KVM + libvirt + root):
    sudo pytest tests/integration/test_mgmt_ns_smoke.py -x -v

NOTE: the per-range NAT/forward/gateway lives inside ``rangectl-mgmt``, and the
host carries a single transit MASQUERADE installed by ensure_mgmt_ns. The legacy
conftest blanket MASQUERADE was retired in 16c (D4) — the new architecture is
the only NAT path.
"""
from __future__ import annotations
import logging
import subprocess
import time

import pytest

from rangectl import Topology, mgmt_namespace
from rangectl.engine import Engine
from rangectl.libvirt_backend import LibvirtBackend
from rangectl.mgmt_namespace import MGMT_NS, VETH_HOST
from rangectl.state import StateDB
from tests.integration.conftest import pytestmark_skip
from tests.integration.test_ns_integration import _netns_exists, _sweep_ranges
from tests.integration.test_ns_regression import _host_ping

log = logging.getLogger(__name__)
pytestmark = pytestmark_skip


@pytest.fixture(autouse=True)
def cleanup_ranges():
    """Sweep stray ranges after each test. rangectl-mgmt is persistent by design
    (excluded from the sweep) — it is left in place between tests."""
    yield
    _sweep_ranges()


def _host_rules() -> set[str]:
    """A snapshot of host iptables (filter + nat) + the IPv4 routing table."""
    out: set[str] = set()
    for args in (["iptables", "-S"], ["iptables", "-t", "nat", "-S"]):
        r = subprocess.run(args, capture_output=True, text=True)
        out.update(ln.strip() for ln in r.stdout.splitlines() if ln.strip())
    routes = subprocess.run(["ip", "route", "show"], capture_output=True, text=True)
    out.update(f"route: {ln.strip()}" for ln in routes.stdout.splitlines()
               if ln.strip())
    return out


def _mgmt_ns_nat() -> str:
    """The nat table inside the mgmt-ns (for stale-jump / chain inspection)."""
    r = subprocess.run(
        ["ip", "netns", "exec", MGMT_NS, "iptables", "-t", "nat", "-S"],
        capture_output=True, text=True,
    )
    return r.stdout


def _exec_ok(node, cmd: str, attempts: int = 3, settle: int = 5):
    r = node.exec(cmd)
    for _ in range(attempts - 1):
        if r.exit_code == 0:
            break
        time.sleep(settle)
        r = node.exec(cmd)
    return r


def _two_node(name: str) -> Topology:
    t = Topology(name)
    a = t.node("a", image="ubuntu-22.04", vcpu=1, memory=1024)
    b = t.node("b", image="ubuntu-22.04", vcpu=1, memory=1024)
    t.link(a.eth1["10.0.1.1/24"], b.eth1["10.0.1.2/24"])
    return t


# --- (a) + (b): reachability through the hop + clean host -------------------

def test_smoke_exec_through_hop_and_host_stays_clean(
        backend: LibvirtBackend, db: StateDB):
    # Clean slate so the host diff captures exactly what a deploy adds.
    mgmt_namespace.destroy_mgmt_ns()
    _sweep_ranges()
    base = _host_rules()

    t = _two_node("nssmoke")
    engine = Engine(backend, db, use_namespaces=True)
    rng = engine.deploy(t)
    try:
        # (a) exec works host -> rangectl-mgmt -> range-ns -> VM.
        assert _netns_exists(MGMT_NS)
        for n in ("a", "b"):
            r = _exec_ok(rng[n], "hostname")
            assert r.exit_code == 0, f"{n}: {r.stderr!r}"
            assert f"nssmoke-{n}" in r.stdout
        # Host can reach the VM mgmt IP through the mgmt-ns (no SSH).
        assert _host_ping(rng["a"].mgmt_ip), "host cannot reach VM via mgmt-ns"

        # (b) host diff == ONLY the 4 static ops; nothing per-range.
        added = _host_rules() - base
        log.info("host additions:\n%s", "\n".join(sorted(added)))
        # The static ops are present.
        assert any(VETH_HOST in r and "FORWARD" in r for r in added), added
        assert any("route: 10.255.0.0/16 via 10.254.0.2" in r
                   for r in added), added
        assert any("MASQUERADE" in r and "10.254.0.0/30" in r
                   for r in added), added
        # NOTHING per-range leaked onto the host.
        assert not any("mgh" in r for r in added), added
        assert not any("RANGE-nssmoke" in r for r in added), added
        assert not any("10.255.1.0/24" in r for r in added), added
    finally:
        engine.destroy(t)
    assert not _netns_exists("rangectl-nssmoke")


# --- (c): runtime internet enable + no stale NAT jump on teardown ----------

def test_smoke_internet_enable_then_no_stale_nat(
        backend: LibvirtBackend, db: StateDB):
    mgmt_namespace.ensure_mgmt_ns()  # host transit MASQUERADE in place

    t = _two_node("nssmokei")
    engine = Engine(backend, db, use_namespaces=True)  # internet defaults none
    rng = engine.deploy(t)
    subnet = engine._range_info["nssmokei"].mgmt_subnet
    try:
        rng.enable_internet()
        # NAT jump + RANGE chain are inside the mgmt-ns, not on the host.
        nat = _mgmt_ns_nat()
        assert "RANGE-nssmokei" in nat, nat
        assert subnet in nat, nat
        host = "\n".join(_host_rules())
        assert "RANGE-nssmokei" not in host, host

        # Real outbound reachability.
        r = _exec_ok(rng["a"],
                     "sudo apt-get update -o Acquire::Retries=3 2>&1 | tail -2",
                     attempts=3, settle=10)
        assert r.exit_code == 0, f"apt-get update failed: {r.stdout}\n{r.stderr}"
    finally:
        engine.destroy(t)

    # (H5) teardown ALWAYS disables internet — no stale jump for the next
    # range that recycles this /24.
    nat_after = _mgmt_ns_nat()
    assert "RANGE-nssmokei" not in nat_after, nat_after
    assert f"-s {subnet}" not in nat_after, nat_after


# --- (d): kill rangectl-mgmt mid-range, heal, reachable again --------------

def test_smoke_heal_after_mgmt_ns_kill(backend: LibvirtBackend, db: StateDB):
    mgmt_namespace.ensure_mgmt_ns()

    t = _two_node("nssmokeh")
    engine = Engine(backend, db, use_namespaces=True)
    rng = engine.deploy(t)
    try:
        assert _exec_ok(rng["a"], "hostname").exit_code == 0

        # Kill the persistent mgmt-ns out from under the running range.
        mgmt_namespace.destroy_mgmt_ns()
        assert not _netns_exists(MGMT_NS)

        # Heal: ensure recreates the ns AND reconnects the running range
        # (same code path as recovery).
        mgmt_namespace.ensure_mgmt_ns()
        assert _netns_exists(MGMT_NS)

        # VM reachable again through the rebuilt hop.
        r = _exec_ok(rng["a"], "hostname", attempts=4, settle=5)
        assert r.exit_code == 0, f"unreachable after heal: {r.stderr!r}"
        assert "nssmokeh-a" in r.stdout
        assert _host_ping(rng["a"].mgmt_ip), "host->VM broken after heal"
    finally:
        engine.destroy(t)
    assert not _netns_exists("rangectl-nssmokeh")
