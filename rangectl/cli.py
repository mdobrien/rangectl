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
        for b in l2_bridges:
            ports = ", ".join(ports_by_bridge.get(b["name"], [])) or "-"
            print(f"    {b['name']:<12} ({b['bridge_type']})  ports: {ports}")
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
