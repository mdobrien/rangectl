"""Unit tests for Phase 8 — rangectl.netns.

Network namespace management: management network (bridge + veth + host route
+ iptables), data-plane bridges, and command execution inside a netns. All
subprocess calls are mocked; tests assert the exact ip/iptables command
sequences.
"""
from __future__ import annotations
from unittest.mock import MagicMock, patch

from rangectl import netns
from rangectl.netns import MgmtNetwork


def _ok(stdout: str = "", stderr: str = "", rc: int = 0):
    m = MagicMock()
    m.returncode = rc
    m.stdout = stdout
    m.stderr = stderr
    return m


def _cmds(run) -> list[list[str]]:
    return [c.args[0] for c in run.call_args_list]


# --- create_mgmt_network ---------------------------------------------------

def test_create_mgmt_network_returns_clean_names():
    with patch("rangectl.netns._run") as run:
        run.return_value = _ok()
        mgmt = netns.create_mgmt_network("rangectl-lab1", "10.255.1.0/24", "lab1")
    assert isinstance(mgmt, MgmtNetwork)
    # Bridge name is clean — no hashing — because it lives inside the netns.
    assert mgmt.bridge_name == "mgmt-br"
    assert mgmt.host_ip == "10.255.1.254"
    assert mgmt.subnet == "10.255.1.0/24"
    # veth names are kernel-legal (<= 15 chars) and distinct.
    assert len(mgmt.veth_host) <= 15 and len(mgmt.veth_ns) <= 15
    assert mgmt.veth_host != mgmt.veth_ns


def test_create_mgmt_network_builds_bridge_in_netns():
    with patch("rangectl.netns._run") as run:
        run.return_value = _ok()
        netns.create_mgmt_network("rangectl-lab1", "10.255.1.0/24", "lab1")
    cmds = _cmds(run)
    # The bridge is created INSIDE the netns with the clean name.
    assert ["ip", "netns", "exec", "rangectl-lab1",
            "ip", "link", "add", "mgmt-br", "type", "bridge"] in cmds
    assert ["ip", "netns", "exec", "rangectl-lab1",
            "ip", "link", "set", "mgmt-br", "up"] in cmds


MGMT = ["ip", "netns", "exec", "rangectl-mgmt"]


def test_create_mgmt_network_creates_veth_pair_into_namespaces():
    with patch("rangectl.netns._run") as run:
        run.return_value = _ok()
        mgmt = netns.create_mgmt_network("rangectl-lab1", "10.255.1.0/24", "lab1")
    cmds = _cmds(run)
    # veth pair created on the host transiently, then BOTH ends moved out:
    # mgp into the range netns, mgh into the persistent mgmt-ns.
    assert ["ip", "link", "add", mgmt.veth_host, "type", "veth",
            "peer", "name", mgmt.veth_ns] in cmds
    assert ["ip", "link", "set", mgmt.veth_ns, "netns", "rangectl-lab1"] in cmds
    assert ["ip", "link", "set", mgmt.veth_host, "netns", "rangectl-mgmt"] in cmds
    # ns-side enslaved to the bridge inside the range netns.
    assert ["ip", "netns", "exec", "rangectl-lab1",
            "ip", "link", "set", mgmt.veth_ns, "master", "mgmt-br"] in cmds


def test_create_mgmt_network_assigns_gateway_ip_in_mgmt_ns():
    with patch("rangectl.netns._run") as run:
        run.return_value = _ok()
        mgmt = netns.create_mgmt_network("rangectl-lab1", "10.255.1.0/24", "lab1")
    cmds = _cmds(run)
    # The .254 gateway now lives on the mgmt-ns side of the veth.
    assert [*MGMT, "ip", "addr", "add", "10.255.1.254/24",
            "dev", mgmt.veth_host] in cmds
    assert [*MGMT, "ip", "link", "set", mgmt.veth_host, "up"] in cmds


def test_create_mgmt_network_installs_iptables_forward_accept_in_mgmt_ns():
    with patch("rangectl.netns._run") as run:
        # iptables -C (check) returns non-zero so the rule is inserted; all
        # other commands succeed.
        def side_effect(cmd, **kw):
            if cmd[:6] == [*MGMT, "iptables", "-C"]:
                return _ok(rc=1)
            return _ok()
        run.side_effect = side_effect
        netns.create_mgmt_network("rangectl-lab1", "10.255.1.0/24", "lab1")
    cmds = _cmds(run)
    assert [*MGMT, "iptables", "-I", "FORWARD", "1",
            "-s", "10.255.1.0/24", "-j", "ACCEPT"] in cmds
    assert [*MGMT, "iptables", "-I", "FORWARD", "1",
            "-d", "10.255.1.0/24", "-j", "ACCEPT"] in cmds


