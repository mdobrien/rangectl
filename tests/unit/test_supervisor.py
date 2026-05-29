"""Unit tests for Phase 8 — rangectl.supervisor.

The supervisor creates a range's per-range directories and libvirt configs,
sets up its named network namespace + management network, and launches libvirtd
inside PID+mount+UTS namespaces (network isolation comes from the named netns,
so ``unshare`` itself does not request --net). Teardown kills libvirtd's
host-PID — the kernel then reaps every QEMU child — and removes the namespace,
network, and directories.

All subprocess/launch/kill calls and the netns module are mocked. ``range_dir``
points at a tmp directory so directory creation, config writes, and the state
file are exercised for real.
"""
from __future__ import annotations
import json
import signal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from rangectl import supervisor
from rangectl.netns import MgmtNetwork
from rangectl.supervisor import RangeInfo


def _ok(stdout: str = "", stderr: str = "", rc: int = 0):
    m = MagicMock()
    m.returncode = rc
    m.stdout = stdout
    m.stderr = stderr
    return m


@pytest.fixture
def fake_mgmt():
    return MgmtNetwork(
        bridge_name="mgmt-br",
        veth_host="mgh12345678",
        veth_ns="mgp12345678",
        host_ip="10.255.1.254",
        subnet="10.255.1.0/24",
    )


@pytest.fixture
def created(tmp_path, fake_mgmt):
    """Run create_range with everything external mocked; return (info, ctx)."""
    proc = MagicMock()
    proc.pid = 9999
    with patch("rangectl.supervisor._run") as run, \
         patch("rangectl.supervisor.subprocess.Popen", return_value=proc) as popen, \
         patch("rangectl.supervisor.netns.create_mgmt_network",
               return_value=fake_mgmt) as cmn:
        run.return_value = _ok()
        info = supervisor.create_range("lab1", "10.255.1.0/24",
                                       range_dir=str(tmp_path))
    return info, {"run": run, "popen": popen, "cmn": cmn,
                  "root": tmp_path, "proc": proc}


# --- create_range ----------------------------------------------------------

def test_create_range_returns_rangeinfo(created):
    info, ctx = created
    assert isinstance(info, RangeInfo)
    assert info.name == "lab1"
    assert info.pid == 9999
    assert info.netns_name == "rangectl-lab1"
    assert info.mgmt_subnet == "10.255.1.0/24"
    assert info.veth_host == "mgh12345678"
    assert info.veth_ns == "mgp12345678"
    assert info.libvirt_socket == str(
        ctx["root"] / "lab1" / "run-libvirt" / "libvirt-sock")


def test_create_range_makes_bind_mount_dirs(created):
    _, ctx = created
    base = ctx["root"] / "lab1"
    for sub in ("run-libvirt", "log-libvirt", "cache-libvirt", "etc-libvirt",
                "lib-libvirt/qemu", "lib-libvirt/dnsmasq", "lib-libvirt/boot",
                "lib-libvirt/swtpm"):
        assert (base / sub).is_dir(), sub


def test_create_range_writes_qemu_conf(created):
    _, ctx = created
    conf = (ctx["root"] / "lab1" / "etc-libvirt" / "qemu.conf").read_text()
    assert 'security_driver = "none"' in conf
    assert "dynamic_ownership = 0" in conf
    assert 'user = "root"' in conf
    assert 'group = "root"' in conf


def test_create_range_writes_libvirtd_conf(created):
    _, ctx = created
    assert (ctx["root"] / "lab1" / "etc-libvirt" / "libvirtd.conf").exists()


def test_create_range_adds_named_netns(created):
    _, ctx = created
    cmds = [c.args[0] for c in ctx["run"].call_args_list]
    assert ["ip", "netns", "add", "rangectl-lab1"] in cmds


def test_create_range_wires_mgmt_network(created):
    _, ctx = created
    ctx["cmn"].assert_called_once_with("rangectl-lab1", "10.255.1.0/24", "lab1")


