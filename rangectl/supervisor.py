"""Per-range libvirtd supervisor (Phase 8).

Launches one libvirtd per range inside its own PID, mount, and UTS namespaces.
Network isolation comes from a named network namespace (see ``netns.py``) that
the libvirtd is entered into via ``ip netns exec`` — so ``unshare`` itself does
not request ``--net``. This reconciles the two halves of the design: a named
netns is what ``netns.py`` manages bridges and veth pairs against, while
``unshare`` supplies the remaining namespaces.

Teardown kills libvirtd's host-PID; because it is PID 1 of the range's PID
namespace, the kernel then SIGKILLs every QEMU child — one kill, guaranteed
clean reap (validated in the feasibility spike, phase C/E).

Per-range state (host-PID, netns name, veth names, subnet) is persisted to
``<range_dir>/<name>/range.json`` so ``destroy_range`` can find what to tear
down given only the range name.
"""
from __future__ import annotations
import json
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from rangectl import netns
from rangectl.netns import MgmtNetwork

log = logging.getLogger(__name__)

DEFAULT_RANGE_DIR = "/ranges"
TERM_GRACE_SECONDS = 5
CGROUP_PLACE_TIMEOUT = 10  # seconds to wait for libvirtd to fork before giving up

# Per-range dir -> libvirt state path. The shared image registry
# (/var/lib/libvirt/images) is deliberately absent: every range reads base
# qcow2 images from it through their backing chains.
BIND_MOUNTS: list[tuple[str, str]] = [
    ("run-libvirt", "/run/libvirt"),
    ("log-libvirt", "/var/log/libvirt"),
    ("cache-libvirt", "/var/cache/libvirt"),
    ("etc-libvirt", "/etc/libvirt"),
    ("lib-libvirt/qemu", "/var/lib/libvirt/qemu"),
    ("lib-libvirt/dnsmasq", "/var/lib/libvirt/dnsmasq"),
    ("lib-libvirt/boot", "/var/lib/libvirt/boot"),
    ("lib-libvirt/swtpm", "/var/lib/libvirt/swtpm"),
]

QEMU_CONF = (
    'security_driver = "none"\n'
    'stdio_handler = "file"\n'
    "dynamic_ownership = 0\n"
    'user = "root"\n'
    'group = "root"\n'
)

LIBVIRTD_CONF = (
    'unix_sock_group = "libvirt"\n'
    'unix_sock_rw_perms = "0770"\n'
    'auth_unix_rw = "none"\n'
)


