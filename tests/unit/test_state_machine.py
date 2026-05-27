from __future__ import annotations

import pytest

from rangectl.types import (
    InvalidTransitionError,
    NodeState,
    VALID_TRANSITIONS,
    transition_node_state,
)


VALID_PAIRS = [
    (NodeState.DEFINED, NodeState.PROVISIONING),
    (NodeState.PROVISIONING, NodeState.READY),
    (NodeState.READY, NodeState.LINKED),
    (NodeState.LINKED, NodeState.RUNNING),
    (NodeState.RUNNING, NodeState.DESTROYING),
    (NodeState.DESTROYING, NodeState.DESTROYED),
]


@pytest.mark.parametrize("current,target", VALID_PAIRS)
def test_valid_transitions(current: NodeState, target: NodeState) -> None:
    assert transition_node_state(current, target) is target


def test_invalid_transition_skipping_states() -> None:
    with pytest.raises(InvalidTransitionError):
        transition_node_state(NodeState.DEFINED, NodeState.RUNNING)


def test_invalid_transition_backwards() -> None:
    with pytest.raises(InvalidTransitionError):
        transition_node_state(NodeState.READY, NodeState.DEFINED)


def test_invalid_transition_from_terminal_destroyed() -> None:
    with pytest.raises(InvalidTransitionError):
        transition_node_state(NodeState.DESTROYED, NodeState.RUNNING)


def test_invalid_transition_from_terminal_failed() -> None:
    with pytest.raises(InvalidTransitionError):
        transition_node_state(NodeState.FAILED, NodeState.RUNNING)


@pytest.mark.parametrize(
    "current",
    [
        NodeState.DEFINED,
        NodeState.PROVISIONING,
        NodeState.READY,
        NodeState.LINKED,
        NodeState.RUNNING,
        NodeState.DESTROYING,
    ],
)
def test_any_non_terminal_to_failed(current: NodeState) -> None:
    assert transition_node_state(current, NodeState.FAILED) is NodeState.FAILED


def test_terminal_states_have_no_outgoing_transitions() -> None:
    assert VALID_TRANSITIONS[NodeState.DESTROYED] == set()
    assert VALID_TRANSITIONS[NodeState.FAILED] == set()
