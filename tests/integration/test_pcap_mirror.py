"""Phase 21 integration — packet capture & port mirroring (Gate 2).

Design: scratch/issues/20260609-14-phase21-pcap-mirror-design.md.
One main lab (a, b, ids VMs + a switch segment) drives:
  a) capture on a VM link -> pcap on host with packets; BPF filter variant
  b) capture on a switch (L2 node) sees segment traffic
  c) mirror a<->b traffic to the IDS link; ingress-only directional count;
     unmirror stops the copy
  d) netem impairment + mirror on the SAME TAP simultaneously
A second mini lab proves e) the kernel reaps the capture process with the
range — no orphan-cleanup code involved.

Requires libvirt + KVM (EC2). Namespace mode, runs as root.
"""
from __future__ import annotations
import logging
import re
import subprocess
import time

from rangectl import Range
from tests.integration.conftest import pytestmark_skip

log = logging.getLogger(__name__)
pytestmark = pytestmark_skip


class PcapLab(Range):
    name = "pcap"

    def define_nodes(self):
        self.a = self.node("a", image="ubuntu-22.04", vcpu=1, memory=1024)
        self.b = self.node("b", image="ubuntu-22.04", vcpu=1, memory=1024)
        self.ids = self.node("ids", image="ubuntu-22.04", vcpu=1, memory=1024)
        self.sw = self.switch("segsw")

    def define_network(self):
        self.link(self.a.eth1["10.0.1.1/24"], self.b.eth1["10.0.1.2/24"])
        self.link(self.a.eth2["10.0.2.1/24"], self.ids.eth1["10.0.2.2/24"])
        self.link(self.a.eth3["10.0.3.1/24"], self.sw.port0)
        self.link(self.b.eth3["10.0.3.2/24"], self.sw.port1)

    def verify(self):
        self.expect_reach(self.a, "10.0.1.2")
        self.expect_reach(self.a, "10.0.2.2")
        self.expect_reach(self.a, "10.0.3.2")


class ReapLab(Range):
    name = "reap"

    def define_nodes(self):
        self.a = self.node("a", image="ubuntu-22.04", vcpu=1, memory=1024)
        self.b = self.node("b", image="ubuntu-22.04", vcpu=1, memory=1024)

    def define_network(self):
        self.link(self.a.eth1["10.0.1.1/24"], self.b.eth1["10.0.1.2/24"])

    def verify(self):
        self.expect_reach(self.a, "10.0.1.2")


def _avg_rtt(ping_output: str) -> float:
    m = re.search(r"=\s*[\d.]+/([\d.]+)/", ping_output)
    assert m, f"no rtt summary in ping output:\n{ping_output}"
    return float(m.group(1))


def _settle():
    """Give freshly spawned tcpdump a moment to open its capture device."""
    time.sleep(2)


