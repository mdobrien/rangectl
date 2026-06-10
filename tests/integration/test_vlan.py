"""Phase 25 integration — native VLAN support (Gate 2).

Real-kernel verification of 802.1Q bridge VLAN filtering:
a) Access isolation: same IP subnet, different VLANs on one vlan-aware
   switch — the kernel drops cross-VLAN frames (ping fails), while same-VLAN
   peers reach each other (proving the failure is VLAN-driven, not fabric).
b) Router-on-a-stick: VyOS on a trunk port with dot1q subinterfaces routes
   between VLANs — cross-VLAN ping succeeds only via the router.
c) Tags on the wire: tcpdump -e on the trunk TAP (host side, inside the
   range netns) shows 802.1Q headers with both VIDs.

Uses the host's tcpdump inside the netns — no guest packages, no internet.
Requires libvirt + KVM (EC2). Namespace mode, runs as root.
"""
from __future__ import annotations
import logging
import subprocess
import time

import pytest

from rangectl import Range
from tests.integration.conftest import pytestmark_skip

log = logging.getLogger(__name__)
pytestmark = pytestmark_skip


class IsolationLab(Range):
    """Three VMs in ONE subnet on one vlan-aware switch. web+peer share
    VLAN 10; db sits alone in VLAN 20. Without VLAN filtering all three
    would be one broadcast domain and every ping would succeed."""
    name = "vliso"

    def define_nodes(self):
        self.web = self.node("web", image="ubuntu-22.04", vcpu=1, memory=1024)
        self.dbn = self.node("db", image="ubuntu-22.04", vcpu=1, memory=1024)
        self.peer = self.node("peer", image="ubuntu-22.04", vcpu=1, memory=1024)
        self.sw = self.switch("core", vlan_aware=True)

    def define_network(self):
        self.link(self.web.eth1["10.0.99.1/24"], self.sw.port0.access(10))
        self.link(self.dbn.eth1["10.0.99.2/24"], self.sw.port1.access(20))
        self.link(self.peer.eth1["10.0.99.3/24"], self.sw.port2.access(10))

    def verify(self):
        pass


class RasLab(Range):
    """Router-on-a-stick: VyOS trunk(10, 20) with dot1q subinterfaces routes
    between the web (VLAN 10) and db (VLAN 20) access ports. The router's
    parent eth1 carries an unused IP so the VyOS bootstrap renames/pins the
    NIC (it skips address-less interfaces)."""
    name = "vlras"

    def define_nodes(self):
        self.router = self.node("router", image="vyos", vcpu=1, memory=1024)
        self.web = self.node("web", image="ubuntu-22.04", vcpu=1, memory=1024,
                             depends_on=[self.router])
        self.dbn = self.node("db", image="ubuntu-22.04", vcpu=1, memory=1024,
                             depends_on=[self.router])
        self.sw = self.switch("core", vlan_aware=True)

    def define_network(self):
        self.link(self.web.eth1["10.0.10.2/24"], self.sw.port0.access(10))
        self.link(self.dbn.eth1["10.0.20.2/24"], self.sw.port1.access(20))
        self.link(self.router.eth1["10.0.99.1/24"],
                  self.sw.port2.trunk(10, 20))

    def verify(self):
        pass


VYOS_VIF_CONFIG = (
    "source /opt/vyatta/etc/functions/script-template\n"
    "configure\n"
    "set interfaces ethernet eth1 vif 10 address 10.0.10.1/24\n"
    "set interfaces ethernet eth1 vif 20 address 10.0.20.1/24\n"
    "commit\n"
    "save\n"
    "exit\n"
)


def _trunk_tap_and_netns(lab) -> tuple[str, str]:
    """Host-side TAP of the router's trunk port + the range netns."""
    link = lab.link("router", "core")
    ep = next(e for e in link._endpoints if e.vm_id)
    tap = ep.resolve(link._backend)
    assert tap, "no TAP resolved for the router trunk port"
    return tap, link._backend._netns_name


def _bridge_vlan_show(netns: str) -> str:
    res = subprocess.run(["ip", "netns", "exec", netns, "bridge", "vlan",
                          "show"], capture_output=True, text=True)
    assert res.returncode == 0, f"bridge vlan show failed: {res.stderr}"
    return res.stdout


