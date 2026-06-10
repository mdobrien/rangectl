"""Unit tests for topology diagram generation (Gate 1).

build_dot is pure string emission over a topology *definition* — no deploy,
no StateDB, no graphviz binary needed. render() is tested with the binary
mocked away (fallback .dot behavior).
"""
from __future__ import annotations

import pytest

from rangectl.cli import main as cli_main
from rangectl.diagram import build_dot, render
from rangectl.topology import Range, Topology


def _p2p_topology() -> Topology:
    """Two-node p2p: vyos router <-> linux host."""
    t = Topology("p2p")
    router = t.node("router", image="vyos", os="vyos")
    host = t.node("host-a", image="ubuntu-22.04")
    t.link(router.eth1["10.0.1.1/24"], host.eth1["10.0.1.2/24"])
    return t


def _vlan_switch_topology() -> Topology:
    t = Topology("vlans")
    web = t.node("web", image="ubuntu-22.04")
    db = t.node("db", image="ubuntu-22.04")
    router = t.node("router", image="vyos")
    sw = t.switch("core", vlan_aware=True)
    t.link(web.eth1["10.0.10.2/24"], sw.port0.access(10))
    t.link(db.eth1["10.0.20.2/24"], sw.port1.access(20))
    t.link(router.eth1["10.0.99.1/24"], sw.port2.trunk(10, 20, native=5))
    return t


def _hub_topology() -> Topology:
    t = Topology("hubnet")
    a = t.node("a", image="ubuntu-22.04")
    ids = t.node("ids", image="ubuntu-22.04")
    hb = t.hub("mon")
    t.link(a.eth1["10.0.2.1/24"], hb.port0)
    t.link(ids.eth1["10.0.2.2/24"], hb.port1)
    return t


# --- build_dot: p2p ---------------------------------------------------------

def test_p2p_dot_has_nodes_os_types_ifaces_ips():
    dot = build_dot(_p2p_topology())
    assert '"router"' in dot and '"host-a"' in dot
    assert ">vyos<" in dot      # os_type badge
    assert ">linux<" in dot
    assert "eth1" in dot
    assert "10.0.1.1/24" in dot and "10.0.1.2/24" in dot
    # p2p edge with iface-only end labels (IPs live in the node tables).
    assert '"router" -- "host-a"' in dot
    assert 'taillabel="eth1"' in dot and 'headlabel="eth1"' in dot
    assert "10.0.1.1" not in dot.split("--")[-1].split("[")[1].split("]")[0]


def test_p2p_dot_is_valid_graph_block():
    dot = build_dot(_p2p_topology())
    assert dot.startswith('graph "p2p" {')
    assert dot.rstrip().endswith("}")
    assert 'fontname="Helvetica"' in dot


# --- build_dot: switch + VLANs ----------------------------------------------

def test_switch_dot_vlan_annotations_and_shape():
    dot = build_dot(_vlan_switch_topology())
    # Switch node: box shape, vlan-aware subtitle, fanned-out edges.
    assert '"core"' in dot
    assert "switch (vlan-aware)" in dot
    core_line = next(ln for ln in dot.splitlines() if ln.startswith('    "core" ['))
    assert "shape=box" in core_line
    # Each link is an edge to the L2 node, never an edge label between VMs.
    assert '"web" -- "core"' in dot
    assert '"db" -- "core"' in dot
    assert '"router" -- "core"' in dot
    # VLAN config annotated on the port end of each edge.
    assert "access(10)" in dot
    assert "access(20)" in dot
    assert "trunk(10,20) native 5" in dot
    # Port names appear on the L2 end labels.
    assert "port0" in dot and "port1" in dot and "port2" in dot


def test_plain_switch_subtitle_not_vlan_aware():
    t = Topology("plain")
    a = t.node("a", image="ubuntu-22.04")
    sw = t.switch("edge")
    t.link(a.eth1["10.0.3.1/24"], sw.port0)
    dot = build_dot(t)
    assert ">switch<" in dot
    assert "vlan-aware" not in dot


# --- build_dot: hub ----------------------------------------------------------

def test_hub_dot_shape_and_subtitle():
    dot = build_dot(_hub_topology())
    mon_line = next(ln for ln in dot.splitlines() if ln.startswith('    "mon" ['))
    assert "shape=ellipse" in mon_line
    assert ">hub<" in dot
    assert '"a" -- "mon"' in dot and '"ids" -- "mon"' in dot