def test_capture_and_mirror(backend, db):
    lab = PcapLab()
    try:
        lab.deploy(backend=backend, db=db, use_namespaces=True)

        # --- a) capture on a VM link --------------------------------------
        cap = lab.capture("a", "eth1")
        _settle()
        lab["a"].run("ping -c 5 10.0.1.2")
        cap.stop()
        assert not cap.possibly_truncated
        n = cap.packet_count()
        log.info("unfiltered capture: %d packets (%s)", n, cap.file)
        assert n >= 10, f"expected >=10 ICMP packets (5 req + 5 rep), got {n}"

        # BPF filter variant: echo-requests only — replies excluded, so the
        # count is exactly the request count (proves in-kernel filtering).
        fcap = lab.capture("a", "eth1", filter="icmp[icmptype] = 8")
        _settle()
        lab["a"].run("ping -c 5 10.0.1.2")
        fcap.stop()
        fn = fcap.packet_count()
        log.info("filtered capture: %d packets", fn)
        assert fn == 5, f"expected exactly 5 echo-requests, got {fn}"

        # Capture index reads live state (D5-B).
        listing = {c["id"]: c for c in lab.captures()}
        assert listing[cap.id]["status"] == "stopped"
        assert listing[cap.id]["file_exists"] is True

        # --- b) capture on the switch sees segment traffic ----------------
        swcap = lab.capture("segsw")
        _settle()
        lab["a"].run("ping -c 5 10.0.3.2")
        swcap.stop()
        sn = swcap.packet_count()
        log.info("switch-bridge capture: %d packets", sn)
        assert sn > 0, "switch capture saw no segment traffic"

        # --- c) mirror a<->b to the IDS node -------------------------------
        # The 10.0.1.x subnet never appears on the ids link naturally, so any
        # 10.0.1.x frames on ids/eth1 are mirrored copies.
        lab.mirror("a", "eth1", to="ids", port="eth1")
        idscap = lab.capture("ids", "eth1", filter="icmp and net 10.0.1.0/24")
        _settle()
        lab["a"].run("ping -c 5 10.0.1.2")
        idscap.stop()
        mn = idscap.packet_count()
        log.info("mirrored-to-ids capture: %d packets", mn)
        assert mn >= 10, f"IDS should see mirrored req+rep, got {mn}"
        assert lab.mirrors()[0]["active"] is True

        # Directional: ingress on a's TAP = frames FROM a (echo-requests).
        # Replies (b->a) egress a's TAP and must NOT be mirrored.
        lab.mirror("a", "eth1", to="ids", port="eth1", direction="ingress")
        req_cap = lab.capture("ids", "eth1",
                              filter="icmp[icmptype] = 8 and net 10.0.1.0/24")
        rep_cap = lab.capture("ids", "eth1",
                              filter="icmp[icmptype] = 0 and net 10.0.1.0/24")
        _settle()
        lab["a"].run("ping -c 5 10.0.1.2")
        req_cap.stop()
        rep_cap.stop()
        reqs, reps = req_cap.packet_count(), rep_cap.packet_count()
        log.info("ingress-only mirror: %d requests, %d replies", reqs, reps)
        assert reqs >= 5, f"requests should be mirrored, got {reqs}"
        assert reps == 0, f"replies must NOT be mirrored (ingress-only), got {reps}"

        # Unmirror stops the copy.
        lab.unmirror("a", "eth1")
        offcap = lab.capture("ids", "eth1", filter="icmp and net 10.0.1.0/24")
        _settle()
        lab["a"].run("ping -c 5 10.0.1.2")
        offcap.stop()
        off = offcap.packet_count()
        log.info("post-unmirror capture: %d packets", off)
        assert off == 0, f"mirror still active after unmirror: {off} packets"
        assert lab.mirrors() == []

        # --- d) impairment + mirror on the SAME TAP simultaneously --------
        link = lab.link("a", "b")
        link.impair(latency="100ms")
        lab.mirror("a", "eth1", to="ids", port="eth1")
        dcap = lab.capture("ids", "eth1", filter="icmp and net 10.0.1.0/24")
        _settle()
        rtt = _avg_rtt(lab["a"].run("ping -c 5 10.0.1.2"))
        dcap.stop()
        dn = dcap.packet_count()
        log.info("impair+mirror: RTT=%.1fms, mirrored=%d packets", rtt, dn)
        assert rtt > 80, f"netem ineffective alongside mirror: {rtt}ms"
        assert dn > 0, "mirror ineffective alongside netem"

        # Mirror survives link down/up (re-applied from intent, D5-B).
        link.down()
        link.up()
        ucap = lab.capture("ids", "eth1", filter="icmp and net 10.0.1.0/24")
        _settle()
        lab["a"].run("ping -c 5 10.0.1.2")
        ucap.stop()
        un = ucap.packet_count()
        log.info("post-up mirror capture: %d packets", un)
        assert un > 0, "mirror lost after link.up()"
        lab.unmirror("a", "eth1")
        link.clear()
    finally:
        lab.destroy()


def test_capture_dies_with_range(backend, db):
    """e) kernel-reap proof: the capture lives in the range's PID namespace,
    so destroying the range kills it with zero capture-specific cleanup."""
    lab = ReapLab()
    deployed = False
    try:
        lab.deploy(backend=backend, db=db, use_namespaces=True)
        deployed = True
        cap = lab.capture("a", "eth1", output="/ranges/reap/captures/reapcap.pcap")
        _settle()
        probe = subprocess.run(["pgrep", "-f", "reapcap"],
                               capture_output=True, text=True)
        assert probe.returncode == 0, "tcpdump not running before destroy"
        log.info("tcpdump host pid(s) before destroy: %s",
                 probe.stdout.split())
        assert cap.running
    finally:
        if deployed:
            lab.destroy()
    time.sleep(2)
    probe = subprocess.run(["pgrep", "-f", "reapcap"],
                           capture_output=True, text=True)
    assert probe.returncode != 0, (
        f"tcpdump survived range destroy: pids {probe.stdout.split()}")
