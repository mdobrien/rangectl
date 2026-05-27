from __future__ import annotations

import pytest

from rangectl import Topology
from rangectl.engine import Engine
from rangectl.types import CycleError


def _waves(backend, db, topology):
    return Engine(backend, db).compute_waves(topology)


def _names(waves):
    return [sorted(n.name for n in wave) for wave in waves]


def test_no_dependencies(backend, db):
    t = Topology("t")
    a = t.node("a", image="u")
    b = t.node("b", image="u")
    c = t.node("c", image="u")
    waves = _waves(backend, db, t)
    assert len(waves) == 1
    assert _names(waves) == [["a", "b", "c"]]


def test_linear_chain(backend, db):
    t = Topology("t")
    a = t.node("a", image="u")
    b = t.node("b", image="u", depends_on=[a])
    c = t.node("c", image="u", depends_on=[b])
    waves = _waves(backend, db, t)
    assert _names(waves) == [["a"], ["b"], ["c"]]


def test_diamond(backend, db):
    t = Topology("t")
    a = t.node("a", image="u")
    b = t.node("b", image="u", depends_on=[a])
    c = t.node("c", image="u", depends_on=[a])
    d = t.node("d", image="u", depends_on=[b, c])
    waves = _waves(backend, db, t)
    assert _names(waves) == [["a"], ["b", "c"], ["d"]]


def test_parallel_no_deps(backend, db):
    t = Topology("t")
    for n in ["a", "b", "c", "d"]:
        t.node(n, image="u")
    waves = _waves(backend, db, t)
    assert len(waves) == 1
    assert _names(waves) == [["a", "b", "c", "d"]]


def test_cycle_detection(backend, db):
    t = Topology("t")
    a = t.node("a", image="u")
    b = t.node("b", image="u", depends_on=[a])
    # introduce a back-edge after the fact
    a.depends_on.append(b)
    with pytest.raises(CycleError):
        _waves(backend, db, t)


def test_self_loop_detection(backend, db):
    t = Topology("t")
    a = t.node("a", image="u")
    a.depends_on.append(a)
    with pytest.raises(CycleError):
        _waves(backend, db, t)
