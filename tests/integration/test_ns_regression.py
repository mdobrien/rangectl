"""Phase 12 — full Topo 3-6 regression + ns-specific tests (Gate 2).

These run the remaining topologies on the namespace-isolated backend and add
the Phase 12 namespace features: freeze/thaw, per-range internet policy, and
cgroup resource limits. Topo 1/2/7 are covered by ``test_ns_integration.py``.

Run on EC2 (KVM + libvirt + root required):
    sudo pytest tests/integration/test_ns_regression.py -x -v

Every test destroys its range(s); a safety fixture sweeps stray state.
"""
from __future__ import annotations
import contextlib
import logging
import subprocess
import time
from pathlib import Path

import pytest

from rangectl import Topology
from rangectl.cgroup import Resources
from rangectl.engine import Engine
from rangectl.libvirt_backend import LibvirtBackend
from rangectl.state import StateDB
from tests.integration.conftest import MGMT_SUBNET_CIDR, _primary_iface, pytestmark_skip
from tests.integration.test_ns_integration import (
    _netns_exists,
    _sweep_ranges,
    _virsh_list,
    _wait_ssh_ping,
)

log = logging.getLogger(__name__)
pytestmark = pytestmark_skip


@pytest.fixture(autouse=True)
def cleanup_ranges():
    yield
    _sweep_ranges()


@contextlib.contextmanager
def _without_blanket_nat():
    """Temporarily drop conftest's session-wide MASQUERADE for the default mgmt
    subnet.

    Every fresh-DB test allocates the first subnet (192.168.100.0/24), which
    the ``vm_internet_nat`` session fixture NATs unconditionally for legacy
    tests. That blanket rule would mask the per-range internet policy, so the
    internet tests remove it for their duration and restore it afterward —
    leaving ONLY the ``RANGE-<name>`` chain (when internet=full) as the NAT
    path, which is exactly what we want to exercise.
    """
    iface = _primary_iface()
    check = ["iptables", "-t", "nat", "-C", "POSTROUTING",
             "-s", MGMT_SUBNET_CIDR, "-o", iface, "-j", "MASQUERADE"]
    had = subprocess.run(check, capture_output=True).returncode == 0
    if had:
        subprocess.run(["iptables", "-t", "nat", "-D", "POSTROUTING",
                        "-s", MGMT_SUBNET_CIDR, "-o", iface, "-j", "MASQUERADE"],
                       capture_output=True)
    try:
        yield
    finally:
        if had:
            subprocess.run(["iptables", "-t", "nat", "-A", "POSTROUTING",
                            "-s", MGMT_SUBNET_CIDR, "-o", iface, "-j", "MASQUERADE"],
                           capture_output=True)


