"""Unit tests for Phase 10 — socket/netns-aware LibvirtBackend.

Two modes:
- legacy (socket=None, netns=None): virsh and ip commands run host-level
  exactly as before — protects the existing integration behaviour.
- per-range (socket + netns set): virsh connects over the range's unix socket;
  bridge/IP operations run inside the range's network namespace with clean
  bridge names.

All subprocess calls are mocked.
"""
from __future__ import annotations
from unittest.mock import MagicMock, patch

from rangectl.libvirt_backend import LibvirtBackend
from rangectl.networking import ns_bridge_name, ns_mgmt_bridge_name
from rangectl.types import InterfaceSpec, OSType, VMSpec


def _ok(stdout: str = "", stderr: str = "", rc: int = 0):
    m = MagicMock()
    m.returncode = rc
    m.stdout = stdout
    m.stderr = stderr
    return m


def _cmds(run):
    return [c.args[0] for c in run.call_args_list]


SOCKET = "/ranges/lab1/run-libvirt/libvirt-sock"
CONN = f"qemu+unix:///system?socket={SOCKET}"


def _spec():
    return VMSpec(
        name="lab1-router", image="ubuntu-22.04", vcpu=1, memory=512,
        os_type=OSType.LINUX, overlay_path="/img/router.qcow2",
        interfaces=[InterfaceSpec(
            node_name="router", interface_name="mgmt",
            bridge="mgmt-br", mac="52:54:00:aa:bb:01")],
        topology_name="lab1",
    )


# --- networking clean-name helpers -----------------------------------------

def test_ns_bridge_name_is_clean():
    assert ns_bridge_name(0) == "data-0"
    assert ns_bridge_name(3) == "data-3"


def test_ns_mgmt_bridge_name_is_clean():
    assert ns_mgmt_bridge_name() == "mgmt-br"


# --- legacy mode (backward compatible) -------------------------------------

def test_legacy_virsh_has_no_connection_uri():
    be = LibvirtBackend()
    with patch("rangectl.libvirt_backend._run") as run:
        run.return_value = _ok()
        be.create_vm(_spec())
    cmd = _cmds(run)[0]
    assert cmd[0] == "virsh"
    assert "-c" not in cmd


def test_legacy_create_bridge_runs_host_level():
    be = LibvirtBackend()
    with patch("rangectl.libvirt_backend._run") as run:
        run.return_value = _ok()
        be.create_bridge("rl-abc-0")
    cmd = _cmds(run)[0]
    assert cmd[:4] == ["ip", "link", "add", "name"]
    assert cmd[0] != "ip" or cmd[1] != "netns"


def test_legacy_assign_host_ip_installs_isolation_rule():
    be = LibvirtBackend()
    with patch("rangectl.libvirt_backend._run") as run:
        def side_effect(cmd, **kw):
            if cmd[:2] == ["iptables", "-C"]:
                return _ok(rc=1)  # check miss → rule inserted
            return _ok()
        run.side_effect = side_effect
        be.assign_host_ip("rlmgt-abc", "10.255.1.254", "24")
    cmds = _cmds(run)
    # Legacy host-IP path still installs the mgmt-isolation FORWARD DROP rule.
    assert any(c[:3] == ["iptables", "-I", "FORWARD"] for c in cmds)


# --- per-range mode (socket) -----------------------------------------------

def test_socket_create_vm_uses_connection_uri():
    be = LibvirtBackend(libvirt_socket=SOCKET)
    with patch("rangectl.libvirt_backend._run") as run:
        run.return_value = _ok()
        be.create_vm(_spec())
    cmd = _cmds(run)[0]
    assert cmd[:4] == ["virsh", "-c", CONN, "define"]


def test_socket_start_stop_destroy_use_connection_uri():
    be = LibvirtBackend(libvirt_socket=SOCKET)
    with patch("rangectl.libvirt_backend._run") as run:
        run.return_value = _ok(stdout="shut off")
        be.start("lab1-router")
        be.destroy("lab1-router")
    for cmd in _cmds(run):
        assert cmd[0] == "virsh"
        assert cmd[1:3] == ["-c", CONN]


def test_socket_domstate_uses_connection_uri():
    be = LibvirtBackend(libvirt_socket=SOCKET)
    with patch("rangectl.libvirt_backend._run") as run:
        run.return_value = _ok(stdout="running")
        be._dom_state("lab1-router")
    cmd = _cmds(run)[0]
    assert cmd == ["virsh", "-c", CONN, "domstate", "lab1-router"]


# --- per-range mode (netns) ------------------------------------------------

def test_netns_create_bridge_runs_in_namespace():
    be = LibvirtBackend(netns_name="rangectl-lab1")
    with patch("rangectl.libvirt_backend._run") as run:
        run.return_value = _ok()
        be.create_bridge("data-0")
    cmds = _cmds(run)
    assert cmds[0] == ["ip", "netns", "exec", "rangectl-lab1",
                       "ip", "link", "add", "name", "data-0", "type", "bridge"]
    assert ["ip", "netns", "exec", "rangectl-lab1",
            "ip", "link", "set", "data-0", "up"] in cmds


def test_netns_delete_bridge_runs_in_namespace():
    be = LibvirtBackend(netns_name="rangectl-lab1")
    with patch("rangectl.libvirt_backend._run") as run:
        run.return_value = _ok()
        be.delete_bridge("data-0")
    for cmd in _cmds(run):
        assert cmd[:4] == ["ip", "netns", "exec", "rangectl-lab1"]


def test_netns_assign_host_ip_runs_in_namespace_no_isolation_rule():
    be = LibvirtBackend(netns_name="rangectl-lab1")
    with patch("rangectl.libvirt_backend._run") as run:
        run.return_value = _ok()
        be.assign_host_ip("mgmt-br", "10.255.1.254", "24")
    cmds = _cmds(run)
    assert ["ip", "netns", "exec", "rangectl-lab1",
            "ip", "addr", "add", "10.255.1.254/24", "dev", "mgmt-br"] in cmds
    # Structural isolation replaces the legacy FORWARD DROP rule.
    assert not any(c[:2] == ["iptables", "-I"] for c in cmds)


def test_socket_and_netns_together():
    be = LibvirtBackend(libvirt_socket=SOCKET, netns_name="rangectl-lab1")
    with patch("rangectl.libvirt_backend._run") as run:
        run.return_value = _ok()
        be.create_vm(_spec())
        be.create_bridge("data-1")
    cmds = _cmds(run)
    assert cmds[0][:3] == ["virsh", "-c", CONN]
    assert cmds[1][:4] == ["ip", "netns", "exec", "rangectl-lab1"]
