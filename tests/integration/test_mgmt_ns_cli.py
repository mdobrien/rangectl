"""Phase 16c — mgmt-ns CLI + multi-range integration (Gate 2).

Completes the Phase 16 integration matrix on top of 16b's smokes
(``test_mgmt_ns_smoke.py`` — do not duplicate those):

  a) TWO ranges deployed simultaneously share the one persistent mgmt-ns —
     it carries a veth + route for each, the host diff is still only the 4
     static ops, and a staggered destroy leaves the survivor wired + reachable;
  b) ``rangectl mgmt-ns status`` exit codes: all-OK -> 0; a deleted invariant
     piece (host pool route) -> 1 naming the item, healed back by
     ``ensure_mgmt_ns()``; a deleted per-range route -> 1 naming the range;
  c) ``rangectl mgmt-ns reset`` is gated on running ranges without ``--force``,
     and with ``--force`` rebuilds the mgmt-ns and reconnects the live range.

Run on EC2 (KVM + libvirt + root):
    sudo pytest tests/integration/test_mgmt_ns_cli.py -x -v
"""
from __future__ import annotations
import logging
import subprocess

import pytest

from rangectl import cli, mgmt_namespace, subnet_registry
from rangectl.engine import Engine
from rangectl.libvirt_backend import LibvirtBackend
from rangectl.mgmt_namespace import MGMT_NS
from rangectl.netns import _mgmt_veth_names
from rangectl.networking import mgmt_host_ip
from rangectl.state import StateDB
from tests.integration.conftest import pytestmark_skip
from tests.integration.test_mgmt_ns_smoke import _exec_ok, _host_rules, _two_node
from tests.integration.test_ns_integration import _netns_exists, _sweep_ranges
from tests.integration.test_ns_regression import _host_ping

log = logging.getLogger(__name__)
pytestmark = pytestmark_skip


@pytest.fixture(autouse=True)
def cleanup_ranges():
    """Sweep stray ranges after each test; rangectl-mgmt stays (persistent)."""
    yield
    _sweep_ranges()


def _in_mgmt(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["ip", "netns", "exec", MGMT_NS, *cmd],
                          capture_output=True, text=True)


def _mgmt_link_exists(dev: str) -> bool:
    return _in_mgmt(["ip", "-o", "link", "show", dev]).returncode == 0


def _mgmt_route_exists(subnet: str) -> bool:
    r = _in_mgmt(["ip", "route", "show", subnet])
    return r.returncode == 0 and bool(r.stdout.strip())


# --- (a) two concurrent ranges, one mgmt-ns, host untouched -----------------

def test_two_ranges_share_mgmt_ns_host_untouched(
        backend: LibvirtBackend, db: StateDB, capsys):
    # Clean slate so the host diff captures exactly what BOTH deploys add.
    mgmt_namespace.destroy_mgmt_ns()
    _sweep_ranges()
    base = _host_rules()

    engine = Engine(backend, db, use_namespaces=True)
    red, blue = _two_node("clired"), _two_node("cliblue")
    rng_red = engine.deploy(red)
    try:
        rng_blue = engine.deploy(blue)
        try:
            # The ONE mgmt-ns carries veth + route for EACH range.
            subnets = {}
            for name in ("clired", "cliblue"):
                subnets[name] = engine._range_info[name].mgmt_subnet
                veth, _ = _mgmt_veth_names(name)
                assert _mgmt_link_exists(veth), f"{name}: {veth} not in mgmt-ns"
                assert _mgmt_route_exists(subnets[name]), (
                    f"{name}: no mgmt-ns route for {subnets[name]}")
            assert subnets["clired"] != subnets["cliblue"]

            # CLI agrees: all OK, both ranges reported.
            assert cli.main(["mgmt-ns", "status"]) == 0
            out = capsys.readouterr().out
            assert "clired" in out and "cliblue" in out
            assert "MISSING" not in out

            # Host diff is still only the 4 static ops — nothing per-range.
            added = _host_rules() - base
            log.info("host additions:\n%s", "\n".join(sorted(added)))
            for leak in ("mgh", "RANGE-",
                         subnets["clired"], subnets["cliblue"]):
                assert not any(leak in r for r in added), (leak, added)

            assert _exec_ok(rng_red["a"], "hostname").exit_code == 0
            assert _exec_ok(rng_blue["a"], "hostname").exit_code == 0
        finally:
            # Staggered destroy: red goes, blue must stay wired + reachable.
            engine.destroy(red)
            assert not _netns_exists("rangectl-clired")
            assert _netns_exists("rangectl-cliblue")
            veth_blue, _ = _mgmt_veth_names("cliblue")
            assert _mgmt_link_exists(veth_blue), "blue lost its mgmt-ns veth"
            survive = _exec_ok(rng_blue["a"], "hostname")
            assert survive.exit_code == 0, (
                f"blue unreachable after red destroy: {survive.stderr!r}")
            engine.destroy(blue)
    finally:
        if _netns_exists("rangectl-clired"):
            engine.destroy(red)
    assert not _netns_exists("rangectl-cliblue")


