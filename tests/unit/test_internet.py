"""Unit tests for Phase 12 — rangectl.internet.

Per-range internet policy via iptables NAT chains. All subprocess calls are
mocked; tests assert the exact iptables command sequences and idempotency.
"""
from __future__ import annotations
from unittest.mock import MagicMock, patch

from rangectl import internet


def _ok(stdout: str = "", stderr: str = "", rc: int = 0):
    m = MagicMock()
    m.returncode = rc
    m.stdout = stdout
    m.stderr = stderr
    return m


def _cmds(run) -> list[list[str]]:
    return [c.args[0] for c in run.call_args_list]


# --- naming / detection ----------------------------------------------------

def test_chain_name():
    assert internet.chain_name("my-lab") == "RANGE-my-lab"


def test_detect_outbound_iface_parses_default_route():
    with patch("rangectl.internet._run") as run:
        run.return_value = _ok(
            stdout="default via 172.31.0.1 dev ens5 proto dhcp src 172.31.1.2")
        assert internet.detect_outbound_iface() == "ens5"


def test_detect_outbound_iface_none_when_no_default():
    with patch("rangectl.internet._run") as run:
        run.return_value = _ok(stdout="")
        assert internet.detect_outbound_iface() is None


# --- enable_internet -------------------------------------------------------

def test_enable_internet_creates_chain_and_masquerade():
    with patch("rangectl.internet._run") as run:
        # Every -C check fails so all rules get appended.
        def side_effect(cmd, **kw):
            if "-C" in cmd:
                return _ok(rc=1)
            return _ok()
        run.side_effect = side_effect
        out = internet.enable_internet("lab1", "10.255.1.0/24", "mgh1234",
                                       outbound_iface="ens5")
    assert out == "ens5"
    cmds = _cmds(run)
    # Per-range chain created in the nat table.
    assert ["iptables", "-t", "nat", "-N", "RANGE-lab1"] in cmds
    # MASQUERADE out the host uplink, inside the range's chain.
    assert ["iptables", "-t", "nat", "-A", "RANGE-lab1",
            "-o", "ens5", "-j", "MASQUERADE"] in cmds
    # POSTROUTING jumps to the range chain for the range's subnet.
    assert ["iptables", "-t", "nat", "-A", "POSTROUTING",
            "-s", "10.255.1.0/24", "-j", "RANGE-lab1"] in cmds
    # FORWARD allows the veth choke point both ways.
    assert ["iptables", "-A", "FORWARD", "-i", "mgh1234", "-j", "ACCEPT"] in cmds
    assert ["iptables", "-A", "FORWARD", "-o", "mgh1234", "-m", "state",
            "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT"] in cmds


def test_enable_internet_idempotent_skips_existing_rules():
    with patch("rangectl.internet._run") as run:
        # -C checks succeed → rules already present → no -A appends.
        run.return_value = _ok(rc=0)
        internet.enable_internet("lab1", "10.255.1.0/24", "mgh1234",
                                 outbound_iface="ens5")
    cmds = _cmds(run)
    appends = [c for c in cmds if "-A" in c]
    assert appends == [], f"expected no appends, got {appends}"


def test_enable_internet_autodetects_outbound():
    with patch("rangectl.internet._run") as run:
        def side_effect(cmd, **kw):
            if cmd[:2] == ["ip", "-o"]:
                return _ok(stdout="default via 172.31.0.1 dev ens5")
            if "-C" in cmd:
                return _ok(rc=1)
            return _ok()
        run.side_effect = side_effect
        out = internet.enable_internet("lab1", "10.255.1.0/24", "mgh1234")
    assert out == "ens5"


def test_enable_internet_raises_without_outbound():
    with patch("rangectl.internet._run") as run:
        run.return_value = _ok(stdout="")  # no default route
        try:
            internet.enable_internet("lab1", "10.255.1.0/24", "mgh1234")
            assert False, "expected RuntimeError"
        except RuntimeError as exc:
            assert "no default-route" in str(exc)


# --- disable_internet ------------------------------------------------------

def test_disable_internet_removes_only_range_rules():
    with patch("rangectl.internet._run") as run:
        run.return_value = _ok()
        internet.disable_internet("lab1", "10.255.1.0/24", "mgh1234")
    cmds = _cmds(run)
    assert ["iptables", "-D", "FORWARD", "-i", "mgh1234", "-j", "ACCEPT"] in cmds
    assert ["iptables", "-D", "FORWARD", "-o", "mgh1234", "-m", "state",
            "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT"] in cmds
    assert ["iptables", "-t", "nat", "-D", "POSTROUTING",
            "-s", "10.255.1.0/24", "-j", "RANGE-lab1"] in cmds
    # Chain flushed and deleted (only this range's).
    assert ["iptables", "-t", "nat", "-F", "RANGE-lab1"] in cmds
    assert ["iptables", "-t", "nat", "-X", "RANGE-lab1"] in cmds
