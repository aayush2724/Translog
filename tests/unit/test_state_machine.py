"""The transition table and its enforcement."""

from __future__ import annotations

import pytest

from translog_quote.domain.workflow import TERMINAL_STATES, TRANSITIONS, RequestState
from translog_quote.errors import IllegalTransition
from translog_quote.pipeline import StateMachine


@pytest.fixture
def machine() -> StateMachine:
    return StateMachine()


def test_every_state_appears_in_the_transition_table() -> None:
    assert set(TRANSITIONS) == set(RequestState)


def test_every_transition_target_is_a_real_state() -> None:
    for source, targets in TRANSITIONS.items():
        for target in targets:
            assert isinstance(target, RequestState), f"{source} -> {target}"


def test_terminal_states_have_no_exits() -> None:
    for state in TERMINAL_STATES:
        assert TRANSITIONS[state] == frozenset()


def test_pending_approval_has_no_automatic_exit(machine: StateMachine) -> None:
    """BR-11: no timer, no default, no elapsed-time path to sending."""
    assert TRANSITIONS[RequestState.PENDING_APPROVAL] == frozenset(
        {RequestState.QUOTATION_SENT, RequestState.MAKER_REJECTED}
    )


def test_clarification_loop_returns_to_extracted(machine: StateMachine) -> None:
    """The one loop in scope, traversable any number of times."""
    assert machine.can_transition(RequestState.CLARIFICATION_SENT, RequestState.EXTRACTED)


def test_illegal_transition_raises(machine: StateMachine) -> None:
    with pytest.raises(IllegalTransition) as excinfo:
        machine.assert_transition(RequestState.RECEIVED, RequestState.QUOTATION_SENT)

    assert "not permitted" in str(excinfo.value)


def test_terminal_state_cannot_be_left(machine: StateMachine) -> None:
    with pytest.raises(IllegalTransition):
        machine.assert_transition(RequestState.ACCEPTED, RequestState.QUOTATION_SENT)


def test_is_terminal(machine: StateMachine) -> None:
    assert machine.is_terminal(RequestState.DECLINED)
    assert not machine.is_terminal(RequestState.PENDING_APPROVAL)