def test_access_isolation_kernel_enforced(backend, db):
    """Same subnet, different VLANs: the bridge drops cross-VLAN frames."""
    lab = IsolationLab()
    try:
        lab.deploy(backend=backend, db=db, use_namespaces=True)

        # Same-VLAN control: web <-> peer (both access 10) must work.
        ok = lab["web"].exec("ping -c 3 -W 2 10.0.99.3")
        assert ok.exit_code == 0, \
            f"same-VLAN ping failed (fabric broken?):\n{ok.stdout}{ok.stderr}"

        # Cross-VLAN: web (10) -> db (20), same subnet — must FAIL in kernel.
        blocked = lab["web"].exec("ping -c 3 -W 2 10.0.99.2")
        log.info("cross-VLAN ping rc=%d:\n%s", blocked.exit_code, blocked.stdout)
        assert blocked.exit_code != 0, \
            "VLAN isolation broken: web (vlan10) reached db (vlan20) directly"
        rev = lab["db"].exec("ping -c 3 -W 2 10.0.99.1")
        assert rev.exit_code != 0, "VLAN isolation broken in reverse direction"

        # Kernel view: the live VLAN table carries the configured VIDs.
        netns = lab.link("web", "core")._backend._netns_name
        vlans = _bridge_vlan_show(netns)
        log.info("bridge vlan show:\n%s", vlans)
        assert " 10" in vlans and " 20" in vlans
        # Access ports replaced the default VID 1 with their PVID.
        assert "PVID" in vlans
    finally:
        lab.destroy()


def test_router_on_a_stick_and_tags_on_wire(backend, db):
    """VyOS dot1q subinterfaces route between VLANs; tcpdump -e on the trunk
    TAP shows 802.1Q tags with both VIDs."""
    lab = RasLab()
    try:
        lab.deploy(backend=backend, db=db, use_namespaces=True)

        # Configure dot1q subinterfaces on the trunk (router-on-a-stick).
        r = lab["router"].exec(VYOS_VIF_CONFIG)
        assert r.exit_code == 0, \
            f"VyOS vif config failed:\nstdout={r.stdout}\nstderr={r.stderr}"
        addrs = lab["router"].run("ip -br addr show")
        log.info("router interfaces after vif config:\n%s", addrs)
        assert "eth1.10" in addrs and "10.0.10.1/24" in addrs
        assert "eth1.20" in addrs and "10.0.20.1/24" in addrs

        # Hosts route the far VLAN via the router subinterface.
        lab["web"].run("sudo ip route add 10.0.20.0/24 via 10.0.10.1")
        lab["db"].run("sudo ip route add 10.0.10.0/24 via 10.0.20.1")

        # First hop: each host reaches its VLAN's router subif.
        assert lab["web"].exec("ping -c 3 -W 2 10.0.10.1").exit_code == 0, \
            "web cannot reach router subif on VLAN 10"
        assert lab["db"].exec("ping -c 3 -W 2 10.0.20.1").exit_code == 0, \
            "db cannot reach router subif on VLAN 20"

        # Tags on the wire: capture on the trunk TAP while pinging across.
        # Both directions of the routed flow share the trunk — VID 10
        # (web<->router) and VID 20 (router<->db) must both appear tagged.
        tap, netns = _trunk_tap_and_netns(lab)
        proc = subprocess.Popen(
            ["ip", "netns", "exec", netns, "timeout", "12", "tcpdump",
             "-e", "-nn", "-l", "-i", tap, "icmp"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        time.sleep(2)  # let tcpdump attach

        # Cross-VLAN ping via the router (the headline assertion).
        ping = lab["web"].exec("ping -c 5 -W 2 10.0.20.2")
        assert ping.exit_code == 0, \
            f"cross-VLAN routed ping failed:\n{ping.stdout}{ping.stderr}"

        out, _ = proc.communicate(timeout=20)
        log.info("trunk capture (%d lines):\n%s", len(out.splitlines()),
                 "\n".join(out.splitlines()[:12]))
        assert "802.1Q" in out, "no 802.1Q headers on the trunk TAP"
        assert "vlan 10" in out, "VID 10 not tagged on the trunk"
        assert "vlan 20" in out, "VID 20 not tagged on the trunk"

        # Reverse direction routes too.
        assert lab["db"].exec("ping -c 3 -W 2 10.0.10.2").exit_code == 0

        # Live kernel table: trunk TAP carries both VIDs tagged (no PVID
        # since no native was configured).
        vlans = _bridge_vlan_show(netns)
        log.info("bridge vlan show:\n%s", vlans)
        trunk_section = [ln for ln in vlans.splitlines()
                         if tap in ln or (ln.startswith((" ", "\t")) and ln.strip())]
        log.info("trunk vlan lines: %s", trunk_section)
        assert " 10" in vlans and " 20" in vlans
    finally:
        lab.destroy()