@dataclass
class RangeInfo:
    name: str
    pid: int              # libvirtd's host-PID
    netns_name: str       # ip netns name
    libvirt_socket: str   # <range_dir>/<name>/run-libvirt/libvirt-sock
    mgmt_subnet: str
    veth_host: str
    veth_ns: str


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    log.debug("RUN: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
    return result


def _make_dirs(range_path: Path) -> None:
    for sub, _ in BIND_MOUNTS:
        (range_path / sub).mkdir(parents=True, exist_ok=True)
    # Empty directory bind-mounted over /run/dbus to block dbus.
    (range_path / "dbus-block").mkdir(parents=True, exist_ok=True)


def _launch_script(range_path: Path) -> str:
    """Shell run inside the new mount namespace: bind-mount per-range state over
    libvirt's paths, block dbus, then exec libvirtd as PID 1."""
    base = str(range_path)
    lines = [
        f"mount --bind {base}/{sub} {dst}" for sub, dst in BIND_MOUNTS
    ]
    lines.append(f"mount --bind {base}/dbus-block /run/dbus")
    lines.append(
        "exec /usr/sbin/libvirtd "
        f"--config {base}/etc-libvirt/libvirtd.conf "
        "--pid-file /run/libvirt/libvirtd.pid"
    )
    return "; ".join(lines)


def _child_pids(pid: int) -> list[int]:
    """Direct children of ``pid`` (host PIDs), via /proc/<pid>/task/<pid>/children."""
    try:
        data = Path(f"/proc/{pid}/task/{pid}/children").read_text()
    except OSError:
        return []
    return [int(x) for x in data.split()]


def _place_in_cgroup(wrapper_pid: int, cgroup_path: str,
                     timeout: float = CGROUP_PLACE_TIMEOUT) -> None:
    """Move the libvirtd the wrapper forked into the range cgroup.

    This MUST run in the host namespace: ``ip netns exec`` gives the launch
    script a fresh ``/sys`` that shadows the cgroup2 mount, so libvirtd cannot
    place itself. ``unshare --fork`` makes libvirtd a direct child of the
    wrapper PID; we poll for it, then write it to ``cgroup.procs``. Every QEMU
    libvirtd later spawns inherits this cgroup (libvirt doesn't relocate them
    with dbus blocked), so the freezer + resource limits cover the whole tree.
    """
    procs = Path(cgroup_path) / "cgroup.procs"
    deadline = time.time() + timeout
    while time.time() < deadline:
        children = _child_pids(wrapper_pid)
        if children:
            for pid in children:
                try:
                    procs.write_text(str(pid))
                except OSError as exc:
                    log.warning("cgroup placement of pid %s failed: %s", pid, exc)
            return
        time.sleep(0.2)
    log.warning("no libvirtd child of %s within %ss; cgroup limits not applied",
                wrapper_pid, timeout)


def create_range(name: str, mgmt_subnet: str,
                 range_dir: str = DEFAULT_RANGE_DIR,
                 cgroup_path: str | None = None) -> RangeInfo:
    """Provision a range: directories, configs, named netns + mgmt network, and
    a libvirtd launched inside PID/mount/UTS namespaces.

    When ``cgroup_path`` is given, libvirtd self-places into that cgroup before
    exec, so it (and all QEMU children) are subject to the range's freezer and
    resource limits.
    """
    log.info("create_range: %s subnet=%s dir=%s cgroup=%s",
             name, mgmt_subnet, range_dir, cgroup_path)
    range_path = Path(range_dir) / name
    netns_name = f"rangectl-{name}"

    _make_dirs(range_path)
    (range_path / "etc-libvirt" / "qemu.conf").write_text(QEMU_CONF)
    (range_path / "etc-libvirt" / "libvirtd.conf").write_text(LIBVIRTD_CONF)

    _run(["ip", "netns", "add", netns_name])
    mgmt = netns.create_mgmt_network(netns_name, mgmt_subnet, name)

    cmd = [
        "ip", "netns", "exec", netns_name,
        "unshare", "--pid", "--fork", "--mount", "--uts",
        "--propagation", "private", "--mount-proc",
        "/bin/bash", "-c", _launch_script(range_path),
    ]
    proc = subprocess.Popen(cmd)

    # Place libvirtd into the range cgroup from the host namespace (it can't do
    # so itself — see _place_in_cgroup). Must happen before any VM starts so
    # QEMU children inherit the cgroup.
    if cgroup_path is not None:
        _place_in_cgroup(proc.pid, cgroup_path)

    socket_path = str(range_path / "run-libvirt" / "libvirt-sock")
    info = RangeInfo(
        name=name,
        pid=proc.pid,
        netns_name=netns_name,
        libvirt_socket=socket_path,
        mgmt_subnet=mgmt_subnet,
        veth_host=mgmt.veth_host,
        veth_ns=mgmt.veth_ns,
    )

    (range_path / "range.json").write_text(json.dumps({
        "pid": info.pid,
        "netns_name": info.netns_name,
        "veth_host": info.veth_host,
        "veth_ns": info.veth_ns,
        "host_ip": mgmt.host_ip,
        "subnet": info.mgmt_subnet,
        "libvirt_socket": info.libvirt_socket,
    }))

    return info


def _signal(pid: int, sig: int) -> bool:
    """Send ``sig`` to ``pid``; return False if the process is already gone."""
    try:
        os.kill(pid, sig)
        return True
    except ProcessLookupError:
        return False


def _terminate(wrapper_pid: int) -> None:
    """Kill the libvirtd tree rooted at the ``unshare`` wrapper.

    The wrapper only forks libvirtd; libvirtd is PID 1 of the range's pid-ns and
    owns every QEMU. Killing the wrapper alone leaves libvirtd (and its QEMU)
    leaked, which holds the range cgroup non-empty (rmdir EBUSY) and contends
    for host resources. So SIGTERM the wrapper's children (libvirtd) *and* the
    wrapper, grace, then SIGKILL survivors — killing libvirtd makes the kernel
    reap its QEMU children."""
    targets = _child_pids(wrapper_pid) + [wrapper_pid]
    survivors = [pid for pid in targets if _signal(pid, signal.SIGTERM)]
    if not survivors:
        return
    time.sleep(TERM_GRACE_SECONDS)
    for pid in survivors:
        _signal(pid, signal.SIGKILL)


def destroy_range(name: str, range_dir: str = DEFAULT_RANGE_DIR) -> None:
    """Kill libvirtd (kernel reaps QEMU), tear down the mgmt network and named
    netns, and remove the range directory. No-op if the range is unknown."""
    log.info("destroy_range: %s dir=%s", name, range_dir)
    range_path = Path(range_dir) / name
    state_file = range_path / "range.json"
    if not state_file.exists():
        log.warning("destroy_range: no state file for %s; nothing to do", name)
        return

    state = json.loads(state_file.read_text())
    _terminate(state["pid"])

    netns.destroy_mgmt_network(MgmtNetwork(
        bridge_name=netns.MGMT_BRIDGE,
        veth_host=state["veth_host"],
        veth_ns=state["veth_ns"],
        host_ip=state["host_ip"],
        subnet=state["subnet"],
    ))
    _run(["ip", "netns", "del", state["netns_name"]], check=False)

    import shutil
    shutil.rmtree(range_path, ignore_errors=True)