def test_create_mgmt_network_iptables_idempotent():
    with patch("rangectl.netns._run") as run:
        # -C returns 0 (rule already present) → no per-subnet ACCEPT insert.
        run.return_value = _ok(rc=0)
        netns.create_mgmt_network("rangectl-lab1", "10.255.1.0/24", "lab1")
    cmds = _cmds(run)
    inserts = [c for c in cmds if c[:6] == [*MGMT, "iptables", "-I"]]
    # The per-subnet ACCEPTs are skipped (already present), but the inter-range
    # isolation DROPs are always re-inserted (delete-then-insert by design) —
    # one per ordered pair of mgmt prefixes (mgh+/rlmgt+), covering cross-scheme.
    from rangectl.networking import mgmt_isolation_rules
    expected = [[*MGMT, "iptables", "-I", "FORWARD", "1", *rule]
                for rule in mgmt_isolation_rules()]
    assert inserts == expected


def test_create_mgmt_network_installs_inter_range_isolation_drop():
    """A DROP for mgh+ -> mgh+ must be re-asserted at the top of the mgmt-ns
    FORWARD chain, after the per-subnet ACCEPTs, so cross-range routing is
    blocked."""
    with patch("rangectl.netns._run") as run:
        # iptables -C returns non-zero (rule absent) so ACCEPTs insert; every
        # other command (ip link, etc.) succeeds.
        def side_effect(cmd, **kw):
            if cmd[:6] == [*MGMT, "iptables", "-C"]:
                return _ok(rc=1)
            return _ok()
        run.side_effect = side_effect
        netns.create_mgmt_network("rangectl-lab1", "10.255.1.0/24", "lab1")
    cmds = _cmds(run)
    drop_del = [*MGMT, "iptables", "-D", "FORWARD",
                "-i", "mgh+", "-o", "mgh+", "-j", "DROP"]
    drop_ins = [*MGMT, "iptables", "-I", "FORWARD", "1",
                "-i", "mgh+", "-o", "mgh+", "-j", "DROP"]
    assert drop_del in cmds and drop_ins in cmds
    # DROP insert must come after the subnet ACCEPT inserts (so it lands on top).
    accept_idx = max(i for i, c in enumerate(cmds)
                     if c[:8] == [*MGMT, "iptables", "-I", "FORWARD", "1"]
                     and "ACCEPT" in c)
    assert cmds.index(drop_ins) > accept_idx


def test_create_mgmt_network_distinct_per_range():
    with patch("rangectl.netns._run") as run:
        run.return_value = _ok()
        a = netns.create_mgmt_network("rangectl-lab1", "10.255.1.0/24", "lab1")
        b = netns.create_mgmt_network("rangectl-lab2", "10.255.2.0/24", "lab2")
    # veth names must not collide on the host between two ranges.
    assert a.veth_host != b.veth_host
    assert a.veth_ns != b.veth_ns


# --- destroy_mgmt_network --------------------------------------------------

def test_destroy_mgmt_network_removes_veth_and_iptables():
    mgmt = MgmtNetwork(
        bridge_name="mgmt-br",
        veth_host="mgh12345678",
        veth_ns="mgp12345678",
        host_ip="10.255.1.254",
        subnet="10.255.1.0/24",
    )
    with patch("rangectl.netns._run") as run:
        run.return_value = _ok()
        netns.destroy_mgmt_network(mgmt)
    cmds = _cmds(run)
    # Deleting the mgmt-ns-side veth removes the whole pair.
    assert [*MGMT, "ip", "link", "delete", "mgh12345678"] in cmds
    assert [*MGMT, "iptables", "-D", "FORWARD",
            "-s", "10.255.1.0/24", "-j", "ACCEPT"] in cmds
    assert [*MGMT, "iptables", "-D", "FORWARD",
            "-d", "10.255.1.0/24", "-j", "ACCEPT"] in cmds


# --- create_data_bridge ----------------------------------------------------

def test_create_data_bridge_in_netns():
    with patch("rangectl.netns._run") as run:
        run.return_value = _ok()
        netns.create_data_bridge("rangectl-lab1", "data-0")
    cmds = _cmds(run)
    assert ["ip", "netns", "exec", "rangectl-lab1",
            "ip", "link", "add", "data-0", "type", "bridge"] in cmds
    assert ["ip", "netns", "exec", "rangectl-lab1",
            "ip", "link", "set", "data-0", "up"] in cmds


def test_create_data_bridge_idempotent_on_exists():
    with patch("rangectl.netns._run") as run:
        # add fails with "exists"; should not raise, still brings link up.
        def side_effect(cmd, **kw):
            if "add" in cmd:
                return _ok(rc=2, stderr="RTNETLINK answers: File exists")
            return _ok()
        run.side_effect = side_effect
        netns.create_data_bridge("rangectl-lab1", "data-0")
    cmds = _cmds(run)
    assert ["ip", "netns", "exec", "rangectl-lab1",
            "ip", "link", "set", "data-0", "up"] in cmds


# --- exec_in_netns ---------------------------------------------------------

def test_exec_in_netns_prefixes_command():
    with patch("rangectl.netns._run") as run:
        run.return_value = _ok(stdout="br0\n")
        res = netns.exec_in_netns("rangectl-lab1", ["ip", "link", "show"])
    cmd = run.call_args_list[0].args[0]
    assert cmd == ["ip", "netns", "exec", "rangectl-lab1", "ip", "link", "show"]
    assert res.stdout == "br0\n"
