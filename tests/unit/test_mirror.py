"""Unit tests for Phase 21 — port mirroring (rangectl/mirror.py).

Design: scratch/issues/20260609-14-phase21-pcap-mirror-design.md (D4, D5-B).
clsact + matchall + mirred copies a port's traffic to the sensor's resolved
endpoint device. clsact occupies a separate slot from the root qdisc, so
mirrors coexist with Phase 19 netem impairments on the same TAP.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from rangectl.engine import Engine
from rangectl.mirror import build_mirror_cmds, build_unmirror_cmds
from rangectl.topology import Topology


# --- tc command builders (pure functions) ------------------------------------

def test_build_mirror_cmds_both_directions():
    cmds = build_mirror_cmds("vnet0", "vnet9", "both", netns="rangectl-r")
    pre = ["ip", "netns", "exec", "rangectl-r"]
    # Idempotent install: del (tolerated when absent) then add, so re-applying
    # never stacks duplicate filters.
    assert cmds[0] == pre + ["tc", "qdisc", "del", "dev", "vnet0", "clsact"]
    assert cmds[1] == pre + ["tc", "qdisc", "add", "dev", "vnet0", "clsact"]
    assert cmds[2] == pre + ["tc", "filter", "add", "dev", "vnet0", "ingress",
                             "matchall", "action", "mirred", "egress",
                             "mirror", "dev", "vnet9"]
    assert cmds[3] == pre + ["tc", "filter", "add", "dev", "vnet0", "egress",
                             "matchall", "action", "mirred", "egress",
                             "mirror", "dev", "vnet9"]
    assert len(cmds) == 4


def test_build_mirror_cmds_ingress_only():
    cmds = build_mirror_cmds("vnet0", "vnet9", "ingress")
    hooks = [c[5] for c in cmds if c[1] == "filter"]
    assert hooks == ["ingress"]


def test_build_mirror_cmds_egress_only():
    cmds = build_mirror_cmds("vnet0", "vnet9", "egress")
    hooks = [c[5] for c in cmds if c[1] == "filter"]
    assert hooks == ["egress"]


def test_build_mirror_cmds_no_netns():
    cmds = build_mirror_cmds("vnet0", "vnet9", "both")
    assert cmds[0][0] == "tc"


def test_build_mirror_cmds_invalid_direction():
    with pytest.raises(ValueError, match="direction"):
        build_mirror_cmds("vnet0", "vnet9", "sideways")


def test_build_unmirror_cmds():
    assert build_unmirror_cmds("vnet0", netns="rangectl-r") == [
        ["ip", "netns", "exec", "rangectl-r",
         "tc", "qdisc", "del", "dev", "vnet0", "clsact"]
    ]
    assert build_unmirror_cmds("vnet0") == [
        ["tc", "qdisc", "del", "dev", "vnet0", "clsact"]
    ]


# --- Range.mirror / unmirror ---------------------------------------------------

def _deployed(backend, db):
    """a <-> b direct link; c (sensor) on its own link to a; switch with two
    VM ports for switch-port mirroring."""
    t = Topology("lab")
    a = t.node("a", image="ubuntu")
    b = t.node("b", image="ubuntu")
    c = t.node("c", image="ubuntu")
    sw = t.switch("core")
    t.link(a.eth1["10.0.1.1/24"], b.eth1["10.0.1.2/24"])
    t.link(c.eth1["10.0.2.2/24"], a.eth2["10.0.2.1/24"])
    t.link(b.eth2["10.0.3.1/24"], sw.port0)
    return Engine(backend, db).deploy(t)


def _tap(backend, rng, node, iface):
    from rangectl.topology import find_link_endpoint
    link, ep = find_link_endpoint(rng.topology, node, iface)
    return ep.resolve(backend)


def test_mirror_applies_clsact_and_filters(backend, db):
    rng = _deployed(backend, db)
    src = _tap(backend, rng, "a", "eth1")
    dst = _tap(backend, rng, "c", "eth1")
    backend.calls.clear()
    rng.mirror("a", "eth1", to="c", port="eth1")
    cmds = backend.tc_cmds()
    assert ["tc", "qdisc", "add", "dev", src, "clsact"] in [c[-6:] for c in cmds]
    filters = [c for c in cmds if "filter" in c]
    assert len(filters) == 2  # both directions by default
    for f in filters:
        assert f[f.index("mirror") + 2] == dst


def test_mirror_directional_single_filter(backend, db):
    rng = _deployed(backend, db)
    backend.calls.clear()
    rng.mirror("a", "eth1", to="c", port="eth1", direction="ingress")
    filters = [c for c in backend.tc_cmds() if "filter" in c]
    assert len(filters) == 1
    assert "ingress" in filters[0]


def test_mirror_invalid_direction(backend, db):
    rng = _deployed(backend, db)
    with pytest.raises(ValueError, match="direction"):
        rng.mirror("a", "eth1", to="c", port="eth1", direction="up")


def test_mirror_switch_port_uses_enslaved_tap(backend, db):
    # The switch side of a VM<->switch link has no device of its own — the
    # VM's TAP IS the port, so mirroring sw/port0 targets that TAP (D4).
    rng = _deployed(backend, db)
    port_tap = _tap(backend, rng, "b", "eth2")
    backend.calls.clear()
    rng.mirror("core", "port0", to="c", port="eth1")
    cmds = backend.tc_cmds()
    assert any(c[1] == "filter" and c[4] == port_tap for c in cmds)


def test_mirror_to_l2_port_without_device_errors(backend, db):
    rng = _deployed(backend, db)
    with pytest.raises(ValueError, match="bridge"):
        rng.mirror("a", "eth1", to="core", port="port0")


def test_mirror_unknown_src_iface_errors(backend, db):
    rng = _deployed(backend, db)
    with pytest.raises(ValueError, match="eth1"):
        rng.mirror("a", "eth9", to="c", port="eth1")


def test_unmirror_deletes_clsact_and_intent(backend, db):
    rng = _deployed(backend, db)
    src = _tap(backend, rng, "a", "eth1")
    rng.mirror("a", "eth1", to="c", port="eth1")
    backend.calls.clear()
    rng.unmirror("a", "eth1")
    cmds = backend.tc_cmds()
    assert ["tc", "qdisc", "del", "dev", src, "clsact"] in [c[-6:] for c in cmds]
    assert db.list_mirrors("lab") == []


def test_unmirror_without_mirror_is_clean(backend, db):
    rng = _deployed(backend, db)
    rng.unmirror("a", "eth1")  # run_tc tolerates the absent clsact
    assert db.list_mirrors("lab") == []


# --- persistence: intent rows + re-apply + connect rebuild (D5-B) -------------

def test_mirror_intent_persisted(backend, db):
    rng = _deployed(backend, db)
    rng.mirror("a", "eth1", to="c", port="eth1", direction="ingress")
    rows = db.list_mirrors("lab")
    assert len(rows) == 1
    assert rows[0]["src_node"] == "a"
    assert rows[0]["src_iface"] == "eth1"
    assert rows[0]["dst_node"] == "c"
    assert rows[0]["dst_iface"] == "eth1"
    assert rows[0]["direction"] == "ingress"


def test_mirror_replaces_existing_intent(backend, db):
    rng = _deployed(backend, db)
    rng.mirror("a", "eth1", to="c", port="eth1", direction="both")
    rng.mirror("a", "eth1", to="c", port="eth1", direction="egress")
    rows = db.list_mirrors("lab")
    assert len(rows) == 1
    assert rows[0]["direction"] == "egress"


def test_link_up_reapplies_mirror(backend, db):
    rng = _deployed(backend, db)
    src = _tap(backend, rng, "a", "eth1")
    rng.mirror("a", "eth1", to="c", port="eth1")
    link = rng.link("a", "b")
    link.down()
    backend.calls.clear()
    link.up()
    cmds = backend.tc_cmds()
    assert ["tc", "qdisc", "add", "dev", src, "clsact"] in [c[-6:] for c in cmds]
    assert any("filter" in c for c in cmds)


def test_link_up_without_mirror_adds_no_clsact(backend, db):
    rng = _deployed(backend, db)
    link = rng.link("a", "b")
    link.down()
    backend.calls.clear()
    link.up()
    assert not any("clsact" in c for c in backend.tc_cmds())


def test_impair_and_mirror_coexist_on_same_tap(backend, db):
    # netem owns the root qdisc; clsact occupies its own slot (Phase 19 + 21).
    rng = _deployed(backend, db)
    src = _tap(backend, rng, "a", "eth1")
    rng.link("a", "b").impair(latency="100ms")
    rng.mirror("a", "eth1", to="c", port="eth1")
    cmds = [c for c in backend.tc_cmds() if "dev" in c and
            c[c.index("dev") + 1] == src]
    assert any("netem" in c and "root" in c for c in cmds)
    assert any("clsact" in c and "add" in c for c in cmds)
    # The mirror never touched the root slot.
    assert not any("clsact" in c and "root" in c for c in cmds)


def test_mirrors_listing_reads_live_filter_state(backend, db):
    rng = _deployed(backend, db)
    src = _tap(backend, rng, "a", "eth1")
    rng.mirror("a", "eth1", to="c", port="eth1", direction="ingress")
    backend.tc_filter_results[(src, "ingress")] = (
        "filter protocol all pref 49152 matchall\n"
        "  action order 1: mirred (Egress Mirror to device tap-vm-3) pipe\n")
    listed = rng.mirrors()
    assert len(listed) == 1
    assert listed[0]["src_node"] == "a"
    assert listed[0]["active"] is True
    backend.tc_filter_results[(src, "ingress")] = ""
    assert rng.mirrors()[0]["active"] is False


def test_connect_rebuilds_mirror_intent(tmp_path, monkeypatch):
    """Mirror intent stored by one process is rebuilt by Range.connect() so
    unmirror / re-apply work cross-process."""
    from rangectl import topology as topo_mod
    from rangectl.state import StateDB
    from rangectl.topology import Range

    monkeypatch.setattr(topo_mod, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(topo_mod, "_netns_exists", lambda ns: True)
    monkeypatch.setattr(topo_mod.cgroup, "is_frozen", lambda name: False)

    db_file = str(tmp_path / "rangectl.db")
    db = StateDB(db_file)
    db.save_topology("lab", "running", "10.255.1.0/24", "mgmt-br")
    for i, n in enumerate(("a", "c")):
        db.save_node(topology_name="lab", name=n, image="ubuntu", vcpu=1,
                     memory_mb=1024, os_type="linux", state="running",
                     mgmt_ip=f"10.255.1.{i + 1}", vm_id=f"lab-{n}")
    db._conn.execute(
        "INSERT INTO links (topology_name, node_a, iface_a, ip_a, node_b, "
        "iface_b, ip_b, bridge_name) VALUES (?,?,?,?,?,?,?,?)",
        ("lab", "a", "eth1", "10.0.1.1", "c", "eth1", "10.0.1.2", "data-0"))
    db._conn.commit()
    db.save_mirror("lab", "a", "eth1", "c", "eth1", "ingress")
    db.close()

    rp = Path(tmp_path) / "ranges" / "lab"
    rp.mkdir(parents=True)
    sock = rp / "libvirt-sock"
    sock.write_text("")
    (rp / "range.json").write_text(json.dumps({
        "pid": 4242, "netns_name": "rangectl-lab", "veth_host": "mgh0001",
        "veth_ns": "mgp0001", "host_ip": "10.255.1.254",
        "subnet": "10.255.1.0/24", "libvirt_socket": str(sock)}))

    rng = Range.connect("lab", db_path=db_file,
                        range_dir=str(tmp_path / "ranges"))
    link = rng.topology._links[0]
    assert link._mirrors == {("a", "eth1"): {
        "dst_node": "c", "dst_iface": "eth1", "direction": "ingress"}}


# --- CLI parsing ---------------------------------------------------------------

def test_cli_capture_parsing():
    from rangectl.cli import build_parser
    args = build_parser().parse_args(
        ["capture", "lab", "a", "eth1", "--filter", "tcp port 80",
         "--output", "/tmp/x.pcap"])
    assert (args.range, args.node, args.iface) == ("lab", "a", "eth1")
    assert args.filter == "tcp port 80"
    assert args.output == "/tmp/x.pcap"


def test_cli_capture_stop_parsing():
    from rangectl.cli import build_parser
    args = build_parser().parse_args(["capture-stop", "lab", "3"])
    assert args.range == "lab"
    assert args.id == 3


def test_cli_captures_parsing():
    from rangectl.cli import build_parser
    args = build_parser().parse_args(["captures", "lab"])
    assert args.range == "lab"


def test_cli_mirror_parsing():
    from rangectl.cli import build_parser
    args = build_parser().parse_args(
        ["mirror", "lab", "a", "eth1", "ids", "eth0",
         "--direction", "ingress"])
    assert (args.src_node, args.src_iface) == ("a", "eth1")
    assert (args.dst_node, args.dst_iface) == ("ids", "eth0")
    assert args.direction == "ingress"


def test_cli_mirror_direction_default_both():
    from rangectl.cli import build_parser
    args = build_parser().parse_args(["mirror", "lab", "a", "eth1", "i", "e"])
    assert args.direction == "both"


def test_cli_unmirror_parsing():
    from rangectl.cli import build_parser
    args = build_parser().parse_args(["unmirror", "lab", "a", "eth1"])
    assert (args.range, args.src_node, args.src_iface) == ("lab", "a", "eth1")


def test_cli_mirrors_parsing():
    from rangectl.cli import build_parser
    args = build_parser().parse_args(["mirrors", "lab"])
    assert args.range == "lab"