# --- (b) status exit codes + heal -------------------------------------------

def test_status_exit_codes_and_heal(backend: LibvirtBackend, db: StateDB,
                                    capsys):
    t = _two_node("clistat")
    engine = Engine(backend, db, use_namespaces=True)
    rng = engine.deploy(t)
    try:
        subnet = engine._range_info["clistat"].mgmt_subnet

        # All OK -> exit 0.
        assert cli.main(["mgmt-ns", "status"]) == 0
        capsys.readouterr()

        # Delete an invariant piece ensure heals: the host pool route.
        aggregate = subnet_registry.pool_aggregate()
        subprocess.run(["ip", "route", "del", aggregate],
                       capture_output=True, check=True)
        assert cli.main(["mgmt-ns", "status"]) == 1
        out = capsys.readouterr().out
        assert any("host pool route" in ln and "MISSING" in ln
                   for ln in out.splitlines()), out

        # status is read-only — the route is still gone until ensure heals it.
        mgmt_namespace.ensure_mgmt_ns()
        assert cli.main(["mgmt-ns", "status"]) == 0
        capsys.readouterr()

        # Delete a per-range piece: the range's route inside the mgmt-ns.
        veth, _ = _mgmt_veth_names("clistat")
        r = _in_mgmt(["ip", "route", "del", subnet])
        assert r.returncode == 0, r.stderr
        assert cli.main(["mgmt-ns", "status"]) == 1
        out = capsys.readouterr().out
        bad = [ln for ln in out.splitlines()
               if "clistat" in ln and "MISSING" in ln]
        assert bad, out

        # Restore the connected route; green again and the VM is reachable.
        r = _in_mgmt(["ip", "route", "add", subnet, "dev", veth,
                      "src", mgmt_host_ip(subnet)])
        assert r.returncode == 0, r.stderr
        assert cli.main(["mgmt-ns", "status"]) == 0
        capsys.readouterr()
        assert _exec_ok(rng["a"], "hostname").exit_code == 0
    finally:
        engine.destroy(t)
    assert not _netns_exists("rangectl-clistat")


# --- (c) reset gating + reset --force with a live range ---------------------

def test_reset_force_with_live_range(backend: LibvirtBackend, db: StateDB,
                                     capsys):
    t = _two_node("clireset")
    engine = Engine(backend, db, use_namespaces=True)
    rng = engine.deploy(t)
    try:
        assert _exec_ok(rng["a"], "hostname").exit_code == 0

        # Gated: a running range blocks reset without --force.
        assert cli.main(["mgmt-ns", "reset"]) == 1
        captured = capsys.readouterr()
        assert "--force" in captured.err and "clireset" in captured.err
        assert _netns_exists(MGMT_NS), "gated reset must not touch the mgmt-ns"

        # --force: rebuild + reconnect the live range.
        assert cli.main(["mgmt-ns", "reset", "--force"]) == 0
        out = capsys.readouterr().out
        assert "clireset" in out, out  # reported as reconnected
        assert _netns_exists(MGMT_NS)

        # Range reachable through the rebuilt hop; invariant fully green.
        r = _exec_ok(rng["a"], "hostname", attempts=4, settle=5)
        assert r.exit_code == 0, f"unreachable after reset: {r.stderr!r}"
        assert "clireset-a" in r.stdout
        assert _host_ping(rng["a"].mgmt_ip), "host->VM broken after reset"
        assert cli.main(["mgmt-ns", "status"]) == 0
        capsys.readouterr()
    finally:
        engine.destroy(t)
    assert not _netns_exists("rangectl-clireset")
