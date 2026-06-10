"""Unit tests for Phase 16b — rangectl.mgmt_namespace.

The persistent management namespace (``rangectl-mgmt``) is verified-and-healed
before every namespace deploy. These tests mock the command runner + the
read-only probes so the heal logic, overlap abort, env overrides, flock guard,
per-range connect/disconnect delegation, and the H5 unconditional-disable fix
are all exercised without touching the kernel.
"""
from __future__ import annotations
import contextlib
import json
from unittest.mock import MagicMock, patch

import pytest

from rangectl import mgmt_namespace as mn
from rangectl.networking import MGMT_NS


def _ok(stdout: str = "", stderr: str = "", rc: int = 0):
    m = MagicMock()
    m.returncode = rc
    m.stdout = stdout
    m.stderr = stderr
    return m


# --- resolve_transit ------------------------------------------------------

def test_resolve_transit_default():
    assert str(mn.resolve_transit()) == "10.254.0.0/30"


def test_resolve_transit_explicit_wins_over_env(monkeypatch):
    monkeypatch.setenv("RANGECTL_MGMT_TRANSIT", "10.99.0.0/30")
    assert str(mn.resolve_transit("10.88.0.0/30")) == "10.88.0.0/30"


def test_resolve_transit_env(monkeypatch):
    monkeypatch.setenv("RANGECTL_MGMT_TRANSIT", "10.77.0.0/29")
    assert str(mn.resolve_transit()) == "10.77.0.0/29"


def test_resolve_transit_invalid_cidr_names_env():
    with pytest.raises(ValueError, match="RANGECTL_MGMT_TRANSIT"):
        mn.resolve_transit("not-a-cidr")


def test_resolve_transit_too_small_prefix():
    # /31 has no room for both a .1 host and a .2 mgmt-ns address.
    with pytest.raises(ValueError, match="at least a /30"):
        mn.resolve_transit("10.254.0.0/31")


def test_transit_ips():
    net = mn.resolve_transit("10.254.0.0/30")
    assert mn._transit_ips(net) == ("10.254.0.1", "10.254.0.2", 30)


# --- ensure_mgmt_ns: create-from-scratch ----------------------------------

@pytest.fixture
def ensure_env(monkeypatch):
    """Patch the command runner + probes; collect every issued command.

    By default the namespace and all invariant pieces are ABSENT so a full
    create path runs. Tests tweak the probe return values to exercise healing.
    """
    cmds: list[list[str]] = []

    def fake_run(cmd, check=True):
        cmds.append(cmd)
        # -C (iptables check) "fails" so rules get added; everything else ok.
        return _ok(rc=1 if "-C" in cmd else 0)

    def fake_exec(cmd, check=True):
        full = ["ip", "netns", "exec", MGMT_NS, *cmd]
        cmds.append(full)
        return _ok(rc=1 if "-C" in cmd else 0)

    monkeypatch.setattr(mn, "_run", fake_run)
    monkeypatch.setattr(mn, "_exec", fake_exec)
    monkeypatch.setattr(mn, "_check_overlap", lambda *a, **k: None)
    monkeypatch.setattr(mn.internet, "detect_outbound_iface", lambda *a, **k: "eth0")
    monkeypatch.setattr(mn, "_reconnect_running_ranges", lambda *a, **k: None)

    flock_used = []

    @contextlib.contextmanager
    def fake_flock():
        flock_used.append(True)
        yield

    monkeypatch.setattr(mn, "_flock", fake_flock)

    # All-absent defaults.
    monkeypatch.setattr(mn, "_ns_exists", lambda: False)
    monkeypatch.setattr(mn, "_link_exists", lambda dev, netns_name=None: False)
    monkeypatch.setattr(mn, "_has_addr",
                        lambda dev, addr, netns_name=None: False)
    monkeypatch.setattr(mn, "_has_route", lambda dest, netns_name=None: False)

    return type("E", (), {"cmds": cmds, "flock_used": flock_used})


