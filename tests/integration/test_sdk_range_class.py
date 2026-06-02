"""Phase 15 integration — Range lifecycle class (Gate 2).

Stand up a two-node Ubuntu range using ONLY the new Range-subclass SDK:
override define_nodes/define_network/verify, call deploy(), then drive the live
nodes through the polished LiveNode interface (run, route, snapshot) and tear
down. Proves the high-level API works end-to-end against real VMs.

Requires libvirt + KVM (EC2). Deploys in namespace mode (per-range libvirtd +
netns), so it must run as root.

The conftest `backend`/`db` fixtures supply a template LibvirtBackend and a
StateDB pre-loaded with the EC2 host's ubuntu image — passed to deploy() so the
range can resolve images. Everything else uses the public Range/LiveNode API.
"""
from __future__ import annotations
import logging

from rangectl import Range
from tests.integration.conftest import pytestmark_skip

log = logging.getLogger(__name__)
pytestmark = pytestmark_skip

RANGE_NAME = "sdkrange"


class TwoNodeLab(Range):
    name = RANGE_NAME

    def define_nodes(self):
        self.a = self.node("a", image="ubuntu-22.04", vcpu=1, memory=1024)
        self.b = self.node("b", image="ubuntu-22.04", vcpu=1, memory=1024,
                           depends_on=[self.a])

    def define_network(self):
        self.link(self.a.eth1["10.0.1.1/24"], self.b.eth1["10.0.1.2/24"])

    def verify(self):
        # b must reach a across the topology link before we call it READY.
        self.expect_reach(self.b, "10.0.1.1")


def test_range_subclass_full_lifecycle(backend, db):
    db_path = db._path
    lab = TwoNodeLab()
    try:
        # deploy() runs define_nodes -> define_network -> boot -> verify.
        lab.deploy(backend=backend, db=db, use_namespaces=True)
        assert "RUNNING" in repr(lab)

        # run() returns stdout and raises on non-zero exit.
        host_a = lab["a"].run("hostname")
        assert f"{RANGE_NAME}-a" in host_a
        assert lab["a"].run("which ip", check=False)  # truthy path string

        # node attribute rebinding: self.b is now a LiveNode after boot.
        assert lab.b is lab["b"]
        assert lab.b.run("hostname").strip().endswith("-b")

        # route() goes through the LinuxDriver -> `sudo ip route add`.
        lab["b"].route("10.99.0.0/24", via="10.0.1.1")
        routes = lab["b"].run("ip route")
        assert "10.99.0.0/24" in routes, f"route not installed: {routes}"

        # connectivity across the link both directions.
        assert lab["a"].exec("ping -c 2 -W 2 10.0.1.2").exit_code == 0

        # snapshot/restore round-trip via the range handle.
        lab["a"].run("echo baseline | sudo tee /tmp/marker")
        lab.snapshot("baseline")
        lab["a"].run("echo mutated | sudo tee /tmp/marker")
        lab.restore("baseline")
        assert "baseline" in lab["a"].run("cat /tmp/marker")

        lab.destroy()
        assert RANGE_NAME not in {r["name"] for r in Range.list(db_path=db_path)}
    finally:
        # Ensure no range leaks if an assertion failed before destroy().
        try:
            Range.connect(RANGE_NAME, db_path=db_path).destroy()
        except Exception:
            try:
                Range.cleanup(RANGE_NAME, db_path=db_path)
            except Exception:
                pass
