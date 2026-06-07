"""Phase 19 integration — link properties / WAN simulation (Gate 2).

Stand up a two-node Ubuntu range, then drive tc netem impairments through the
SDK and measure the effect with ping: latency shows up as RTT, 100% loss makes
ping fail, asymmetric impairment slows one direction only, and clear() restores
the clean link. Definition-time defaults are checked with a second range.

Requires libvirt + KVM (EC2). Namespace mode, runs as root.
"""
from __future__ import annotations
import logging
import re
import subprocess

from rangectl import Range
from tests.integration.conftest import pytestmark_skip

log = logging.getLogger(__name__)
pytestmark = pytestmark_skip

RANGE_NAME = "impair"


class ImpairLab(Range):
    name = RANGE_NAME

    def define_nodes(self):
        self.a = self.node("a", image="ubuntu-22.04", vcpu=1, memory=1024)
        self.b = self.node("b", image="ubuntu-22.04", vcpu=1, memory=1024,
                           depends_on=[self.a])

    def define_network(self):
        self.link(self.a.eth1["10.0.1.1/24"], self.b.eth1["10.0.1.2/24"])

    def verify(self):
        self.expect_reach(self.b, "10.0.1.1")


class DefaultsLab(Range):
    name = "impairdef"

    def define_nodes(self):
        self.a = self.node("a", image="ubuntu-22.04", vcpu=1, memory=1024)
        self.b = self.node("b", image="ubuntu-22.04", vcpu=1, memory=1024,
                           depends_on=[self.a])

    def define_network(self):
        # Definition-time impairment default — applied at deploy.
        self.link(self.a.eth1["10.0.1.1/24"], self.b.eth1["10.0.1.2/24"],
                  latency="80ms")

    def verify(self):
        self.expect_reach(self.b, "10.0.1.1")


def _avg_rtt(ping_output: str) -> float:
    """Parse average RTT (ms) from ping summary 'rtt min/avg/max/mdev'."""
    m = re.search(r"=\s*[\d.]+/([\d.]+)/", ping_output)
    assert m, f"no rtt summary in ping output:\n{ping_output}"
    return float(m.group(1))


def _tap_has_netem(netns: str, tap: str) -> bool:
    """True if the TAP carries a netem qdisc inside the range netns. A ping
    RTT cannot tell apart which direction is impaired (every round trip crosses
    each TAP exactly once), so asymmetry is verified at the qdisc level."""
    r = subprocess.run(
        ["ip", "netns", "exec", netns, "tc", "qdisc", "show", "dev", tap],
        capture_output=True, text=True)
    return "netem" in r.stdout


def test_link_impairment_via_ping(backend, db):
    lab = ImpairLab()
    try:
        lab.deploy(backend=backend, db=db, use_namespaces=True)
        link = lab.link("a", "b")

        # Baseline — clean link, sub-millisecond to low single-digit RTT.
        base = _avg_rtt(lab["a"].run("ping -c 5 10.0.1.2"))
        log.info("baseline avg RTT = %.3f ms", base)
        assert base < 20

        # Symmetric latency: netem adds 100ms egress on BOTH taps, so a 1-way
        # ping traverses one impaired tap -> ~100ms RTT.
        link.impair(latency="100ms")
        lat = _avg_rtt(lab["a"].run("ping -c 5 10.0.1.2"))
        log.info("impaired avg RTT = %.3f ms", lat)
        assert lat > 80, f"expected >80ms RTT, got {lat}"

        # State tracking.
        assert lab.link("a", "b").impairments == {
            "a": {"latency": "100ms"}, "b": {"latency": "100ms"}}

        # Clear — back to baseline.
        link.clear()
        cleared = _avg_rtt(lab["a"].run("ping -c 5 10.0.1.2"))
        log.info("cleared avg RTT = %.3f ms", cleared)
        assert cleared < 20
        assert lab.link("a", "b").impairments == {"a": {}, "b": {}}

        # Asymmetric: impair only one endpoint's TAP. Verified at the qdisc
        # level — exactly one TAP carries netem.
        netns = link._backend._netns_name
        tap_a = link._backend._find_tap_for_mac(*link._endpoints[0])
        tap_b = link._backend._find_tap_for_mac(*link._endpoints[1])
        link.impair(latency="200ms", outbound="a")
        assert _tap_has_netem(netns, tap_a), "a's TAP should have netem"
        assert not _tap_has_netem(netns, tap_b), "b's TAP should be clean"
        assert lab.link("a", "b").impairments == {
            "a": {"latency": "200ms"}, "b": {}}
        # Any round trip crosses the single impaired TAP once -> ~200ms RTT.
        asym_rtt = _avg_rtt(lab["a"].run("ping -c 5 10.0.1.2"))
        log.info("asym single-tap avg RTT = %.1f ms", asym_rtt)
        assert asym_rtt > 150, f"expected >150ms, got {asym_rtt}"
        link.clear()
        assert not _tap_has_netem(netns, tap_a), "clear should remove netem"

        # 100% loss — ping fails outright.
        link.impair(loss="100%")
        assert lab["a"].exec("ping -c 3 -W 2 10.0.1.2").exit_code != 0
        link.clear()
        assert lab["a"].exec("ping -c 2 -W 2 10.0.1.2").exit_code == 0

        # Impairment survives link down/up (re-applied after restoration).
        link.impair(latency="100ms")
        link.down()
        link.up()
        survived = _avg_rtt(lab["a"].run("ping -c 5 10.0.1.2"))
        log.info("post-up avg RTT = %.3f ms", survived)
        assert survived > 80, f"impairment lost after up(), got {survived}"
        link.clear()

        # Bandwidth: tbf root qdisc with netem child, verified at qdisc level,
        # plus the link still carries traffic at the shaped latency.
        link.impair(bandwidth="10mbit", latency="20ms")
        show = subprocess.run(
            ["ip", "netns", "exec", netns, "tc", "qdisc", "show", "dev", tap_a],
            capture_output=True, text=True).stdout
        log.info("qdisc on %s: %s", tap_a, show.strip())
        assert "tbf" in show, f"tbf qdisc missing: {show}"
        assert "netem" in show, f"netem child missing: {show}"
        assert lab["a"].exec("ping -c 2 -W 2 10.0.1.2").exit_code == 0
        link.clear()
    finally:
        lab.destroy()


def test_definition_time_default_impairment(backend, db):
    lab = DefaultsLab()
    try:
        lab.deploy(backend=backend, db=db, use_namespaces=True)
        # The 80ms default declared at link() time should already be live.
        rtt = _avg_rtt(lab["a"].run("ping -c 5 10.0.1.2"))
        log.info("default-impaired avg RTT = %.3f ms", rtt)
        assert rtt > 60, f"definition-time latency not applied, got {rtt}"
    finally:
        lab.destroy()
