#!/usr/bin/env python3
"""Render diagrams for every integration-test topology DEFINITION.

Definition phase only — never deploys, never touches libvirt/EC2/StateDB.
Importable topologies are imported from their test modules; topologies built
inline inside test functions (test_topo7, test_ns_integration,
test_ns_regression) are re-declared here as faithful copies — see
scratch/issues/20260610-2-topology-diagram-options.md.

Output: scratch/capstone/diagrams/<name>.{svg,png}. If the graphviz `dot`
binary is missing, .dot sources are emitted instead.

Run from anywhere: python3 scratch/scripts/generate_test_topology_diagrams.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from rangectl import Topology  # noqa: E402
from rangectl.diagram import build_dot, render  # noqa: E402

OUT_DIR = REPO / "scratch" / "capstone" / "diagrams"


# --- re-declared inline topologies (faithful copies) -------------------------

def _topo7_vm_container() -> Topology:
    """test_topo7.py: nginx container <-> ubuntu VM, one /24 (inline)."""
    t = Topology("topo7")
    server = t.node("server", container="nginx:latest", vcpu=1, memory=128)
    client = t.node("client", image="ubuntu-22.04", vcpu=1, memory=1024,
                    depends_on=[server])
    t.link(server.eth1["10.0.1.1/24"], client.eth1["10.0.1.2/24"])
    return t


def _ns_two_node() -> Topology:
    """test_ns_integration.py test_ns_two_node (inline)."""
    t = Topology("nstwo")
    a = t.node("a", image="ubuntu-22.04", vcpu=1, memory=1024)
    b = t.node("b", image="ubuntu-22.04", vcpu=1, memory=1024)
    t.link(a.eth1["10.0.1.1/24"], b.eth1["10.0.1.2/24"])
    return t


def _ns_vyos_routed() -> Topology:
    """test_ns_integration.py test_ns_vyos_routed (inline)."""
    t = Topology("nsvyos")
    router = t.node("router", image="vyos", os="vyos", vcpu=1, memory=1024)
    a = t.node("ubuntu-a", image="ubuntu-22.04", vcpu=1, memory=1024,
               depends_on=[router])
    b = t.node("ubuntu-b", image="ubuntu-22.04", vcpu=1, memory=1024,
               depends_on=[router])
    t.link(a.eth1["10.0.1.2/24"], router.eth1["10.0.1.1/24"])
    t.link(router.eth2["10.0.2.1/24"], b.eth1["10.0.2.2/24"])
    return t


def _ns_mixed_vm_container() -> Topology:
    """test_ns_integration.py test_ns_mixed_vm_container (inline)."""
    t = Topology("nsmix")
    server = t.node("server", container="nginx:latest", vcpu=1, memory=128)
    client = t.node("client", image="ubuntu-22.04", vcpu=1, memory=1024,
                    depends_on=[server])
    t.link(server.eth1["10.0.1.1/24"], client.eth1["10.0.1.2/24"])
    return t


def _force_vyos(topo: Topology) -> Topology:
    """The _topoN helpers declare routers via image='vyos' only (os defaults
    to linux; the engine resolves the real os_type from the image registry at
    deploy). For definition-only diagrams, patch the os_type so the picture
    shows the router correctly."""
    from rangectl.types import OSType
    for node in topo._nodes.values():
        if node.image == "vyos":
            node.os_type = OSType.VYOS
    return topo


def _collect() -> list[tuple[str, Topology]]:
    """(output-filename, topology) for every integration-test topology."""
    from tests.integration.test_topo1 import _topo1
    from tests.integration.test_topo2 import _topo2
    from tests.integration.test_topo3 import _topo3
    from tests.integration.test_topo4 import _topo4
    from tests.integration.test_topo5 import _topo5
    from tests.integration.test_topo6 import _blue_team, _red_team
    from tests.integration.test_hub_switch import (
        HubLab, LoopedLab, SwitchLab, UplinkLab)
    from tests.integration.test_link_properties import DefaultsLab, ImpairLab
    from tests.integration.test_mgmt_ns_smoke import _two_node
    from tests.integration.test_pcap_mirror import PcapLab, ReapLab
    from tests.integration.test_sdk_range_class import TwoNodeLab
    from tests.integration.test_vlan import IsolationLab, RasLab

    def lab(cls) -> Topology:
        instance = cls()
        instance.define()  # definition phase only — no deploy
        return instance.topology

    return [
        # topo1-7 (module-level builders take backend/db; None = define only)
        ("topo1-p2p", _topo1(None, None)),
        ("topo2-routed-vyos", _topo2(None, None)),
        ("topo3-routed-web", _topo3(None, None)),
        ("topo4-diamond", _topo4(None, None)),
        ("topo5-link-toggle", _topo5(None, None)),
        ("topo6-red-team", _red_team(None, None)),
        ("topo6-blue-team", _blue_team(None, None)),
        ("topo7-vm-container", _topo7_vm_container()),
        # VLAN labs (Phase 25)
        ("vlan-isolation-lab", lab(IsolationLab)),
        ("vlan-ras-lab", lab(RasLab)),
        # hub/switch labs (Phase 20)
        ("hub-switch-switch-lab", lab(SwitchLab)),
        ("hub-switch-hub-lab", lab(HubLab)),
        ("hub-switch-uplink-lab", lab(UplinkLab)),
        ("hub-switch-looped-switches", lab(LoopedLab)),
        # link properties (Phase 19)
        ("link-props-impair-lab", lab(ImpairLab)),
        ("link-props-defaults-lab", lab(DefaultsLab)),
        # pcap/mirror (Phase 21)
        ("pcap-lab", lab(PcapLab)),
        ("pcap-reap-lab", lab(ReapLab)),
        # SDK Range class (Phase 15)
        ("sdk-two-node-lab", lab(TwoNodeLab)),
        # mgmt-ns smoke
        ("mgmt-ns-two-node", _two_node("mgmtns")),
        # ns integration/regression (inline re-declarations)
        ("ns-two-node", _ns_two_node()),
        ("ns-vyos-routed", _ns_vyos_routed()),
        ("ns-mixed-vm-container", _ns_mixed_vm_container()),
        # Skipped as exact shape duplicates already rendered above:
        #   test_ns_regression nstopo3/4/5 (= topo3/4/5), nsfreeze/nsinet*/
        #   nsres/_pair (= topo1 two-node shape).
    ]


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    failures = []
    for fname, topo in _collect():
        dot_text = build_dot(_force_vyos(topo))
        for fmt in ("svg", "png"):
            out = OUT_DIR / f"{fname}.{fmt}"
            try:
                render(dot_text, out, fmt=fmt)
                print(f"  wrote {out.relative_to(REPO)}")
            except RuntimeError as exc:
                failures.append(f"{out.name}: {exc}")
                print(f"  FAILED {out.name}: {exc}", file=sys.stderr)
                break  # .dot fallback already written; skip the other format
    if failures:
        print(f"\n{len(failures)} render(s) failed — .dot sources emitted "
              "instead (install graphviz and re-run).", file=sys.stderr)
        return 1
    print(f"\nAll diagrams in {OUT_DIR.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
