"""Opt-in env overrides for parallel test isolation (Fix 2 + Fix 3).

RANGECTL_RANGE_PREFIX  -> prepended to every Topology name, so two concurrent
                          runs of the same range never share netns/veth/seed
                          paths (all keyed on the range name).
RANGECTL_STATE_ROOT    -> overrides the overlay/seed root, so disk artifacts
                          don't collide across concurrent runs.

Both are empty/unset by default — no behavior change outside a parallel runner.
See scratch/issues/20260602-1-parallel-test-exploration.md.
"""
from __future__ import annotations

import importlib

from rangectl.topology import Topology


def test_range_prefix_default_is_noop(monkeypatch):
    monkeypatch.delenv("RANGECTL_RANGE_PREFIX", raising=False)
    assert Topology("topo1").name == "topo1"


def test_range_prefix_applied(monkeypatch):
    monkeypatch.setenv("RANGECTL_RANGE_PREFIX", "w3-")
    assert Topology("topo1").name == "w3-topo1"


def test_state_root_override(monkeypatch, tmp_path):
    monkeypatch.setenv("RANGECTL_STATE_ROOT", str(tmp_path / "worker3"))
    import rangectl.engine as engine_mod
    assert engine_mod._state_root() == tmp_path / "worker3"


def test_state_root_default_unaffected(monkeypatch):
    monkeypatch.delenv("RANGECTL_STATE_ROOT", raising=False)
    import rangectl.engine as engine_mod
    root = engine_mod._state_root()
    # One of the two production defaults, never an override path.
    assert root.name == "rangectl" or root.name == ".rangectl"
