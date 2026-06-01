"""Phase 13 integration — persistent ranges (Gate 2).

Deploy a range in one "session", drop the handles, then reconnect via
Range.connect() and drive it: exec over SSH, list/status, snapshot/restore, and
finally destroy through the reconnected handle. Validates that a range survives
the process that created it and is fully controllable from a fresh handle.

Requires libvirt + KVM (EC2). Deploys in namespace mode (per-range libvirtd +
netns), so it must run as root.
"""
from __future__ import annotations
import logging

import pytest

from rangectl import Range
from rangectl.libvirt_backend import LibvirtBackend
from rangectl.state import StateDB
from rangectl.topology import Topology
from tests.integration.conftest import pytestmark_skip

log = logging.getLogger(__name__)
pytestmark = pytestmark_skip

RANGE_NAME = "persist"


def _topo(backend: LibvirtBackend, db: StateDB) -> Topology:
    t = Topology(RANGE_NAME, backend=backend, db=db)
    a = t.node("a", image="ubuntu-22.04", vcpu=1, memory=1024)
    b = t.node("b", image="ubuntu-22.04", vcpu=1, memory=1024)
    t.link(a.eth1["10.0.1.1/24"], b.eth1["10.0.1.2/24"])
    return t


def test_persistent_reconnect_lifecycle(backend, db):
    db_path = db._path

    # --- Session 1: deploy, confirm it works, then drop the handles. ---
    topo = _topo(backend, db)
    rng = topo.deploy(use_namespaces=True)  # NOT a context manager — must persist
    try:
        assert rng["a"].mgmt_ip and rng["b"].mgmt_ip
        assert rng["a"].exec("hostname").exit_code == 0
        log.info("Session 1 deployed: a=%s b=%s",
                 rng["a"].mgmt_ip, rng["b"].mgmt_ip)
        # Drop the in-process engine/backends without tearing anything down.
        del rng

        # --- Session 2: reconnect from persisted state only. ---
        rng2 = Range.connect(RANGE_NAME, db_path=db_path)
        assert set(rng2._nodes) == {"a", "b"}

        # SSH must work on the reconnected handle.
        r = rng2["a"].exec("hostname")
        assert r.exit_code == 0, f"exec on reconnected node failed: {r.stderr}"
        assert f"{RANGE_NAME}-a" in r.stdout

        # The link still carries traffic.
        ping = rng2["a"].exec("ping -c 3 -W 2 10.0.1.2")
        assert ping.exit_code == 0, f"ping a->b failed: {ping.stdout}{ping.stderr}"

        # list() discovers it as running.
        listed = {row["name"]: row for row in Range.list(db_path=db_path)}
        assert RANGE_NAME in listed
        assert listed[RANGE_NAME]["status"] == "running"
        assert listed[RANGE_NAME]["node_count"] == 2

        # upload + snapshot/restore round-trip on the reconnected range.
        rng2["a"].exec("echo baseline > /tmp/marker")
        rng2.snapshot("baseline")
        rng2["a"].exec("echo mutated > /tmp/marker")
        rng2.restore("baseline")
        marker = rng2["a"].exec("cat /tmp/marker")
        assert "baseline" in marker.stdout, f"restore failed: {marker.stdout!r}"

        # --- Teardown via the reconnected handle. ---
        rng2.destroy()
        after = {row["name"] for row in Range.list(db_path=db_path)}
        assert RANGE_NAME not in after
    finally:
        # Belt-and-suspenders: ensure no range leaks if an assertion failed
        # before destroy().
        try:
            Range.connect(RANGE_NAME, db_path=db_path).destroy()
        except Exception:
            try:
                Range.cleanup(RANGE_NAME, db_path=db_path)
            except Exception:
                pass


def test_connect_stale_raises(db):
    """Connecting to a range that was never deployed raises RangeNotRunning."""
    from rangectl import RangeNotRunning
    with pytest.raises(RangeNotRunning):
        Range.connect("does-not-exist", db_path=db._path)