# --- build_dot: mgmt toggle ---------------------------------------------------

def test_mgmt_nic_excluded_by_default():
    dot = build_dot(_p2p_topology())
    assert "eth0" not in dot
    assert "mgmt" not in dot


def test_mgmt_nic_included_on_request():
    dot = build_dot(_p2p_topology(), include_mgmt=True)
    assert "eth0" in dot
    assert "mgmt" in dot


# --- render -------------------------------------------------------------------

def test_render_missing_binary_writes_dot_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr("rangectl.diagram.shutil.which", lambda _: None)
    out = tmp_path / "topo.svg"
    dot_text = build_dot(_p2p_topology())
    with pytest.raises(RuntimeError, match="graphviz 'dot' binary not found"):
        render(dot_text, out, fmt="svg")
    fallback = tmp_path / "topo.dot"
    assert fallback.exists()
    assert fallback.read_text() == dot_text
    assert not out.exists()


def test_render_dot_format_needs_no_binary(tmp_path, monkeypatch):
    monkeypatch.setattr("rangectl.diagram.shutil.which", lambda _: None)
    out = tmp_path / "topo.dot"
    dot_text = build_dot(_p2p_topology())
    assert render(dot_text, out, fmt="dot") == out
    assert out.read_text() == dot_text


def test_render_invokes_dot_binary(tmp_path, monkeypatch):
    monkeypatch.setattr("rangectl.diagram.shutil.which",
                        lambda _: "/usr/bin/dot")
    calls = {}

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        calls["input"] = kwargs.get("input")

        class R:
            returncode = 0
            stderr = ""
        return R()

    monkeypatch.setattr("rangectl.diagram.subprocess.run", fake_run)
    out = tmp_path / "topo.png"
    dot_text = build_dot(_p2p_topology())
    render(dot_text, out, fmt="png")
    assert calls["cmd"][0] == "/usr/bin/dot"
    assert "-Tpng" in calls["cmd"]
    assert calls["input"] == dot_text


def test_render_rejects_unknown_format(tmp_path):
    with pytest.raises(ValueError, match="unsupported diagram format"):
        render("graph g {}", tmp_path / "x.pdf", fmt="pdf")


# --- SDK surface ---------------------------------------------------------------

class _DiagramLab(Range):
    name = "diaglab"

    def define_nodes(self):
        self.a = self.node("a", image="ubuntu-22.04")
        self.b = self.node("b", image="ubuntu-22.04")

    def define_network(self):
        self.link(self.a.eth1["10.0.1.1/24"], self.b.eth1["10.0.1.2/24"])

    def verify(self):
        pass


def test_range_subclass_diagram_before_deploy(tmp_path):
    out = tmp_path / "lab.dot"
    lab = _DiagramLab()
    path = lab.diagram(str(out), fmt="dot")
    assert path == str(out)
    text = out.read_text()
    assert '"a"' in text and '"b"' in text and "10.0.1.1/24" in text


def test_range_define_is_idempotent(tmp_path):
    lab = _DiagramLab()
    lab.define()
    lab.define()
    assert len(lab.topology._links) == 1


def test_topology_diagram_dot_format(tmp_path):
    out = tmp_path / "p2p.dot"
    path = _p2p_topology().diagram(str(out), fmt="dot")
    assert path == str(out)
    assert "10.0.1.2/24" in out.read_text()


# --- CLI -------------------------------------------------------------------------

def test_cli_diagram_from_yaml(tmp_path):
    yaml_path = tmp_path / "topo.yaml"
    _vlan_switch_topology().export(str(yaml_path))
    out = tmp_path / "vlans.dot"
    rc = cli_main(["diagram", "--file", str(yaml_path),
                   "-o", str(out), "--format", "dot"])
    assert rc == 0
    text = out.read_text()
    assert "switch (vlan-aware)" in text
    assert "access(10)" in text
    assert "trunk(10,20) native 5" in text


def test_cli_diagram_requires_range_or_file(capsys):
    rc = cli_main(["diagram"])
    assert rc == 1
    assert "requires a range name or --file" in capsys.readouterr().err