def test_ensure_creates_full_invariant_when_absent(ensure_env):
    mn.ensure_mgmt_ns()
    cmds = ensure_env.cmds
    M = ["ip", "netns", "exec", MGMT_NS]
    # Namespace + veth pair created, ns side moved in.
    assert ["ip", "netns", "add", MGMT_NS] in cmds
    assert ["ip", "link", "add", mn.VETH_HOST, "type", "veth",
            "peer", "name", mn.VETH_NS] in cmds
    assert ["ip", "link", "set", mn.VETH_NS, "netns", MGMT_NS] in cmds
    # Host side: address, up, aggregate route.
    assert ["ip", "addr", "add", "10.254.0.1/30", "dev", mn.VETH_HOST] in cmds
    assert ["ip", "route", "replace", "10.255.0.0/16", "via", "10.254.0.2"] in cmds
    # Host iptables: FORWARD ACCEPT + transit MASQUERADE out the uplink.
    assert ["iptables", "-A", "FORWARD", "-i", mn.VETH_HOST, "-j", "ACCEPT"] in cmds
    assert ["iptables", "-t", "nat", "-A", "POSTROUTING", "-s", "10.254.0.0/30",
            "-o", "eth0", "-j", "MASQUERADE"] in cmds
    assert ["sysctl", "-w", "net.ipv4.ip_forward=1"] in cmds
    # Mgmt-ns side: address, default route, lo up, forwarding.
    assert [*M, "ip", "addr", "add", "10.254.0.2/30", "dev", mn.VETH_NS] in cmds
    assert [*M, "ip", "route", "add", "default", "via", "10.254.0.1"] in cmds
    assert [*M, "ip", "link", "set", "lo", "up"] in cmds


def test_ensure_is_flock_guarded(ensure_env):
    mn.ensure_mgmt_ns()
    assert ensure_env.flock_used == [True]


def test_ensure_heals_only_missing_pieces(ensure_env, monkeypatch):
    """When the ns + veth + host addr already exist, ensure must NOT re-create
    them — only the genuinely-missing pieces are issued."""
    monkeypatch.setattr(mn, "_ns_exists", lambda: True)
    monkeypatch.setattr(mn, "_link_exists", lambda dev, netns_name=None: True)
    # Host addr present; mgmt-ns addr absent.
    monkeypatch.setattr(mn, "_has_addr",
                        lambda dev, addr, netns_name=None: netns_name is None)
    monkeypatch.setattr(mn, "_has_route", lambda dest, netns_name=None: True)

    mn.ensure_mgmt_ns()
    cmds = ensure_env.cmds
    # Nothing re-created.
    assert ["ip", "netns", "add", MGMT_NS] not in cmds
    assert ["ip", "link", "add", mn.VETH_HOST, "type", "veth",
            "peer", "name", mn.VETH_NS] not in cmds
    assert ["ip", "addr", "add", "10.254.0.1/30", "dev", mn.VETH_HOST] not in cmds
    # The one missing piece (mgmt-ns address) IS healed.
    assert ["ip", "netns", "exec", MGMT_NS, "ip", "addr", "add",
            "10.254.0.2/30", "dev", mn.VETH_NS] in cmds


def test_ensure_reconnects_running_ranges_when_ns_created(ensure_env, monkeypatch):
    """If the ns was missing, ensure must reconnect every running range."""
    reconnected = []
    monkeypatch.setattr(mn, "_reconnect_running_ranges",
                        lambda range_dir: reconnected.append(range_dir))
    mn.ensure_mgmt_ns()
    assert len(reconnected) == 1


def test_ensure_does_not_reconnect_when_ns_present(ensure_env, monkeypatch):
    monkeypatch.setattr(mn, "_ns_exists", lambda: True)
    reconnected = []
    monkeypatch.setattr(mn, "_reconnect_running_ranges",
                        lambda range_dir: reconnected.append(range_dir))
    mn.ensure_mgmt_ns()
    assert reconnected == []


