"""Phase 11 — namespace-isolated range integration tests (Gate 2).

These exercise the full v2 path: ``Engine(use_namespaces=True)`` stands up a
per-range netns + libvirtd via the supervisor, deploys nodes through a
per-range LibvirtBackend bound to the range's socket + netns, and tears the
whole thing down with one ``supervisor.destroy_range``.

Run on EC2 (KVM + libvirt + root required):
    sudo pytest tests/integration/test_ns_integration.py -x -v

Each test cleans up its range(s) so no namespace/bridge/VM state leaks between
tests. A safety fixture sweeps stray rangectl netns + /ranges dirs on teardown.
"""
from __future__ import annotations
import logging
import shutil
import subprocess
import time

import pytest

from rangectl import Topology
from rangectl.engine import Engine
from rangectl.libvirt_backend import LibvirtBackend
from rangectl.state import StateDB
from tests.integration.conftest import pytestmark_skip

log = logging.getLogger(__name__)
pytestmark = pytestmark_skip


# --- helpers ---------------------------------------------------------------

def _virsh_list(socket_path: str) -> list[str]:
    """Running domain names visible through a range's per-range libvirtd."""
    r = subprocess.run(
        ["virsh", "-c", f"qemu+unix:///system?socket={socket_path}",
         "list", "--name", "--state-running"],
        capture_output=True, text=True,
    )
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def _netns_exists(name: str) -> bool:
    r = subprocess.run(["ip", "netns", "list"], capture_output=True, text=True)
    return any(line.split()[0] == name for line in r.stdout.splitlines() if line.strip())