def test_create_range_launches_libvirtd_in_namespaces(created):
    _, ctx = created
    cmd = ctx["popen"].call_args.args[0]
    # Runs inside the named netns, then unshares pid/mount/uts.
    assert cmd[:4] == ["ip", "netns", "exec", "rangectl-lab1"]
    assert "unshare" in cmd
    for flag in ("--pid", "--fork", "--mount", "--uts", "--mount-proc"):
        assert flag in cmd, flag
    assert "--propagation" in cmd and "private" in cmd
    # Network isolation is supplied by the named netns, not unshare.
    assert "--net" not in cmd
    # The inner script bind-mounts and execs libvirtd.
    script = cmd[-1]
    assert "exec /usr/sbin/libvirtd" in script
    assert "--config" in script
    assert "/run/libvirt/libvirtd.pid" in script


def test_create_range_bind_mounts_and_blocks_dbus(created):
    _, ctx = created
    script = ctx["popen"].call_args.args[0][-1]
    base = str(ctx["root"] / "lab1")
    assert f"mount --bind {base}/run-libvirt /run/libvirt" in script
    assert f"mount --bind {base}/etc-libvirt /etc/libvirt" in script
    assert f"mount --bind {base}/lib-libvirt/qemu /var/lib/libvirt/qemu" in script
    # dbus is blocked with an empty dir bind mount.
    assert "/run/dbus" in script
    # The shared image registry is NEVER bind-mounted over.
    assert "/var/lib/libvirt/images" not in script


def test_create_range_persists_state_file(created):
    info, ctx = created
    state = json.loads(
        (ctx["root"] / "lab1" / "range.json").read_text())
    assert state["pid"] == 9999
    assert state["netns_name"] == "rangectl-lab1"
    assert state["veth_host"] == "mgh12345678"
    assert state["subnet"] == "10.255.1.0/24"


# --- destroy_range ---------------------------------------------------------

def test_destroy_range_kills_pid_and_cleans_up(created):
    info, ctx = created
    root = ctx["root"]
    with patch("rangectl.supervisor._run") as run, \
         patch("rangectl.supervisor.os.kill") as kill, \
         patch("rangectl.supervisor.netns.destroy_mgmt_network") as dmn, \
         patch("rangectl.supervisor.time.sleep"):
        run.return_value = _ok()
        supervisor.destroy_range("lab1", range_dir=str(root))

    # libvirtd host-PID is signalled (SIGKILL guarantees pidns reap).
    signals = [c.args[1] for c in kill.call_args_list]
    assert signal.SIGKILL in signals
    # mgmt network torn down with the reconstructed handle.
    dmn.assert_called_once()
    mgmt_arg = dmn.call_args.args[0]
    assert mgmt_arg.veth_host == "mgh12345678"
    assert mgmt_arg.subnet == "10.255.1.0/24"
    # named netns deleted.
    cmds = [c.args[0] for c in run.call_args_list]
    assert ["ip", "netns", "del", "rangectl-lab1"] in cmds
    # range directory removed.
    assert not (root / "lab1").exists()


def test_destroy_range_missing_state_is_noop(tmp_path):
    # Destroying a range with no state file must not raise.
    with patch("rangectl.supervisor._run") as run, \
         patch("rangectl.supervisor.os.kill") as kill:
        run.return_value = _ok()
        supervisor.destroy_range("ghost", range_dir=str(tmp_path))
    kill.assert_not_called()


def test_destroy_range_tolerates_dead_pid(created):
    info, ctx = created
    root = ctx["root"]
    with patch("rangectl.supervisor._run") as run, \
         patch("rangectl.supervisor.os.kill",
               side_effect=ProcessLookupError) as kill, \
         patch("rangectl.supervisor.netns.destroy_mgmt_network"), \
         patch("rangectl.supervisor.time.sleep"):
        run.return_value = _ok()
        # Should swallow the already-dead PID and finish cleanup.
        supervisor.destroy_range("lab1", range_dir=str(root))
    assert not (root / "lab1").exists()