def test_ensure_env_override_transit(ensure_env, monkeypatch):
    monkeypatch.setenv("RANGECTL_MGMT_TRANSIT", "10.200.0.0/30")
    mn.ensure_mgmt_ns()
    cmds = ensure_env.cmds
    assert ["ip", "addr", "add", "10.200.0.1/30", "dev", mn.VETH_HOST] in cmds
    assert ["ip", "route", "replace", "10.255.0.0/16", "via", "10.200.0.2"] in cmds


# --- overlap abort (D3b) --------------------------------------------------

def test_overlap_abort_on_foreign_route(monkeypatch):
    transit = mn.resolve_transit("10.254.0.0/30")
    aggregate = __import__("ipaddress").IPv4Network("10.255.0.0/16")
    monkeypatch.setattr(mn, "_parse_addrs", lambda: [])
    monkeypatch.setattr(mn, "_parse_routes",
                        lambda: [("10.255.5.0/24", "eth9")])
    with pytest.raises(RuntimeError) as exc:
        mn._check_overlap(transit, aggregate)
    msg = str(exc.value)
    assert "10.255.5.0/24" in msg
    assert "RANGECTL_MGMT_TRANSIT" in msg and "RANGECTL_MGMT_POOL" in msg


def test_overlap_abort_on_foreign_address(monkeypatch):
    transit = mn.resolve_transit("10.254.0.0/30")
    aggregate = __import__("ipaddress").IPv4Network("10.255.0.0/16")
    monkeypatch.setattr(mn, "_parse_addrs",
                        lambda: [("10.254.0.1/30", "wg0")])
    monkeypatch.setattr(mn, "_parse_routes", lambda: [])
    with pytest.raises(RuntimeError, match="wg0"):
        mn._check_overlap(transit, aggregate)


def test_overlap_ignores_rangectls_own_veth(monkeypatch):
    transit = mn.resolve_transit("10.254.0.0/30")
    aggregate = __import__("ipaddress").IPv4Network("10.255.0.0/16")
    # Our own transit address on veth-mgmt-host must NOT trip the check.
    monkeypatch.setattr(mn, "_parse_addrs",
                        lambda: [("10.254.0.1/30", mn.VETH_HOST)])
    monkeypatch.setattr(mn, "_parse_routes",
                        lambda: [("10.255.0.0/16", mn.VETH_HOST)])
    mn._check_overlap(transit, aggregate)  # no raise


def test_overlap_ignores_aggregate_route_we_install(monkeypatch):
    transit = mn.resolve_transit("10.254.0.0/30")
    aggregate = __import__("ipaddress").IPv4Network("10.255.0.0/16")
    # The exact aggregate route via the mgmt-ns is ours, on whatever dev.
    monkeypatch.setattr(mn, "_parse_addrs", lambda: [])
    monkeypatch.setattr(mn, "_parse_routes",
                        lambda: [("10.255.0.0/16", "")])
    mn._check_overlap(transit, aggregate)  # no raise


def test_overlap_no_conflict_when_disjoint(monkeypatch):
    transit = mn.resolve_transit("10.254.0.0/30")
    aggregate = __import__("ipaddress").IPv4Network("10.255.0.0/16")
    monkeypatch.setattr(mn, "_parse_addrs",
                        lambda: [("192.168.1.5/24", "eth0")])
    monkeypatch.setattr(mn, "_parse_routes",
                        lambda: [("172.16.0.0/12", "eth0")])
    mn._check_overlap(transit, aggregate)  # no raise


# --- connect / disconnect -------------------------------------------------

def test_connect_range_delegates_to_netns():
    with patch("rangectl.mgmt_namespace.netns.create_mgmt_network") as cmn:
        cmn.return_value = "mgmt-obj"
        out = mn.connect_range("lab1", "10.255.1.0/24")
    cmn.assert_called_once_with("rangectl-lab1", "10.255.1.0/24", "lab1")
    assert out == "mgmt-obj"


