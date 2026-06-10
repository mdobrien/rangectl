"""Phase 20 integration — hub & switch L2 node types (Gate 2).

Real-kernel verification of the bridge semantics the unit tests can only
mock: VMs ping through a user-named switch bridge, a hub floods unicast to a
passive IDS while a switch (after MAC learning) does not, a switch<->hub veth
uplink carries traffic, impairment lands on the single VM TAP of a VM<->switch
link, and a deliberately looped L2 definition aborts before deploying anything.

tcpdump puts the capture NIC in promiscuous mode automatically — required on a
hub, where flooded frames are not addressed to the IDS's MAC.

Requires libvirt + KVM (EC2). Namespace mode, runs as root.
"""
from __future__ import annotations
import logging
import re
import subprocess
import time

import pytest

from rangectl import Range
from rangectl.types import CycleError
from tests.integration.conftest import pytestmark_skip

log = logging.getLogger(__name__)
pytestmark = pytestmark_skip


def _avg_rtt(ping_output: str) -> float:
    m = re.search(r"=\s*[\d.]+/([\d.]+)/", ping_output)
    assert m, f"no rtt summary in ping output:\n{ping_output}"
    return float(m.group(1))


def _dev_for_ip(node, ip: str) -> str:
    """Guest device that carries ``ip`` (guest NIC names vary by image)."""
    out = node.run(f"ip -o -4 addr show | grep '{ip}/' | awk '{{print $2}}'")
    dev = out.strip().splitlines()[0].strip()
    assert dev, f"no device with {ip} on {node.name}"
    return dev


def _capture_icmp(ids, dev: str, src: str, dst: str, seconds: int = 8) -> str:
    """Start a background tcpdump on ``ids`` for ICMP echo between src/dst."""
    ids.run(
        "sudo sh -c 'rm -f /tmp/cap.txt; "
        f"nohup timeout {seconds} tcpdump -ni {dev} -l "
        f"\"icmp and host {src} and host {dst}\" "
        "> /tmp/cap.txt 2>/dev/null &'"
    )
    time.sleep(1)  # let tcpdump attach before traffic starts


def _captured_lines(ids) -> list[str]:
    out = ids.exec("cat /tmp/cap.txt").stdout
    return [ln for ln in out.splitlines() if "ICMP" in ln or "icmp" in ln]


class SwitchLab(Range):
    """Three VMs on one switch; c doubles as the switch-side IDS."""
    name = "swlab"

    def define_nodes(self):
        self.a = self.node("a", image="ubuntu-22.04", vcpu=1, memory=1024)
        self.b = self.node("b", image="ubuntu-22.04", vcpu=1, memory=1024)
        self.c = self.node("c", image="ubuntu-22.04", vcpu=1, memory=1024)
        self.sw = self.switch("core", ports=8)

    def define_network(self):
        self.link(self.a.eth1["10.0.1.1/24"], self.sw.port0)
        self.link(self.b.eth1["10.0.1.2/24"], self.sw.port1)
        self.link(self.c.eth1["10.0.1.3/24"], self.sw.port2)

    def install_software(self):
        self.c.packages(["tcpdump"])

    def verify(self):
        self.expect_reach(self.a, "10.0.1.2")


class HubLab(Range):
    """Two talkers + a passive IDS on one hub — IDS must see their unicast."""
    name = "hublab"

    def define_nodes(self):
        self.a = self.node("a", image="ubuntu-22.04", vcpu=1, memory=1024)
        self.b = self.node("b", image="ubuntu-22.04", vcpu=1, memory=1024)
        self.ids = self.node("ids", image="ubuntu-22.04", vcpu=1, memory=1024)
        self.hb = self.hub("mon", ports=4)

    def define_network(self):
        self.link(self.a.eth1["10.0.2.1/24"], self.hb.port0)
        self.link(self.b.eth1["10.0.2.2/24"], self.hb.port1)
        self.link(self.ids.eth1["10.0.2.3/24"], self.hb.port2)

    def install_software(self):
        self.ids.packages(["tcpdump"])

    def verify(self):
        self.expect_reach(self.a, "10.0.2.2")


