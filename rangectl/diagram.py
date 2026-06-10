"""Topology diagram generation (Option A — DOT emit + graphviz subprocess).

``build_dot`` is a pure function over a Topology *definition*: no deployed
range, no StateDB, no kernel. It works on both freshly-defined topologies
(SDK / YAML) and the reconstructed topology behind ``Range.connect`` — the
two carry interface/VLAN data slightly differently, and both paths are
handled here so there is exactly one renderer.

``render`` shells out to the ``dot`` binary. If the binary is missing the
DOT text is written next to the requested output so nothing is lost, and a
clear error explains how to install graphviz.
"""
from __future__ import annotations

import html
import shutil
import subprocess
from pathlib import Path
from typing import Any

from rangectl.types import OSType

# (fill, accent) per os_type — accent is used for the border and badge text.
_NODE_COLORS: dict[str, tuple[str, str]] = {
    "vyos": ("#FEF5E7", "#B9770E"),
    "linux": ("#EBF5FB", "#2874A6"),
    "windows": ("#F4ECF7", "#7D3C98"),
    "container": ("#E9F7EF", "#1E8449"),
    "switch": ("#EAECEE", "#566573"),
    "hub": ("#F2F3F4", "#797D7F"),
}
_DEFAULT_COLORS = ("#EBF5FB", "#2874A6")

_FONT = "Helvetica"


def _esc(text: str) -> str:
    """Escape for use inside Graphviz HTML-like labels."""
    return html.escape(str(text), quote=False)


def _vlan_desc(vlan: dict | None) -> str | None:
    """Human form of a port's 802.1Q config: access(10) / trunk(10,20) nat 5."""
    if not vlan:
        return None
    if vlan["mode"] == "access":
        return f"access({vlan['vids'][0]})"
    desc = "trunk(" + ",".join(str(v) for v in vlan["vids"]) + ")"
    if vlan.get("native") is not None:
        desc += f" native {vlan['native']}"
    return desc


def _iface_ip(spec: Any) -> str | None:
    """``ip/cidr`` display string for an InterfaceSpec, or None if no IP."""
    if not getattr(spec, "ip", None):
        return None
    cidr = getattr(spec, "cidr", None)
    return f"{spec.ip}/{cidr}" if cidr else str(spec.ip)


def _collect_interfaces(topology: Any) -> dict[str, dict[str, str | None]]:
    """Per VM/container node: {iface_name: ip_display_or_None}.

    Union of node._interfaces (definition path) and link endpoint specs
    (Range.connect path, where node._interfaces is not populated).
    """
    rows: dict[str, dict[str, str | None]] = {
        n.name: {} for n in topology._nodes.values() if not n.is_l2
    }
    for node in topology._nodes.values():
        if node.is_l2:
            continue
        for iface, spec in node._interfaces.items():
            rows[node.name][iface] = _iface_ip(spec)
    for link in topology._links:
        for spec in (link.if_a, link.if_b):
            node = topology._nodes.get(spec.node_name)
            if node is None or node.is_l2:
                continue
            ip = _iface_ip(spec)
            if ip or spec.interface_name not in rows[node.name]:
                rows[node.name][spec.interface_name] = ip
    return rows


def _vm_node_dot(node: Any, ifaces: dict[str, str | None],
                 include_mgmt: bool) -> str:
    fill, accent = _NODE_COLORS.get(node.os_type.value, _DEFAULT_COLORS)
    rows = [
        f'<tr><td align="center"><b>{_esc(node.name)}</b></td></tr>',
        f'<tr><td align="center"><font point-size="9" color="{accent}">'
        f"{_esc(node.os_type.value)}</font></td></tr>",
    ]
    if include_mgmt:
        # eth0 is implicitly the mgmt NIC; its IP is allocated at deploy.
        rows.append(
            '<tr><td align="left"><font face="Courier" point-size="9">'
            "eth0 &#8212; mgmt</font></td></tr>"
        )
    for iface in sorted(ifaces):
        ip = ifaces[iface]
        detail = f"{_esc(iface)} &#8212; {_esc(ip)}" if ip else _esc(iface)
        rows.append(
            f'<tr><td align="left"><font face="Courier" point-size="9">'
            f"{detail}</font></td></tr>"
        )
    label = ('<<table border="0" cellborder="0" cellspacing="0" '
             'cellpadding="2">' + "".join(rows) + "</table>>")
    return (f'    "{node.name}" [shape=box, style="rounded,filled", '
            f'fillcolor="{fill}", color="{accent}", label={label}];')