def test_disconnect_range_always_disables_internet():
    """H5: disconnect_range disables internet UNCONDITIONALLY (idempotent), so a
    runtime-enabled range can't leak a stale NAT jump into the mgmt-ns."""
    with patch("rangectl.mgmt_namespace.internet.disable_internet") as dis, \
         patch("rangectl.mgmt_namespace.netns.destroy_mgmt_network") as dmn:
        mn.disconnect_range("lab1", "10.255.1.0/24", "mgh1234",
                            veth_ns="mgp1234", host_ip="10.255.1.254")
    dis.assert_called_once_with("lab1", "10.255.1.0/24", "mgh1234", netns=MGMT_NS)
    dmn.assert_called_once()
    handle = dmn.call_args.args[0]
    assert handle.veth_host == "mgh1234"
    assert handle.subnet == "10.255.1.0/24"


# --- reconnect running ranges ---------------------------------------------

def test_reconnect_running_ranges(tmp_path, monkeypatch):
    # Two ranges with state files; one's netns is gone (must be skipped).
    for name, subnet in (("alive", "10.255.1.0/24"), ("gone", "10.255.2.0/24")):
        d = tmp_path / name
        d.mkdir()
        (d / "range.json").write_text(json.dumps({
            "netns_name": f"rangectl-{name}", "subnet": subnet,
        }))
    monkeypatch.setattr(mn, "_live_netns",
                        lambda n: n == "rangectl-alive")
    connected = []
    monkeypatch.setattr(mn, "connect_range",
                        lambda name, subnet: connected.append((name, subnet)))
    mn._reconnect_running_ranges(str(tmp_path))
    assert connected == [("alive", "10.255.1.0/24")]


def test_reconnect_running_ranges_missing_dir(monkeypatch):
    # No /ranges dir → no-op, no raise.
    connected = []
    monkeypatch.setattr(mn, "connect_range",
                        lambda name, subnet: connected.append(name))
    mn._reconnect_running_ranges("/nonexistent/ranges/dir")
    assert connected == []


# --- destroy / status -----------------------------------------------------

def test_destroy_mgmt_ns_removes_ns_veth_route():
    with patch("rangectl.mgmt_namespace._run") as run:
        run.return_value = _ok()
        mn.destroy_mgmt_ns()
    cmds = [c.args[0] for c in run.call_args_list]
    assert ["ip", "netns", "del", MGMT_NS] in cmds
    assert ["ip", "link", "del", mn.VETH_HOST] in cmds


def test_status_reports_invariant(monkeypatch):
    monkeypatch.setattr(mn, "_ns_exists", lambda: True)
    monkeypatch.setattr(mn, "_link_up", lambda dev, netns_name=None: True)
    monkeypatch.setattr(mn, "_has_addr", lambda dev, addr, netns_name=None: True)
    monkeypatch.setattr(mn, "_has_route", lambda dest, netns_name=None: True)
    monkeypatch.setattr(mn, "_iptables_present",
                        lambda args, netns_name=None: True)
    monkeypatch.setattr(mn, "_ip_forward", lambda netns_name=None: True)
    st = mn.status()
    assert st["namespace"] == MGMT_NS
    assert st["transit"] == "10.254.0.0/30"
    assert st["aggregate"] == "10.255.0.0/16"
    assert st["ns_exists"] is True
    assert all(st[k] is True for k in (
        "veth_host_up", "veth_ns_up", "host_addr", "host_route",
        "host_forward", "host_ip_forward", "ns_addr", "ns_default_route",
        "ns_ip_forward"))


def test_status_when_ns_absent(monkeypatch):
    monkeypatch.setattr(mn, "_ns_exists", lambda: False)
    monkeypatch.setattr(mn, "_link_up", lambda dev, netns_name=None: False)
    monkeypatch.setattr(mn, "_has_addr", lambda dev, addr, netns_name=None: False)
    monkeypatch.setattr(mn, "_has_route", lambda dest, netns_name=None: False)
    monkeypatch.setattr(mn, "_iptables_present",
                        lambda args, netns_name=None: False)
    monkeypatch.setattr(mn, "_ip_forward", lambda netns_name=None: False)
    st = mn.status()
    assert st["ns_exists"] is False
    # Namespace-internal checks short-circuit to False (no exec into a dead ns).
    assert st["ns_addr"] is False and st["ns_ip_forward"] is False