def _sweep_ranges():
    """Best-effort removal of leftover rangectl netns, host veths, /ranges.

    A half-built range (deploy failed mid-way) leaves a named netns plus a
    host-side mgmt veth; deleting the netns orphans the veth, so sweep both.

    The persistent ``rangectl-mgmt`` namespace (Phase 16) is host
    infrastructure — NEVER swept here. It is excluded by exact name; the next
    deploy's ``ensure_mgmt_ns()`` would heal it regardless, but leaving it in
    place avoids needless churn between tests.
    """
    r = subprocess.run(["ip", "netns", "list"], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        ns = line.split()[0] if line.strip() else ""
        if ns.startswith("rangectl-") and ns != "rangectl-mgmt":
            subprocess.run(["ip", "netns", "del", ns], capture_output=True)
    links = subprocess.run(["ip", "-br", "link", "show"],
                           capture_output=True, text=True)
    for line in links.stdout.splitlines():
        dev = line.split("@")[0].split()[0] if line.strip() else ""
        if dev.startswith(("mgh", "mgp")):
            subprocess.run(["ip", "link", "delete", dev], capture_output=True)
    shutil.rmtree("/ranges", ignore_errors=True)


@pytest.fixture(autouse=True)
def cleanup_ranges():
    yield
    _sweep_ranges()


def _wait_ssh_ping(node, target_ip: str, attempts: int = 3):
    """Ping with a short settle/retry to absorb veth/bridge warm-up."""
    ping = node.exec(f"ping -c 3 -W 2 {target_ip}")
    for _ in range(attempts - 1):
        if ping.exit_code == 0:
            break
        time.sleep(3)
        ping = node.exec(f"ping -c 3 -W 2 {target_ip}")
    return ping


# --- Test 1: 2-node ubuntu range -------------------------------------------

def test_ns_two_node(backend: LibvirtBackend, db: StateDB):
    t = Topology("nstwo")
    a = t.node("a", image="ubuntu-22.04", vcpu=1, memory=1024)
    b = t.node("b", image="ubuntu-22.04", vcpu=1, memory=1024)
    t.link(a.eth1["10.0.1.1/24"], b.eth1["10.0.1.2/24"])

    engine = Engine(backend, db, use_namespaces=True)
    rng = engine.deploy(t)
    try:
        info = engine._range_info["nstwo"]
        assert _netns_exists(info.netns_name)

        # virsh via the per-range socket lists exactly the 2 range VMs.
        doms = _virsh_list(info.libvirt_socket)
        assert sorted(doms) == ["nstwo-a", "nstwo-b"], doms

        # SSH from host via the veth mgmt path.
        for name in ("a", "b"):
            r = rng[name].exec("hostname")
            assert r.exit_code == 0, f"{name} hostname failed: {r.stderr!r}"
            assert f"nstwo-{name}" in r.stdout

        # Inter-VM ping across the data link inside the netns.
        ping = _wait_ssh_ping(rng["a"], "10.0.1.2")
        assert ping.exit_code == 0, (
            f"a->b ping failed:\nstdout={ping.stdout}\nstderr={ping.stderr}"
        )
    finally:
        engine.destroy(t)

    # Clean teardown: netns gone, no domains on a (now-dead) socket.
    assert not _netns_exists("rangectl-nstwo")


# --- Test 2: VyOS routed range ---------------------------------------------

def test_ns_vyos_routed(backend: LibvirtBackend, db: StateDB):
    t = Topology("nsvyos")
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
        info = engine._range_info["nsvyos"]
        # VyOS serial-console bootstrap ran through the per-range libvirtd:
        # the router exposes eth0/eth1/eth2 with the configured addresses.
        out = rng["router"].exec("ip -br addr show")
        assert out.exit_code == 0, f"router addr show: {out.stderr!r}"
        assert "10.0.1.1/24" in out.stdout
        assert "10.0.2.1/24" in out.stdout

        for name in ("ubuntu-a", "ubuntu-b"):
            r = rng[name].exec("hostname")
            assert r.exit_code == 0
            assert f"nsvyos-{name}" in r.stdout

        # Static routes so the ubuntu hosts reach the far subnet via the router.
        rng["ubuntu-a"].exec("sudo ip route add 10.0.2.0/24 via 10.0.1.1")
        rng["ubuntu-b"].exec("sudo ip route add 10.0.1.0/24 via 10.0.2.1")

        # Cross-subnet routing through the VyOS router.
        ping = _wait_ssh_ping(rng["ubuntu-a"], "10.0.2.2")
        assert ping.exit_code == 0, (
            f"cross-subnet a->b through router failed:\n"
            f"stdout={ping.stdout}\nstderr={ping.stderr}"
        )
    finally:
        engine.destroy(t)
    assert not _netns_exists("rangectl-nsvyos")


# --- Test 3: mixed VM + container range ------------------------------------

def test_ns_mixed_vm_container(backend: LibvirtBackend, db: StateDB):
    if not shutil.which("docker"):
        pytest.skip("docker not installed on this host")
    from rangectl.container_backend import ContainerBackend

    t = Topology("nsmix")
    server = t.node("server", container="nginx:latest", vcpu=1, memory=128)
    client = t.node("client", image="ubuntu-22.04", vcpu=1, memory=1024,
                    depends_on=[server])
    t.link(server.eth1["10.0.1.1/24"], client.eth1["10.0.1.2/24"])

    # The container template backend is only used in legacy mode; in ns mode
    # the engine builds a per-range ContainerBackend bound to the netns.
    engine = Engine(backend, db, container_backend=ContainerBackend(),
                    use_namespaces=True)
    rng = engine.deploy(t)
    try:
        # docker exec path works.
        r = rng["server"].exec("hostname")
        assert r.exit_code == 0, f"container hostname: {r.stderr!r}"
        assert "nsmix-server" in r.stdout

        # VM SSH path works.
        r = rng["client"].exec("hostname")
        assert r.exit_code == 0
        assert "nsmix-client" in r.stdout

        # VM -> container ping over the shared bridge inside the netns.
        ping = _wait_ssh_ping(rng["client"], "10.0.1.1")
        assert ping.exit_code == 0, (
            f"client->server ping failed:\n"
            f"stdout={ping.stdout}\nstderr={ping.stderr}"
        )

        # nginx reachable from the VM (proves a real container, not just veth).
        r = rng["client"].exec(
            "curl -sS -o /dev/null -w '%{http_code}' http://10.0.1.1/")
        assert r.exit_code == 0 and r.stdout.strip() == "200", (
            f"curl nginx: stdout={r.stdout!r} stderr={r.stderr!r}")
    finally:
        engine.destroy(t)
    assert not _netns_exists("rangectl-nsmix")


# --- Test 4: two simultaneous isolated ranges ------------------------------

def test_ns_multi_range_isolation(backend: LibvirtBackend, db: StateDB):
    """Two ranges with identical internal data addressing coexist with no
    cross-talk — structural proof of netns isolation — and destroy
    independently."""
    def _pair(name: str) -> Topology:
        t = Topology(name)
        a = t.node("a", image="ubuntu-22.04", vcpu=1, memory=1024)
        b = t.node("b", image="ubuntu-22.04", vcpu=1, memory=1024)
        # Identical data subnet in BOTH ranges — only netns isolation makes
        # this non-colliding.
        t.link(a.eth1["10.0.9.1/24"], b.eth1["10.0.9.2/24"])
        return t

    ta, tb = _pair("nsA"), _pair("nsB")
    engine = Engine(backend, db, use_namespaces=True)

    rng_a = engine.deploy(ta)
    try:
        rng_b = engine.deploy(tb)
        try:
            ia = engine._range_info["nsA"]
            ib = engine._range_info["nsB"]
            # Separate netns + sockets.
            assert ia.netns_name != ib.netns_name
            assert ia.libvirt_socket != ib.libvirt_socket
            assert _netns_exists(ia.netns_name)
            assert _netns_exists(ib.netns_name)
            # Each socket only lists its own range's domains.
            assert sorted(_virsh_list(ia.libvirt_socket)) == ["nsA-a", "nsA-b"]
            assert sorted(_virsh_list(ib.libvirt_socket)) == ["nsB-a", "nsB-b"]

            # Intra-range ping works in BOTH, despite identical addressing.
            pa = _wait_ssh_ping(rng_a["a"], "10.0.9.2")
            assert pa.exit_code == 0, f"nsA intra ping: {pa.stdout}{pa.stderr}"
            pb = _wait_ssh_ping(rng_b["a"], "10.0.9.2")
            assert pb.exit_code == 0, f"nsB intra ping: {pb.stdout}{pb.stderr}"
        finally:
            # Destroy A first; B must be unaffected.
            engine.destroy(ta)
            assert not _netns_exists("rangectl-nsA")
            assert _netns_exists("rangectl-nsB")
            pb2 = _wait_ssh_ping(rng_b["a"], "10.0.9.2")
            assert pb2.exit_code == 0, (
                f"nsB ping broke after destroying nsA: {pb2.stdout}{pb2.stderr}")
            engine.destroy(tb)
    finally:
        # If B deploy failed, ensure A is gone.
        if _netns_exists("rangectl-nsA"):
            engine.destroy(ta)
    assert not _netns_exists("rangectl-nsB")