def _host_ping(target_ip: str, count: int = 2, wait: int = 2) -> bool:
    """Ping a VM's mgmt IP from the host (root netns) — no SSH involved."""
    r = subprocess.run(
        ["ping", "-c", str(count), "-W", str(wait), target_ip],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def _add_cross_subnet_routes(rng, a_name: str, b_name: str):
    """Both Ubuntu hosts need explicit routes for the far subnet via the router."""
    rng[a_name].exec("sudo ip route add 10.0.2.0/24 via 10.0.1.1")
    rng[b_name].exec("sudo ip route add 10.0.1.0/24 via 10.0.2.1")


# --- Topo 3: services + internet (apt-get nginx, cross-subnet curl) --------

def test_ns_topo3_services_internet(backend: LibvirtBackend, db: StateDB):
    """attacker -- vyos-router -- web(nginx). nginx is apt-installed during
    deploy (requires internet=full), then reached cross-subnet via curl."""
    t = Topology("nstopo3")
    router = t.node("router", image="vyos", vcpu=1, memory=1024)
    attacker = t.node("attacker", image="ubuntu-22.04", vcpu=1, memory=1024,
                      depends_on=[router])
    web = t.node("web", image="ubuntu-22.04", vcpu=1, memory=1024,
                 depends_on=[router])
    web.packages(["nginx"])
    web.service("nginx", enabled=True)
    t.link(attacker.eth1["10.0.1.2/24"], router.eth1["10.0.1.1/24"])
    t.link(router.eth2["10.0.2.1/24"], web.eth1["10.0.2.2/24"])

    engine = Engine(backend, db, use_namespaces=True, internet="full")
    rng = engine.deploy(t)
    try:
        # apt-get install nginx during deploy proves outbound internet worked.
        web_local = rng["web"].exec(
            "for i in $(seq 1 20); do "
            "curl -fsS -o /dev/null http://127.0.0.1/ && exit 0; "
            "sleep 1; done; exit 1"
        )
        assert web_local.exit_code == 0, (
            f"nginx never came up on web:\n{web_local.stdout}\n{web_local.stderr}")

        _add_cross_subnet_routes(rng, "attacker", "web")
        ping = _wait_ssh_ping(rng["attacker"], "10.0.2.2")
        assert ping.exit_code == 0, f"attacker->web ping: {ping.stdout}{ping.stderr}"

        curl = rng["attacker"].exec(
            "curl -fsS -o /dev/null -w '%{http_code}' --max-time 10 http://10.0.2.2/")
        assert curl.exit_code == 0 and curl.stdout.strip() == "200", (
            f"cross-subnet curl: stdout={curl.stdout!r} stderr={curl.stderr!r}")
    finally:
        engine.destroy(t)
    assert not _netns_exists("rangectl-nstopo3")


# --- Topo 4: diamond DAG + snapshot/restore --------------------------------

def test_ns_topo4_diamond_snapshot(backend: LibvirtBackend, db: StateDB):
    """4-node diamond deployed in waves (router → web+db → monitor), then a
    snapshot/restore cycle through the per-range libvirtd."""
    t = Topology("nstopo4")
    router = t.node("router", image="ubuntu-22.04", vcpu=1, memory=1024)
    web = t.node("web", image="ubuntu-22.04", vcpu=1, memory=1024,
                 depends_on=[router])
    db_node = t.node("db", image="ubuntu-22.04", vcpu=1, memory=1024,
                     depends_on=[router])
    monitor = t.node("monitor", image="ubuntu-22.04", vcpu=1, memory=1024,
                     depends_on=[web, db_node])
    t.link(router.eth1["10.0.1.1/24"], web.eth1["10.0.1.2/24"])
    t.link(router.eth2["10.0.2.1/24"], db_node.eth1["10.0.2.2/24"])
    t.link(web.eth2["10.0.3.1/24"], monitor.eth1["10.0.3.2/24"])
    t.link(db_node.eth2["10.0.4.1/24"], monitor.eth2["10.0.4.2/24"])

    engine = Engine(backend, db, use_namespaces=True)
    rng = engine.deploy(t)
    try:
        for name in ("router", "web", "db", "monitor"):
            r = rng[name].exec("hostname")
            assert r.exit_code == 0, f"{name} ssh: {r.stderr}"
            assert f"nstopo4-{name}" in r.stdout

        # Direct-link connectivity (endpoints share L2, no routing needed).
        assert _wait_ssh_ping(rng["web"], "10.0.1.1").exit_code == 0
        assert _wait_ssh_ping(rng["monitor"], "10.0.3.1").exit_code == 0

        # Snapshot/restore marker cycle.
        assert rng["monitor"].exec("echo before | sudo tee /tmp/marker").exit_code == 0
        rng.snapshot("baseline")
        assert rng["monitor"].exec("echo after | sudo tee /tmp/marker").exit_code == 0
        rng.restore("baseline")

        # After restore, the marker must read 'before'. Allow SSH to settle.
        deadline = time.time() + 60
        marker = None
        while time.time() < deadline:
            r = rng["monitor"].exec("cat /tmp/marker")
            if r.exit_code == 0:
                marker = r.stdout
                if "before" in marker:
                    break
            time.sleep(2)
        assert marker and "before" in marker, (
            f"post-restore marker not reverted: {marker!r}")
    finally:
        engine.destroy(t)
    assert not _netns_exists("rangectl-nstopo4")


# --- Topo 5: link toggle ----------------------------------------------------

def test_ns_topo5_link_toggle(backend: LibvirtBackend, db: StateDB):
    """Link.down() breaks the cross-subnet path; Link.up() restores it, with
    the bridge living inside the range's netns."""
    t = Topology("nstopo5")
    router = t.node("router", image="vyos", vcpu=1, memory=1024)
    a = t.node("ubuntu-a", image="ubuntu-22.04", vcpu=1, memory=1024,
               depends_on=[router])
    b = t.node("ubuntu-b", image="ubuntu-22.04", vcpu=1, memory=1024,
               depends_on=[router])
    t.link(a.eth1["10.0.1.2/24"], router.eth1["10.0.1.1/24"])
    t.link(router.eth2["10.0.2.1/24"], b.eth1["10.0.2.2/24"])

    engine = Engine(backend, db, use_namespaces=True)
    rng = engine.deploy(t)
    try:
        _add_cross_subnet_routes(rng, "ubuntu-a", "ubuntu-b")
        base = _wait_ssh_ping(rng["ubuntu-a"], "10.0.2.2")
        assert base.exit_code == 0, f"baseline a->b: {base.stdout}{base.stderr}"

        link = rng.link("router", "ubuntu-b")
        link.down()
        assert not link._is_up
        down = rng["ubuntu-a"].exec("ping -c 1 -W 3 10.0.2.2")
        assert down.exit_code != 0, (
            f"ping should fail after link.down():\n{down.stdout}\n{down.stderr}")

        link.up()
        assert link._is_up
        up = _wait_ssh_ping(rng["ubuntu-a"], "10.0.2.2", attempts=10)
        assert up.exit_code == 0, (
            f"ping should recover after link.up():\n{up.stdout}\n{up.stderr}")
    finally:
        engine.destroy(t)
    assert not _netns_exists("rangectl-nstopo5")


# --- Topo 6: multi-topology isolation + staggered destroy ------------------

def test_ns_topo6_multi_topology_isolation(backend: LibvirtBackend, db: StateDB):
    """Two ranges on identical internal addressing run simultaneously with no
    cross-talk; destroying one leaves the other fully functional."""
    def _pair(name: str) -> Topology:
        t = Topology(name)
        a = t.node("a", image="ubuntu-22.04", vcpu=1, memory=1024)
        b = t.node("b", image="ubuntu-22.04", vcpu=1, memory=1024)
        t.link(a.eth1["10.0.1.1/24"], b.eth1["10.0.1.2/24"])
        return t

    red, blue = _pair("nsred"), _pair("nsblue")
    engine = Engine(backend, db, use_namespaces=True)

    rng_red = engine.deploy(red)
    try:
        rng_blue = engine.deploy(blue)
        try:
            ir = engine._range_info["nsred"]
            ib = engine._range_info["nsblue"]
            assert ir.netns_name != ib.netns_name
            assert ir.mgmt_subnet != ib.mgmt_subnet
            assert sorted(_virsh_list(ir.libvirt_socket)) == ["nsred-a", "nsred-b"]
            assert sorted(_virsh_list(ib.libvirt_socket)) == ["nsblue-a", "nsblue-b"]

            # Identical data subnet, both work — only netns isolation allows it.
            assert _wait_ssh_ping(rng_red["a"], "10.0.1.2").exit_code == 0
            assert _wait_ssh_ping(rng_blue["a"], "10.0.1.2").exit_code == 0

            # Cross-range isolation: red's VM cannot reach blue's mgmt IP.
            blue_mgmt = rng_blue["a"].mgmt_ip
            breach = rng_red["a"].exec(f"ping -c 1 -W 2 {blue_mgmt}")
            assert breach.exit_code != 0, (
                f"ISOLATION BREACH: red reached blue mgmt {blue_mgmt}")
        finally:
            # Staggered destroy: red first, blue must survive.
            engine.destroy(red)
            assert not _netns_exists("rangectl-nsred")
            assert _netns_exists("rangectl-nsblue")
            survive = _wait_ssh_ping(rng_blue["a"], "10.0.1.2")
            assert survive.exit_code == 0, (
                f"blue broke after red destroy: {survive.stdout}{survive.stderr}")
            engine.destroy(blue)
    finally:
        if _netns_exists("rangectl-nsred"):
            engine.destroy(red)
    assert not _netns_exists("rangectl-nsblue")


# --- Freeze / thaw ----------------------------------------------------------

def test_ns_freeze_thaw(backend: LibvirtBackend, db: StateDB):
    """A frozen range stops responding (cgroup freezer suspends every QEMU);
    thawing resumes it."""
    t = Topology("nsfreeze")
    a = t.node("a", image="ubuntu-22.04", vcpu=1, memory=1024)
    b = t.node("b", image="ubuntu-22.04", vcpu=1, memory=1024)
    t.link(a.eth1["10.0.1.1/24"], b.eth1["10.0.1.2/24"])

    # Resources → a cgroup is created, which the freezer acts on.
    engine = Engine(backend, db, use_namespaces=True,
                    resources=Resources(memory="4G"))
    rng = engine.deploy(t)
    try:
        a_ip = rng["a"].mgmt_ip
        assert _host_ping(a_ip), "VM a should be pingable before freeze"

        rng.freeze()
        # Give the freezer a moment; a frozen VM cannot answer ICMP.
        time.sleep(2)
        assert not _host_ping(a_ip, count=2, wait=2), (
            "VM a still responded to ping while frozen")

        freeze_state = Path("/sys/fs/cgroup/rangectl-nsfreeze/cgroup.freeze")
        assert freeze_state.read_text().strip() == "1"

        rng.thaw()
        # After thaw the VM resumes; allow a few seconds to start answering.
        resumed = False
        for _ in range(15):
            if _host_ping(a_ip):
                resumed = True
                break
            time.sleep(2)
        assert resumed, "VM a never resumed responding after thaw"
        assert freeze_state.read_text().strip() == "0"
    finally:
        engine.destroy(t)
    assert not _netns_exists("rangectl-nsfreeze")


# --- Internet policy: none blocks, full allows, runtime toggle -------------

def test_ns_internet_none_blocks_outbound(backend: LibvirtBackend, db: StateDB):
    t = Topology("nsinetnone")
    a = t.node("a", image="ubuntu-22.04", vcpu=1, memory=1024)
    b = t.node("b", image="ubuntu-22.04", vcpu=1, memory=1024)
    t.link(a.eth1["10.0.1.1/24"], b.eth1["10.0.1.2/24"])

    with _without_blanket_nat():
        engine = Engine(backend, db, use_namespaces=True)  # internet defaults none
        rng = engine.deploy(t)
        try:
            out = rng["a"].exec("ping -c 2 -W 3 8.8.8.8")
            assert out.exit_code != 0, (
                f"internet=none should block outbound, but ping succeeded:\n{out.stdout}")
        finally:
            engine.destroy(t)


def test_ns_internet_full_allows_outbound(backend: LibvirtBackend, db: StateDB):
    t = Topology("nsinetfull")
    a = t.node("a", image="ubuntu-22.04", vcpu=1, memory=1024)
    b = t.node("b", image="ubuntu-22.04", vcpu=1, memory=1024)
    t.link(a.eth1["10.0.1.1/24"], b.eth1["10.0.1.2/24"])

    # Drop the blanket NAT so ONLY the per-range RANGE-<name> chain provides
    # outbound — this proves the feature, not the legacy fixture.
    with _without_blanket_nat():
        engine = Engine(backend, db, use_namespaces=True, internet="full")
        rng = engine.deploy(t)
        try:
            ping = rng["a"].exec("ping -c 2 -W 3 8.8.8.8")
            assert ping.exit_code == 0, (
                f"internet=full should allow outbound ping:\n{ping.stdout}{ping.stderr}")
            # apt-get update exercises DNS + outbound HTTP through the MASQUERADE.
            apt = rng["a"].exec(
                "sudo DEBIAN_FRONTEND=noninteractive apt-get update")
            assert apt.exit_code == 0, (
                f"apt-get update failed with internet=full:\n{apt.stdout}\n{apt.stderr}")
        finally:
            engine.destroy(t)


def test_ns_internet_runtime_toggle(backend: LibvirtBackend, db: StateDB):
    """Deploy isolated, then enable_internet() opens outbound and
    disable_internet() closes it again — runtime control via the Range."""
    t = Topology("nsinettoggle")
    a = t.node("a", image="ubuntu-22.04", vcpu=1, memory=1024)
    b = t.node("b", image="ubuntu-22.04", vcpu=1, memory=1024)
    t.link(a.eth1["10.0.1.1/24"], b.eth1["10.0.1.2/24"])

    with _without_blanket_nat():
        engine = Engine(backend, db, use_namespaces=True)
        rng = engine.deploy(t)
        try:
            assert rng["a"].exec("ping -c 2 -W 3 8.8.8.8").exit_code != 0, (
                "outbound should be blocked before enable_internet()")

            rng.enable_internet()
            assert rng.internet == "full"
            opened = None
            for _ in range(5):
                opened = rng["a"].exec("ping -c 2 -W 3 8.8.8.8")
                if opened.exit_code == 0:
                    break
                time.sleep(2)
            assert opened.exit_code == 0, (
                f"enable_internet() did not open outbound:\n{opened.stdout}{opened.stderr}")

            rng.disable_internet()
            assert rng.internet == "none"
            closed = rng["a"].exec("ping -c 2 -W 3 8.8.8.8")
            assert closed.exit_code != 0, (
                f"disable_internet() did not close outbound:\n{closed.stdout}")
        finally:
            engine.destroy(t)


# --- Resource limits --------------------------------------------------------

def test_ns_resource_limits(backend: LibvirtBackend, db: StateDB):
    """Deploy with Resources(memory=…) → the cgroup carries the right limit."""
    t = Topology("nsres")
    a = t.node("a", image="ubuntu-22.04", vcpu=1, memory=1024)
    t.node("b", image="ubuntu-22.04", vcpu=1, memory=1024)
    # No link needed; single-subnet mgmt is enough for this check.

    engine = Engine(backend, db, use_namespaces=True,
                    resources=Resources(memory="8G", cpus=4))
    rng = engine.deploy(t)
    try:
        mem_max = Path("/sys/fs/cgroup/rangectl-nsres/memory.max")
        assert mem_max.exists(), "cgroup memory.max missing"
        assert mem_max.read_text().strip() == str(8 * 1024 ** 3), (
            f"memory.max wrong: {mem_max.read_text()!r}")
        cpu_max = Path("/sys/fs/cgroup/rangectl-nsres/cpu.max")
        assert cpu_max.read_text().split()[0] == str(4 * 100000), (
            f"cpu.max wrong: {cpu_max.read_text()!r}")
        # The VM is reachable, proving libvirtd runs inside the cgroup fine.
        assert rng["a"].exec("hostname").exit_code == 0
    finally:
        engine.destroy(t)
    assert not _netns_exists("rangectl-nsres")