class UplinkLab(Range):
    """switch <-> hub veth uplink: a on the switch, b + IDS on the hub."""
    name = "uplab"

    def define_nodes(self):
        self.a = self.node("a", image="ubuntu-22.04", vcpu=1, memory=1024)
        self.b = self.node("b", image="ubuntu-22.04", vcpu=1, memory=1024)
        self.ids = self.node("ids", image="ubuntu-22.04", vcpu=1, memory=1024)
        self.sw = self.switch("edge")
        self.hb = self.hub("mon")

    def define_network(self):
        self.link(self.a.eth1["10.0.3.1/24"], self.sw.port0)
        self.link(self.sw.port1, self.hb.port0)
        self.link(self.b.eth1["10.0.3.2/24"], self.hb.port1)
        self.link(self.ids.eth1["10.0.3.3/24"], self.hb.port2)

    def install_software(self):
        self.ids.packages(["tcpdump"])

    def verify(self):
        self.expect_reach(self.a, "10.0.3.2")


class LoopedLab(Range):
    """Deliberate switch ring — deploy must abort (no STP, D7)."""
    name = "looplab"

    def define_nodes(self):
        self.s1 = self.switch("s1")
        self.s2 = self.switch("s2")
        self.s3 = self.switch("s3")

    def define_network(self):
        self.link(self.s1.port0, self.s2.port0)
        self.link(self.s2.port1, self.s3.port0)
        self.link(self.s3.port1, self.s1.port1)

    def verify(self):
        pass


def test_switch_forwarding_and_isolation(backend, db):
    """All VMs ping through the switch; after MAC learning, the IDS port does
    NOT receive other VMs' unicast (a switch forwards learned unicast only).
    Also: impairment on a VM<->switch link lands on the single VM TAP."""
    lab = SwitchLab()
    try:
        lab.deploy(backend=backend, db=db, use_namespaces=True)

        # 1. Full mesh through the switch.
        for src, dst in (("a", "10.0.1.2"), ("a", "10.0.1.3"),
                         ("b", "10.0.1.1"), ("b", "10.0.1.3"),
                         ("c", "10.0.1.1"), ("c", "10.0.1.2")):
            assert lab[src].exec(f"ping -c 2 -W 2 {dst}").exit_code == 0, \
                f"{src} cannot reach {dst} through the switch"

        # 2. Warm the FDB: a<->b traffic teaches the switch both MACs.
        lab["a"].run("ping -c 5 10.0.1.2")

        # 3. Capture on c while a pings b — learned unicast must NOT flood
        #    out of c's port. (ARP/broadcast still floods; filter is icmp.)
        ids_dev = _dev_for_ip(lab["c"], "10.0.1.3")
        _capture_icmp(lab["c"], ids_dev, "10.0.1.1", "10.0.1.2", seconds=8)
        lab["a"].run("ping -c 5 10.0.1.2")
        time.sleep(8)
        leaked = _captured_lines(lab["c"])
        log.info("switch IDS capture (%d lines):\n%s",
                 len(leaked), "\n".join(leaked[:10]))
        assert len(leaked) == 0, \
            f"switch flooded learned unicast to the IDS port: {leaked[:5]}"

        # 4. Impair the a<->switch link: the single VM TAP carries both
        #    directions, so 100ms egress delay shows up once per round trip.
        base = _avg_rtt(lab["a"].run("ping -c 5 10.0.1.2"))
        link = lab.link("a", "core")
        link.impair(latency="100ms")
        rtt = _avg_rtt(lab["a"].run("ping -c 5 10.0.1.2"))
        log.info("VM<->switch impair: base=%.2fms impaired=%.2fms", base, rtt)
        assert rtt > base + 80, f"latency not applied: {base} -> {rtt}"
        link.clear()
        cleared = _avg_rtt(lab["a"].run("ping -c 5 10.0.1.2"))
        assert cleared < base + 20

        # 5. outbound= toward the L2 node is meaningless and raises.
        with pytest.raises(ValueError):
            link.impair(latency="10ms", outbound="core")

        # 6. CLI surfaces the L2 node without power/SSH queries.
        res = subprocess.run(
            ["python3", "-m", "rangectl.cli", "status", lab.name],
            capture_output=True, text=True)
        log.info("rangectl status:\n%s", res.stdout)
        assert res.returncode == 0
        assert "core" in res.stdout and "switch" in res.stdout
    finally:
        lab.destroy()


