"""Transition enforcement.

The transition table lives in `domain.workflow` because states are domain
vocabulary. This module is the engine that refuses to disagree with it.
"""

from __future__ import annotations

from translog_quote.domain.workflow import TERMINAL_STATES, TRANSITIONS, RequestState
from translog_quote.errors import IllegalTransition


class StateMachine:
    """Guards every state change.

    Illegal transitions raise rather than silently no-op: an attempt to move
    between two states not in the table is a programming error, and a request that
    quietly stays put is far harder to diagnose than one that stops.
    """

    def can_transition(self, current: RequestState, target: RequestState) -> bool:
        return target in TRANSITIONS[current]

    def assert_transition(self, current: RequestState, target: RequestState) -> None:
        if not self.can_transition(current, target):
            allowed = sorted(s.value for s in TRANSITIONS[current])
            permitted = ", ".join(allowed) if allowed else "none (terminal state)"
            raise IllegalTransition(
                f"{current.value} -> {target.value} is not permitted. "
                f"Allowed from {current.value}: {permitted}."
            )

    @staticmethod
    def is_terminal(state: RequestState) -> bool:
        return state in TERMINAL_STATES
