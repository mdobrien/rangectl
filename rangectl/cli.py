"""rangectl command-line tool (Phase 14).

Day-2 operations on ranges deployed via the SDK. The CLI does not define
topologies — it connects to running ranges (``Range.connect``) and drives them:
inspect, exec, upload, power nodes, snapshot, freeze, destroy, manage images.

Each subcommand: parse args -> ``Range.connect(name)`` -> SDK call -> format
output -> exit code. Exit codes: 0 success, 1 error, 2 range/node not found.
``rangectl exec`` passes through the remote command's exit code.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from rangectl import mgmt_namespace, supervisor
from rangectl.state import StateDB
from rangectl.topology import Range
from rangectl.types import RangeNotRunning

KEYS_ROOT = "~/.rangectl/keys"


# --- helpers ---------------------------------------------------------------

def _err(msg: str) -> None:
    print(f"Error: {msg}", file=sys.stderr)


def _range_info(name: str) -> dict | None:
    """Read a range's persisted runtime state (range.json), or None if absent."""
    p = Path(supervisor.DEFAULT_RANGE_DIR) / name / "range.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())


def _require_range_info(name: str) -> dict:
    info = _range_info(name)
    if info is None:
        raise RangeNotRunning(name, f"no range.json under "
                              f"{supervisor.DEFAULT_RANGE_DIR}/{name}")
    return info


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    """Print a left-aligned table padded to the widest cell per column."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    for row in rows:
        print(fmt.format(*[str(c) for c in row]))


# --- discovery & inspection ------------------------------------------------

def cmd_list(args: argparse.Namespace) -> int:
    ranges = Range.list()
    if not ranges:
        print("No ranges found.")
        return 0
    rows = [[r["name"], r["status"], r["node_count"], r["mgmt_subnet"],
             r.get("created_at") or "-"] for r in ranges]
    _print_table(["NAME", "STATUS", "NODES", "SUBNET", "CREATED"], rows)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    rng = Range.connect(args.range)
    nodes = rng._db.list_nodes(rng.name)
    detail = []
    for n in nodes:
        if n["os_type"] in ("switch", "hub"):
            # L2 devices have no VM — render their DB state, never query
            # power/SSH (Phase 20, D8).
            live_status = n.get("state", "?")
        else:
            try:
                live_status = rng[n["name"]].status
            except Exception:
                live_status = n.get("state", "?")
        detail.append({
            "name": n["name"],
            "status": live_status,
            "ip": n.get("mgmt_ip"),
            "image": n["image"],
            "os": n["os_type"],
            "vcpu": n["vcpu"],
            "memory_mb": n["memory_mb"],
        })
    if args.yaml:
        import yaml
        print(yaml.dump({"range": rng.name, "nodes": detail},
                        default_flow_style=False, sort_keys=False), end="")
        return 0
    print(f"Range: {rng.name}")
    rows = [[d["name"], d["status"], d["ip"] or "-", d["image"] or "-",
             d["os"], d["vcpu"], d["memory_mb"]] for d in detail]
    _print_table(["NODE", "STATUS", "IP", "IMAGE", "OS", "VCPU", "MEM(MB)"],
                 rows)
    return 0


# --- node interaction ------------------------------------------------------

def _get_node(rng: Range, node_name: str):
    try:
        return rng[node_name]
    except KeyError:
        _err(f"node '{node_name}' not found in range '{rng.name}'. "
             f"Run 'rangectl status {rng.name}' to list nodes.")
        return None


def cmd_exec(args: argparse.Namespace) -> int:
    rng = Range.connect(args.range)
    node = _get_node(rng, args.node)
    if node is None:
        return 2
    if args.interactive:
        return _interactive_ssh(rng, args.node, node)
    result = node.exec(" ".join(args.command))
    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.exit_code


def _interactive_ssh(rng: Range, node_name: str, node) -> int:
    """Drop into a native SSH session using the per-range key."""
    key = os.path.expanduser(f"{KEYS_ROOT}/{rng.name}/id_ed25519")
    user = getattr(node, "ssh_user", "ubuntu")
    os.execvp("ssh", [
        "ssh", "-i", key,
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        f"{user}@{node.mgmt_ip}",
    ])
    return 0  # unreachable — execvp replaces the process


def cmd_upload(args: argparse.Namespace) -> int:
    rng = Range.connect(args.range)
    node = _get_node(rng, args.node)
    if node is None:
        return 2
    node.upload(args.src, args.dst)
    print(f"Uploaded {args.src} -> {args.node}:{args.dst}")
    return 0


def cmd_ssh_config(args: argparse.Namespace) -> int:
    rng = Range.connect(args.range)
    key = f"{KEYS_ROOT}/{rng.name}/id_ed25519"
    blocks = []
    for node in rng._nodes.values():
        user = getattr(node, "ssh_user", "ubuntu")
        blocks.append(
            f"Host {rng.name}-{node.name}\n"
            f"    HostName {node.mgmt_ip}\n"
            f"    User {user}\n"
            f"    IdentityFile {key}\n"
            f"    StrictHostKeyChecking no\n"
            f"    UserKnownHostsFile /dev/null\n"
        )
    print("\n".join(blocks))
    return 0


# --- node power ------------------------------------------------------------

def cmd_node(args: argparse.Namespace) -> int:
    rng = Range.connect(args.range)
    node = _get_node(rng, args.node)
    if node is None:
        return 2
    if args.action == "status":
        print(node.status)
        return 0
    getattr(node, args.action)()
    print(f"{args.node}: {args.action} ok")
    return 0


# --- virsh & namespace access ----------------------------------------------

def cmd_virsh(args: argparse.Namespace) -> int:
    # Ensure the range is live, then hand off to virsh against its socket.
    Range.connect(args.range)
    info = _require_range_info(args.range)
    socket = info["libvirt_socket"]
    os.execvp("virsh", ["virsh", "-c",
                        f"qemu+unix:///system?socket={socket}", *args.command])
    return 0  # unreachable


def cmd_netns(args: argparse.Namespace) -> int:
    info = _require_range_info(args.range)
    os.execvp("ip", ["ip", "netns", "exec", info["netns_name"], *args.command])
    return 0  # unreachable


# --- logs & debugging ------------------------------------------------------

def cmd_logs(args: argparse.Namespace) -> int:
    rng = Range.connect(args.range)
    if args.node:
        entries = rng[args.node].logs(level=args.level)
    else:
        entries = rng.logs(level=args.level)
    if not entries:
        print("No log entries.")
        return 0
    for e in entries:
        node = e.get("node_name") or "*"
        print(f"{e.get('timestamp', '')} [{e['level']}] {node}: {e['message']}")
    return 0


def _print_vlan_table(bridge: str, links: list[dict], netns: str) -> None:
    """Per-port 802.1Q config of a vlan-aware switch (Phase 25): declared
    config from the links table, plus the live kernel VLAN table when
    readable. Unconfigured ports keep the bridge default (PVID 1)."""
    import json
    import subprocess
    node_name = bridge.split("-", 1)[1]
    print("      vlans:")
    for lk in links:
        for me, other in (("a", "b"), ("b", "a")):
            if lk[f"node_{me}"] != node_name:
                continue
            cfg_raw = lk.get(f"vlan_{me}")
            peer = f"{lk[f'node_{other}']}/{lk[f'iface_{other}']}"
            if not cfg_raw:
                desc = "default (pvid 1, untagged)"
            else:
                cfg = json.loads(cfg_raw)
                if cfg["mode"] == "access":
                    desc = f"access {cfg['vids'][0]}"
                else:
                    desc = "trunk " + ",".join(str(v) for v in cfg["vids"])
                    if cfg.get("native") is not None:
                        desc += f" native {cfg['native']}"
            print(f"        {lk[f'iface_{me}']:<8} <- {peer:<16} {desc}")
    print("        (unconfigured ports default to pvid 1, untagged)")
    res = subprocess.run(
        ["ip", "netns", "exec", netns, "bridge", "vlan", "show"],
        capture_output=True, text=True)
    if res.returncode == 0 and res.stdout.strip():
        print("      live vlan table (bridge vlan show):")
        for line in res.stdout.strip().splitlines():
            print(f"        {line}")


def cmd_net(args: argparse.Namespace) -> int:
    rng = Range.connect(args.range)
    info = _require_range_info(args.range)
    netns = info["netns_name"]
    print(f"Range: {rng.name} (netns: {netns})")
    print(f"  mgmt subnet: {info.get('subnet')}")
    print(f"  veth: {info.get('veth_host')} <-> {info.get('veth_ns')} "
          f"(host <-> ns)")
    print("  nodes:")
    for n in rng._db.list_nodes(rng.name):
        print(f"    {n['name']:<12} {n.get('mgmt_ip') or '-':<16} "
              f"({n['os_type']})")
    # L2 devices (switches/hubs) with their enslaved ports. Port listing is
    # best-effort (`bridge link show` inside the netns, may need root).
    l2_bridges = [b for b in rng._db.list_bridges(rng.name)
                  if b["bridge_type"] in ("switch", "hub")]
    import subprocess
    if l2_bridges:
        ports_by_bridge: dict[str, list[str]] = {}
        res = subprocess.run(
            ["ip", "netns", "exec", netns, "bridge", "link", "show"],
            capture_output=True, text=True)
        if res.returncode == 0:
            for line in res.stdout.strip().splitlines():
                # "N: dev@ifN: <flags> mtu 1500 master br0 ..." — take the
                # port name and the bridge after "master".
                fields = line.split()
                if len(fields) < 2 or "master" not in fields:
                    continue
                dev = fields[1].rstrip(":").split("@", 1)[0]
                master = fields[fields.index("master") + 1]
                ports_by_bridge.setdefault(master, []).append(dev)
        print("  l2 devices:")
        links: list[dict] | None = None  # fetched only if a vlan-aware switch exists
        for b in l2_bridges:
            ports = ", ".join(ports_by_bridge.get(b["name"], [])) or "-"
            vlan_aware = bool(b.get("vlan_aware"))
            label = b["bridge_type"] + (", vlan-aware" if vlan_aware else "")
            print(f"    {b['name']:<12} ({label})  ports: {ports}")
            if vlan_aware:
                if links is None:
                    links = rng._db.list_links(rng.name)
                _print_vlan_table(b["name"], links, netns)
    res = subprocess.run(
        ["ip", "netns", "exec", netns, "ip", "-o", "link", "show",
         "type", "bridge"],
        capture_output=True, text=True)
    if res.returncode == 0 and res.stdout.strip():
        print("  bridges:")
        for line in res.stdout.strip().splitlines():
            # "N: name: <flags> ..." — take the interface name.
            parts = line.split(":")
            if len(parts) >= 2:
                print(f"    {parts[1].strip()}")
    return 0


def cmd_diagram(args: argparse.Namespace) -> int:
    """Render a topology picture: deployed range (by name) or YAML file."""
    if args.file:
        from rangectl.topology import Topology
        topo = Topology.from_yaml(args.file)
    elif args.range:
        topo = Range.connect(args.range).topology
    else:
        _err("diagram requires a range name or --file <topology.yaml>")
        return 1
    out = args.output or f"{topo.name}.{args.format}"
    path = topo.diagram(out, fmt=args.format, include_mgmt=args.mgmt)
    print(f"Diagram written to {path}")
    return 0


def cmd_ps(args: argparse.Namespace) -> int:
    info = _require_range_info(args.range)
    pid = info["pid"]
    import subprocess
    res = subprocess.run(["pstree", "-p", str(pid)],
                         capture_output=True, text=True)
    if res.returncode == 0:
        print(res.stdout, end="")
        return 0
    # Fallback: list direct children if pstree is unavailable.
    children = Path(f"/proc/{pid}/task/{pid}/children")
    print(f"libvirtd pid: {pid}")
    if children.exists():
        print(f"  children: {children.read_text().strip()}")
    return 0


# --- lifecycle -------------------------------------------------------------

def cmd_freeze(args: argparse.Namespace) -> int:
    Range.connect(args.range).freeze()
    print(f"Range '{args.range}' frozen.")
    return 0


def cmd_thaw(args: argparse.Namespace) -> int:
    Range.connect(args.range).thaw()
    print(f"Range '{args.range}' thawed.")
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    Range.connect(args.range).snapshot(args.name)
    print(f"Snapshot '{args.name}' created for range '{args.range}'.")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    Range.connect(args.range).restore(args.name)
    print(f"Range '{args.range}' restored to snapshot '{args.name}'.")
    return 0


def cmd_internet(args: argparse.Namespace) -> int:
    rng = Range.connect(args.range)
    if args.policy == "full":
        rng.enable_internet()
    else:
        rng.disable_internet()
    print(f"Range '{args.range}' internet: {args.policy}")
    return 0


def cmd_link(args: argparse.Namespace) -> int:
    if not args.action:
        _err("link requires an action: impair, clear, or status")
        return 1
    rng = Range.connect(args.range)
    try:
        link = rng.link(args.node_a, args.node_b)
    except KeyError:
        _err(f"no link between {args.node_a} and {args.node_b} in '{args.range}'")
        return 2
    if args.action == "impair":
        params = {k: getattr(args, k) for k in
                  ("latency", "jitter", "bandwidth", "loss", "reorder",
                   "corrupt", "duplicate", "outbound")
                  if getattr(args, k, None) is not None}
        link.impair(**params)
        print(f"Impaired {args.node_a} <-> {args.node_b}: "
              f"{', '.join(f'{k}={v}' for k, v in params.items())}")
    elif args.action == "clear":
        link.clear()
        print(f"Cleared impairments on {args.node_a} <-> {args.node_b}")
    elif args.action == "status":
        imp = link.impairments
        for node, params in imp.items():
            desc = ", ".join(f"{k}={v}" for k, v in params.items()) or "none"
            print(f"  {node} egress: {desc}")
    return 0


# --- capture & mirror (Phase 21) ---------------------------------------------

def cmd_capture(args: argparse.Namespace) -> int:
    rng = Range.connect(args.range)
    cap = rng.capture(args.node, args.iface, filter=args.filter,
                      output=args.output)
    print(f"Capture {cap.id} started on {cap.device}: {cap.file}")
    return 0


def cmd_capture_stop(args: argparse.Namespace) -> int:
    rng = Range.connect(args.range)
    cap = rng.stop_capture(args.id)
    note = " (possibly truncated)" if cap.possibly_truncated else ""
    print(f"Capture {cap.id} stopped: {cap.file}{note}")
    return 0


def cmd_captures(args: argparse.Namespace) -> int:
    rng = Range.connect(args.range)
    caps = rng.captures()
    if not caps:
        print("No captures.")
        return 0
    rows = [[c["id"], c["node_name"] or "-", c["iface"] or "-",
             c["device"] or "-", c["status"],
             (c["file"] or "-") + ("" if c["file_exists"] else " (missing)")]
            for c in caps]
    _print_table(["ID", "NODE", "IFACE", "DEVICE", "STATUS", "FILE"], rows)
    return 0


def cmd_mirror(args: argparse.Namespace) -> int:
    rng = Range.connect(args.range)
    rng.mirror(args.src_node, args.src_iface, to=args.dst_node,
               port=args.dst_iface, direction=args.direction)
    print(f"Mirroring {args.src_node}/{args.src_iface} -> "
          f"{args.dst_node}/{args.dst_iface} ({args.direction})")
    return 0


def cmd_unmirror(args: argparse.Namespace) -> int:
    rng = Range.connect(args.range)
    rng.unmirror(args.src_node, args.src_iface)
    print(f"Unmirrored {args.src_node}/{args.src_iface}")
    return 0


def cmd_mirrors(args: argparse.Namespace) -> int:
    rng = Range.connect(args.range)
    mirrors = rng.mirrors()
    if not mirrors:
        print("No mirrors.")
        return 0
    rows = [[f"{m['src_node']}/{m['src_iface']}",
             f"{m['dst_node']}/{m['dst_iface']}", m["direction"],
             "yes" if m["active"] else "NO"] for m in mirrors]
    _print_table(["SOURCE", "DEST", "DIRECTION", "ACTIVE"], rows)
    return 0


def cmd_destroy(args: argparse.Namespace) -> int:
    if args.all:
        ranges = Range.list()
        if not ranges:
            print("No ranges to destroy.")
            return 0
        for r in ranges:
            _destroy_one(r["name"])
        return 0
    if not args.range:
        _err("destroy requires a range name or --all")
        return 1
    return _destroy_one(args.range)


def _destroy_one(name: str) -> int:
    try:
        rng = Range.connect(name)
    except RangeNotRunning:
        # Orphaned / not running — fall back to force cleanup.
        Range.cleanup(name)
        print(f"Range '{name}' cleaned up (was not running).")
        return 0
    rng.destroy()
    print(f"Range '{name}' destroyed.")
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    Range.cleanup(args.range)
    print(f"Range '{args.range}' cleaned up.")
    return 0


# --- image management ------------------------------------------------------

def cmd_images(args: argparse.Namespace) -> int:
    db = StateDB()
    try:
        if args.images_action == "list":
            images = db.list_images()
            if not images:
                print("No images registered.")
                return 0
            rows = [[i["name"], i["os_type"], i["inject"],
                     i.get("size_mb") or "-", i["path"]] for i in images]
            _print_table(["NAME", "OS", "INJECT", "SIZE(MB)", "PATH"], rows)
            return 0
        if args.images_action == "add":
            size_mb = None
            if os.path.exists(args.path):
                size_mb = round(os.path.getsize(args.path) / (1024 * 1024))
            db.add_image(args.name, args.path, inject=args.inject,
                         os_type=args.os_type, size_mb=size_mb)
            print(f"Image '{args.name}' registered.")
            return 0
        if args.images_action == "remove":
            db.remove_image(args.name)
            print(f"Image '{args.name}' removed.")
            return 0
        if args.images_action == "info":
            img = db.get_image(args.name)
            if img is None:
                _err(f"image '{args.name}' not found. "
                     "Run 'rangectl images list' to see registered images.")
                return 1
            for k in ("name", "path", "os_type", "inject", "size_mb",
                      "built_from", "created_at"):
                if k in img:
                    print(f"  {k}: {img[k]}")
            return 0
        return 1
    finally:
        db.close()


# --- management namespace ----------------------------------------------------

# (status() key, human label) — one line per invariant item, in check order.
_MGMT_STATUS_ITEMS = [
    ("ns_exists", "namespace"),
    ("veth_host_up", "host veth up (veth-mgmt-host)"),
    ("veth_ns_up", "mgmt-ns veth up (veth-mgmt-ns)"),
    ("host_addr", "host transit address"),
    ("host_route", "host pool route"),
    ("host_forward", "host FORWARD accept"),
    ("host_masquerade", "host transit MASQUERADE"),
    ("host_ip_forward", "host ip_forward"),
    ("ns_addr", "mgmt-ns transit address"),
    ("ns_default_route", "mgmt-ns default route"),
    ("ns_lo_up", "mgmt-ns loopback up"),
    ("ns_ip_forward", "mgmt-ns ip_forward"),
]


def cmd_mgmt_ns(args: argparse.Namespace) -> int:
    if args.mgmt_action == "status":
        return _mgmt_ns_status()
    if args.mgmt_action == "reset":
        return _mgmt_ns_reset(args.force)
    _err("mgmt-ns requires an action: status or reset")
    return 1


def _mgmt_ns_status() -> int:
    """Read-only invariant report. Exit 0 only when every item is present."""
    st = mgmt_namespace.status()
    ranges = mgmt_namespace.connected_ranges()
    print(f"mgmt-ns: {st['namespace']}  transit={st['transit']}  "
          f"pool={st['aggregate']}")
    all_ok = True
    for key, label in _MGMT_STATUS_ITEMS:
        present = bool(st.get(key))
        all_ok = all_ok and present
        print(f"  {label:<32} {'OK' if present else 'MISSING'}")
    if not ranges:
        print("connected ranges: none")
    else:
        print("connected ranges:")
        for r in ranges:
            veth_ok = r["veth_present"]
            route_ok = r["route_present"]
            all_ok = all_ok and veth_ok and route_ok
            print(f"  {r['name']:<16} {r['subnet']:<18} "
                  f"veth {r['veth']} {'OK' if veth_ok else 'MISSING'}  "
                  f"route {'OK' if route_ok else 'MISSING'}")
    return 0 if all_ok else 1


def _mgmt_ns_reset(force: bool) -> int:
    """Destroy + recreate the mgmt-ns; ensure reconnects running ranges."""
    running = mgmt_namespace.running_ranges()
    if running and not force:
        names = ", ".join(r["name"] for r in running)
        _err(f"{len(running)} range(s) running ({names}); reset briefly drops "
             "their connectivity. Re-run with --force.")
        return 1
    mgmt_namespace.destroy_mgmt_ns()
    mgmt_namespace.ensure_mgmt_ns()
    print("mgmt-ns rebuilt (namespace, transit veth, host route/FORWARD/NAT).")
    for r in running:
        print(f"reconnected: {r['name']} ({r['subnet']})")
    return 0


# --- parser ----------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rangectl",
        description="Operate on ranges deployed via the rangectl SDK.")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("list", help="list ranges")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("status", help="per-node detail for a range")
    p.add_argument("range")
    p.add_argument("--yaml", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("exec", help="run a command on a node over SSH")
    p.add_argument("range")
    p.add_argument("node")
    p.add_argument("-i", "--interactive", action="store_true",
                   help="drop into an interactive SSH session")
    p.add_argument("command", nargs="*", help="command (after --)")
    p.set_defaults(func=cmd_exec)

    p = sub.add_parser("upload", help="copy a file to a node")
    p.add_argument("range")
    p.add_argument("node")
    p.add_argument("src")
    p.add_argument("dst")
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser("ssh-config", help="print SSH config block for a range")
    p.add_argument("range")
    p.set_defaults(func=cmd_ssh_config)

    p = sub.add_parser("node", help="node power operations")
    p.add_argument("range")
    p.add_argument("node")
    p.add_argument("action",
                   choices=["stop", "start", "restart", "status"])
    p.set_defaults(func=cmd_node)

    p = sub.add_parser("virsh", help="virsh scoped to a range's libvirt socket")
    p.add_argument("range")
    p.add_argument("command", nargs=argparse.REMAINDER,
                   help="virsh arguments")
    p.set_defaults(func=cmd_virsh)

    p = sub.add_parser("netns", help="run a command inside a range's netns")
    p.add_argument("range")
    p.add_argument("command", nargs=argparse.REMAINDER,
                   help="command (after --)")
    p.set_defaults(func=cmd_netns)

    p = sub.add_parser("logs", help="range or per-node logs")
    p.add_argument("range")
    p.add_argument("--node", help="filter to a single node")
    p.add_argument("--level", help="filter by log level")
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("net", help="network topology summary")
    p.add_argument("range")
    p.set_defaults(func=cmd_net)

    p = sub.add_parser("diagram", help="render a topology picture (graphviz)")
    p.add_argument("range", nargs="?", help="deployed range name")
    p.add_argument("--file", help="topology YAML (no deployment needed)")
    p.add_argument("-o", "--output", help="output path "
                                          "(default: <name>.<format>)")
    p.add_argument("--format", choices=["svg", "png", "dot"], default="svg")
    p.add_argument("--mgmt", action="store_true",
                   help="include the implicit eth0 mgmt NIC in node tables")
    p.set_defaults(func=cmd_diagram)

    p = sub.add_parser("ps", help="process tree inside the range's PID namespace")
    p.add_argument("range")
    p.set_defaults(func=cmd_ps)

    p = sub.add_parser("freeze", help="pause every process in the range")
    p.add_argument("range")
    p.set_defaults(func=cmd_freeze)

    p = sub.add_parser("thaw", help="resume a frozen range")
    p.add_argument("range")
    p.set_defaults(func=cmd_thaw)

    p = sub.add_parser("snapshot", help="snapshot all nodes")
    p.add_argument("range")
    p.add_argument("name")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("restore", help="restore all nodes to a snapshot")
    p.add_argument("range")
    p.add_argument("name")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("internet", help="toggle outbound internet")
    p.add_argument("range")
    p.add_argument("policy", choices=["full", "none"])
    p.set_defaults(func=cmd_internet)

    p = sub.add_parser("destroy", help="tear down a range")
    p.add_argument("range", nargs="?")
    p.add_argument("--all", action="store_true", help="tear down every range")
    p.set_defaults(func=cmd_destroy)

    p = sub.add_parser("cleanup", help="force-remove an orphaned range")
    p.add_argument("range")
    p.set_defaults(func=cmd_cleanup)

    p = sub.add_parser("link", help="impair, clear, or inspect a link")
    p.add_argument("range")
    p.add_argument("node_a")
    p.add_argument("node_b")
    lsub = p.add_subparsers(dest="action")
    lp = lsub.add_parser("impair", help="apply tc netem impairments")
    lp.add_argument("--latency")
    lp.add_argument("--jitter")
    lp.add_argument("--bandwidth")
    lp.add_argument("--loss")
    lp.add_argument("--reorder")
    lp.add_argument("--corrupt")
    lp.add_argument("--duplicate")
    lp.add_argument("--outbound", help="degrade only this node's egress")
    lsub.add_parser("clear", help="remove all impairments")
    lsub.add_parser("status", help="show current impairments")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("capture", help="start a tcpdump capture in a range")
    p.add_argument("range")
    p.add_argument("node")
    p.add_argument("iface", nargs="?",
                   help="interface (omit for switch/hub: captures the bridge)")
    p.add_argument("--filter", help='BPF filter, e.g. "tcp port 80"')
    p.add_argument("--output", help="pcap path (default: "
                                    "/ranges/<range>/captures/cap-<id>.pcap)")
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("capture-stop", help="stop a running capture")
    p.add_argument("range")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_capture_stop)

    p = sub.add_parser("captures", help="list captures (live status)")
    p.add_argument("range")
    p.set_defaults(func=cmd_captures)

    p = sub.add_parser("mirror", help="mirror a port's traffic to a sensor")
    p.add_argument("range")
    p.add_argument("src_node")
    p.add_argument("src_iface")
    p.add_argument("dst_node")
    p.add_argument("dst_iface")
    p.add_argument("--direction", choices=["ingress", "egress", "both"],
                   default="both")
    p.set_defaults(func=cmd_mirror)

    p = sub.add_parser("unmirror", help="remove a port mirror")
    p.add_argument("range")
    p.add_argument("src_node")
    p.add_argument("src_iface")
    p.set_defaults(func=cmd_unmirror)

    p = sub.add_parser("mirrors", help="list mirrors (live status)")
    p.add_argument("range")
    p.set_defaults(func=cmd_mirrors)

    p = sub.add_parser("mgmt-ns",
                       help="persistent management namespace operations")
    msub = p.add_subparsers(dest="mgmt_action")
    msub.add_parser("status",
                    help="invariant check, read-only (exit 1 if anything "
                         "missing)")
    mp = msub.add_parser("reset",
                         help="destroy + recreate the mgmt-ns and reconnect "
                              "running ranges")
    mp.add_argument("--force", action="store_true",
                    help="proceed even when ranges are running (brief "
                         "connectivity blip)")
    p.set_defaults(func=cmd_mgmt_ns)

    p = sub.add_parser("images", help="manage registered images")
    isub = p.add_subparsers(dest="images_action")
    isub.add_parser("list", help="list registered images")
    ip = isub.add_parser("add", help="register an image")
    ip.add_argument("name")
    ip.add_argument("path")
    ip.add_argument("--inject", default="pre-baked",
                    help="injection method (e.g. cloud-init)")
    ip.add_argument("--os-type", dest="os_type", default="linux",
                    choices=["linux", "vyos", "windows", "container"])
    ip = isub.add_parser("remove", help="unregister an image")
    ip.add_argument("name")
    ip = isub.add_parser("info", help="show image detail")
    ip.add_argument("name")
    p.set_defaults(func=cmd_images)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except RangeNotRunning as e:
        _err(str(e))
        print(f"Hint: run 'rangectl list' to see ranges, or "
              f"'rangectl cleanup {e.name}' to clear orphaned state.",
              file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        _err(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