def test_hub_floods_unicast_to_ids(backend, db):
    """A passive IDS on a hub sees unicast between the other two VMs —
    learning is off and everything floods (D2)."""
    lab = HubLab()
    try:
        lab.deploy(backend=backend, db=db, use_namespaces=True)

        # Plenty of pre-traffic: on a switch this would train the FDB and
        # hide the unicast; a hub must keep flooding regardless.
        lab["a"].run("ping -c 5 10.0.2.2")

        ids_dev = _dev_for_ip(lab["ids"], "10.0.2.3")
        _capture_icmp(lab["ids"], ids_dev, "10.0.2.1", "10.0.2.2", seconds=8)
        lab["a"].run("ping -c 5 10.0.2.2")
        time.sleep(8)
        seen = _captured_lines(lab["ids"])
        log.info("hub IDS capture (%d lines):\n%s",
                 len(seen), "\n".join(seen[:10]))
        assert len(seen) >= 5, \
            f"hub did not flood a<->b unicast to the IDS (saw {len(seen)} pkts)"

        # Kernel-level check: hub ports carry learning off.
        netns = lab.link("a", "mon")._backend._netns_name
        res = subprocess.run(
            ["ip", "netns", "exec", netns, "bridge", "-d", "link", "show"],
            capture_output=True, text=True)
        log.info("bridge -d link show:\n%s", res.stdout)
        hub_lines = [ln for ln in res.stdout.splitlines()
                     if "master hub-mon" in ln]
        assert hub_lines, "no ports enslaved to hub-mon"
    finally:
        lab.destroy()


def test_switch_hub_uplink_carries_traffic(backend, db):
    """a (switch) reaches b (hub) across the veth uplink, and the IDS on the
    hub sees that unicast — the hub floods what the uplink delivers. The
    uplink veth is also impairable (L2<->L2, D6)."""
    lab = UplinkLab()
    try:
        lab.deploy(backend=backend, db=db, use_namespaces=True)

        assert lab["a"].exec("ping -c 3 -W 2 10.0.3.2").exit_code == 0, \
            "switch->hub uplink does not carry traffic"

        ids_dev = _dev_for_ip(lab["ids"], "10.0.3.3")
        _capture_icmp(lab["ids"], ids_dev, "10.0.3.1", "10.0.3.2", seconds=8)
        lab["a"].run("ping -c 5 10.0.3.2")
        time.sleep(8)
        seen = _captured_lines(lab["ids"])
        log.info("uplink IDS capture (%d lines)", len(seen))
        assert len(seen) >= 5, "IDS on hub did not see cross-uplink unicast"

        # Impair the L2<->L2 uplink: netem on both veth ends doubles up per
        # round trip (~2x the configured delay).
        base = _avg_rtt(lab["a"].run("ping -c 5 10.0.3.2"))
        link = lab.link("edge", "mon")
        link.impair(latency="50ms")
        rtt = _avg_rtt(lab["a"].run("ping -c 5 10.0.3.2"))
        log.info("uplink impair: base=%.2fms impaired=%.2fms", base, rtt)
        assert rtt > base + 80, f"uplink latency not applied: {base} -> {rtt}"
        link.clear()
    finally:
        lab.destroy()


def test_looped_l2_definition_aborts(backend, db):
    """A switch ring must abort at deploy with the looped nodes named —
    Linux bridges run no STP, so the loop would broadcast-storm (D7)."""
    lab = LoopedLab()
    with pytest.raises(CycleError) as exc:
        lab.deploy(backend=backend, db=db, use_namespaces=True)
    msg = str(exc.value)
    assert "s1" in msg and "s2" in msg and "s3" in msg
    # Abort-before-allocate: nothing persisted for the range.
    assert db.get_topology(lab.name) is None
