from __future__ import annotations

import argparse

import pytest

from rangectl.link_properties import build_clear_cmds, build_netem_cmds
from rangectl.topology import Link, LinkEndpoint
from rangectl.types import InterfaceSpec


# --- tc command builders (pure functions) ----------------------------------

def test_build_netem_cmds_latency_only():
    cmds = build_netem_cmds("vnet0", "rangectl-r", latency="100ms")
    assert cmds == [
        ["ip", "netns", "exec", "rangectl-r",
         "tc", "qdisc", "replace", "dev", "vnet0", "root", "netem",
         "delay", "100ms"]
    ]


def test_build_netem_cmds_no_netns():
    cmds = build_netem_cmds("vnet0", None, latency="100ms")
    assert cmds == [
        ["tc", "qdisc", "replace", "dev", "vnet0", "root", "netem",
         "delay", "100ms"]
    ]


def test_build_netem_cmds_latency_and_jitter():
    cmds = build_netem_cmds("vnet0", None, latency="100ms", jitter="20ms")
    assert cmds[0][-3:] == ["delay", "100ms", "20ms"]


def test_build_netem_cmds_all_netem_params():
    cmds = build_netem_cmds("vnet0", None, latency="50ms", loss="5%",
                            duplicate="1%", corrupt="2%", reorder="25%")
    tail = cmds[0]
    assert tail[:6] == ["tc", "qdisc", "replace", "dev", "vnet0", "root"]
    assert "loss" in tail and "5%" in tail
    assert "duplicate" in tail and "corrupt" in tail and "reorder" in tail


def test_build_netem_cmds_reorder_without_latency_gets_default_delay():
    # netem reorder requires a delay to be present, so a base delay is injected.
    cmds = build_netem_cmds("vnet0", None, reorder="25%")
    assert "delay" in cmds[0]


def test_build_netem_cmds_with_bandwidth():
    cmds = build_netem_cmds("vnet0", "rangectl-r", bandwidth="10mbit",
                            latency="100ms")
    assert len(cmds) == 2
    tbf, netem = cmds
    assert tbf == ["ip", "netns", "exec", "rangectl-r", "tc", "qdisc",
                   "replace", "dev", "vnet0", "root", "handle", "1:", "tbf",
                   "rate", "10mbit", "burst", "32kbit", "latency", "50ms"]
    assert netem[:13] == ["ip", "netns", "exec", "rangectl-r", "tc", "qdisc",
                          "replace", "dev", "vnet0", "parent", "1:1", "handle",
                          "10:"]
    assert netem[-2:] == ["delay", "100ms"]


def test_build_netem_cmds_bandwidth_only():
    cmds = build_netem_cmds("vnet0", None, bandwidth="1mbit")
    # No netem opts -> just the tbf qdisc.
    assert len(cmds) == 1
    assert "tbf" in cmds[0] and "rate" in cmds[0] and "1mbit" in cmds[0]


def test_build_clear_cmds():
    assert build_clear_cmds("vnet0", "rangectl-r") == [
        ["ip", "netns", "exec", "rangectl-r",
         "tc", "qdisc", "del", "dev", "vnet0", "root"]
    ]
    assert build_clear_cmds("vnet0", None) == [
        ["tc", "qdisc", "del", "dev", "vnet0", "root"]
    ]


# --- Link.impair / clear / impairments -------------------------------------

def _make_link(backend):
    a = InterfaceSpec(node_name="a", interface_name="eth1", ip="10.0.1.1", cidr="24")
    b = InterfaceSpec(node_name="b", interface_name="eth1", ip="10.0.1.2", cidr="24")
    link = Link(a, b, topology=None)
    link._backend = backend
    link._bridge_name = "data-0"
    link._endpoints = [
        LinkEndpoint(node_name="a", bridge="data-0",
                     vm_id="vm-a", mac="52:54:00:aa:aa:aa"),
        LinkEndpoint(node_name="b", bridge="data-0",
                     vm_id="vm-b", mac="52:54:00:bb:bb:bb"),
    ]
    return link


def test_impair_symmetric(backend):
    link = _make_link(backend)
    link.impair(latency="100ms")
    # tc applied on both TAPs.
    taps = backend.tc_taps()
    assert taps == {"tap-vm-a", "tap-vm-b"}


def test_impair_asymmetric(backend):
    link = _make_link(backend)
    link.impair(latency="200ms", outbound="a")
    assert backend.tc_taps() == {"tap-vm-a"}


def test_impair_bad_outbound_raises(backend):
    link = _make_link(backend)
    with pytest.raises(ValueError):
        link.impair(latency="10ms", outbound="nope")


def test_clear_removes_impairments(backend):
    link = _make_link(backend)
    link.impair(latency="100ms")
    backend.calls.clear()
    link.clear()
    # A del command was issued for both taps.
    dels = [c for c in backend.tc_cmds() if "del" in c]
    assert len(dels) == 2
    assert link.impairments == {"a": {}, "b": {}}


def test_impairments_property_symmetric(backend):
    link = _make_link(backend)
    link.impair(latency="100ms", loss="5%")
    assert link.impairments == {
        "a": {"latency": "100ms", "loss": "5%"},
        "b": {"latency": "100ms", "loss": "5%"},
    }


def test_impairments_property_asymmetric(backend):
    link = _make_link(backend)
    link.impair(latency="200ms", outbound="a")
    assert link.impairments == {"a": {"latency": "200ms"}, "b": {}}


def test_up_reapplies_impairments(backend):
    link = _make_link(backend)
    link.impair(latency="100ms")
    link.down()
    backend.calls.clear()
    link.up()
    # After up(), the impairment was re-applied on both taps.
    assert backend.tc_taps() == {"tap-vm-a", "tap-vm-b"}


# --- CLI argument parsing ---------------------------------------------------

def test_cli_link_impair_parsing():
    from rangectl.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(
        ["link", "r1", "a", "b", "impair", "--latency", "100ms", "--loss", "5%"])
    assert args.range == "r1"
    assert args.node_a == "a"
    assert args.node_b == "b"
    assert args.action == "impair"
    assert args.latency == "100ms"
    assert args.loss == "5%"


def test_cli_link_clear_parsing():
    from rangectl.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["link", "r1", "a", "b", "clear"])
    assert args.action == "clear"


def test_cli_link_status_parsing():
    from rangectl.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["link", "r1", "a", "b", "status"])
    assert args.action == "status"