def _l2_node_dot(node: Any) -> str:
    fill, accent = _NODE_COLORS[node.os_type.value]
    if node.os_type is OSType.SWITCH:
        shape = "box"
        subtitle = ("switch (vlan-aware)"
                    if getattr(node, "vlan_aware", False) else "switch")
    else:
        shape = "ellipse"
        subtitle = "hub"
    label = ('<<table border="0" cellborder="0" cellspacing="0" '
             'cellpadding="2">'
             f'<tr><td align="center"><b>{_esc(node.name)}</b></td></tr>'
             f'<tr><td align="center"><font point-size="9" color="{accent}">'
             f"{subtitle}</font></td></tr></table>>")
    return (f'    "{node.name}" [shape={shape}, style="filled", '
            f'fillcolor="{fill}", color="{accent}", label={label}];')


def _end_label(topology: Any, link: Any, side: str,
               iface_counts: dict[str, int]) -> str | None:
    """Edge-end label, or None when it would be redundant noise.

    L2 port ends are always labeled (port name + access/trunk config —
    switch/hub nodes have no per-port table rows). A VM end gets its iface
    name only when the node has >=2 data interfaces and the label actually
    disambiguates; with one interface the node table's ``iface — ip`` row
    already says it. IPs never appear on edge ends.
    """
    spec = link.if_a if side == "a" else link.if_b
    node = topology._nodes.get(spec.node_name)
    is_l2_end = node is not None and node.is_l2
    if not is_l2_end and iface_counts.get(spec.node_name, 0) < 2:
        return None
    parts = [spec.interface_name]
    vlan = getattr(spec, "vlan", None)
    if vlan is None and getattr(link, "_endpoints", None):
        # Range.connect path: VLAN config lives on the rebuilt endpoints.
        idx = 0 if side == "a" else 1
        if len(link._endpoints) == 2:
            vlan = link._endpoints[idx].vlan
    desc = _vlan_desc(vlan)
    if desc:
        parts.append(desc)
    return "\\n".join(parts)


def build_dot(topology: Any, include_mgmt: bool = False) -> str:
    """Emit Graphviz DOT for a topology definition. Pure — no subprocess,
    no kernel, no DB; works pre-deploy."""
    ifaces = _collect_interfaces(topology)
    iface_counts = {name: len(rows) for name, rows in ifaces.items()}
    lines = [
        f'graph "{topology.name}" {{',
        f'    label="{topology.name}"; labelloc=t; fontsize=16;',
        f'    fontname="{_FONT}";',
        "    nodesep=1.0; ranksep=1.2; splines=true;",
        f'    node [fontname="{_FONT}", margin=0.12];',
        f'    edge [fontname="{_FONT}", fontsize=9, color="#85929E", '
        'labelfontsize=8, labelfontcolor="#7F8C8D", labeldistance=3.0, '
        "labelangle=45];",
        "    rankdir=TB;",
    ]
    for node in topology._nodes.values():
        if node.is_l2:
            lines.append(_l2_node_dot(node))
        else:
            lines.append(_vm_node_dot(node, ifaces.get(node.name, {}),
                                      include_mgmt))
    for link in topology._links:
        attrs = []
        tail = _end_label(topology, link, "a", iface_counts)
        head = _end_label(topology, link, "b", iface_counts)
        if tail:
            attrs.append(f'taillabel="{tail}"')
        if head:
            attrs.append(f'headlabel="{head}"')
        if not getattr(link, "_is_up", True):
            attrs.append('style=dashed, color="#C0392B"')
        suffix = f' [{", ".join(attrs)}]' if attrs else ""
        lines.append(f'    "{link.if_a.node_name}" -- '
                     f'"{link.if_b.node_name}"{suffix};')
    lines.append("}")
    return "\n".join(lines) + "\n"


def render(dot_text: str, out_path: str | Path, fmt: str = "svg") -> Path:
    """Render DOT text to ``out_path`` via the ``dot`` binary.

    ``fmt="dot"`` writes the text directly (no binary needed). If ``dot``
    is missing, the text is saved as ``<out_path>.dot`` and a RuntimeError
    explains how to install graphviz.
    """
    out = Path(out_path)
    if fmt == "dot":
        out.write_text(dot_text)
        return out
    if fmt not in ("svg", "png"):
        raise ValueError(f"unsupported diagram format {fmt!r}: "
                         "use svg, png, or dot")
    dot_bin = shutil.which("dot")
    if dot_bin is None:
        fallback = out.with_suffix(".dot")
        fallback.write_text(dot_text)
        raise RuntimeError(
            f"graphviz 'dot' binary not found; DOT source saved to "
            f"{fallback}. Install graphviz (brew install graphviz / "
            f"apt-get install graphviz) and run: dot -T{fmt} {fallback} "
            f"-o {out}"
        )
    result = subprocess.run([dot_bin, f"-T{fmt}", "-o", str(out)],
                            input=dot_text, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"dot failed (exit {result.returncode}): "
                           f"{result.stderr.strip()}")
    return out
