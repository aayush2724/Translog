"""Routing one inbound message to the request it belongs to.

    RawEmail -> CorrelationPolicy -> existing request_id | new request | refuse
             -> ClarificationWorkflow.handle
             -> record the message id on that request's thread

The step that was missing between "an email arrived" and "process it as request
R-123". Until now every caller supplied a ``request_id`` it had invented; this
module is where that identity is *decided*, once, by the policy — and where the
thread is recorded so the next reply can be placed against it.

It orchestrates and stores; it decides nothing. The correlation rule lives in
``domain.conversation``, the merge rule in ``domain.shipment``, and neither is
restated here. What this module owns is the consequence of a refusal: an
ambiguous message is not handed to the workflow at all, so there is no path by
which a message the policy could not place reaches a shipment record.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from translog_quote.domain.conversation import AmbiguousCorrelation, NewRequest, Thread

if TYPE_CHECKING:
    from collections.abc import Callable

    from translog_quote.domain.clarification import ClarificationMessage
    from translog_quote.domain.conversation import CorrelationPolicy
    from translog_quote.domain.email import RawEmail
    from translog_quote.domain.quotation import Approved
    from translog_quote.pipeline.clarification_loop import ClarificationWorkflow, TurnOutcome
    from translog_quote.ports import StorePort


@dataclass(frozen=True, slots=True)
class RoutedMessage:
    """What routing decided about one inbound message, and what came of it.

    ``outcome`` is ``None`` exactly when the message was refused: nothing was
    extracted, nothing was merged, and no request was touched.
    """

    request_id: str | None
    is_reply: bool
    """True when the policy placed this message on an *existing* request."""

    needs_manual_review: bool
    outcome: TurnOutcome | None = None
    reason: str = ""
    """Why a refused message was refused. Empty when it was routed."""

    @property
    def was_refused(self) -> bool:
        return self.outcome is None


class InboundRouter:
    """Correlates inbound mail, then drives the existing clarification loop.

    ``new_request_id`` decides what a first-contact enquiry is called. It is a
    caller's concern, not a domain rule — a demo wants a deterministic id
    derived from the message, and production will want one from its own
    numbering — so it is injected rather than invented here.
    """

    def __init__(
        self,
        *,
        policy: CorrelationPolicy,
        workflow: ClarificationWorkflow,
        store: StorePort,
        new_request_id: Callable[[RawEmail], str],
    ) -> None:
        self._policy = policy
        self._workflow = workflow
        self._store = store
        self._new_request_id = new_request_id

    def route(self, email: RawEmail) -> RoutedMessage:
        """Place one message and process it. Never merges into a maybe."""
        decision = self._policy.correlate(email, self._store.all_threads())

        if isinstance(decision, AmbiguousCorrelation):
            # Deliberately not handed to the workflow. A message that cannot be
            # placed is not extracted, not merged, and not recorded against any
            # thread — recording it would be the same guess, one layer down.
            return RoutedMessage(
                request_id=None,
                is_reply=False,
                needs_manual_review=True,
                reason=(
                    "The reply's headers place it in more than one known request. "
                    "A person must decide which enquiry it answers."
                ),
            )

        is_reply = not isinstance(decision, NewRequest)
        request_id = self._new_request_id(email) if isinstance(decision, NewRequest) else decision

        outcome = self._workflow.handle(request_id, email)
        self._record(request_id, email.message_id)

        return RoutedMessage(
            request_id=request_id,
            is_reply=is_reply,
            needs_manual_review=outcome.needs_a_person,
            outcome=outcome,
        )

    def pending_draft(self, request_id: str) -> ClarificationMessage | None:
        """The draft holding this request at NEEDS_INFO, if there is one."""
        return self._workflow.pending_draft(request_id)

    def approve(self, request_id: str, *, by: str) -> Approved:
        """Release a held draft on a named person's authority.

        A pass-through to the gate rather than a second gate: the router does
        not decide anything about approval, it only saves callers from reaching
        around it into the workflow. `by` stays required all the way down.
        """
        return self._workflow.approve_clarification(request_id, by=by)

    def _record(self, request_id: str, message_id: str) -> None:
        """Append this message to the request's thread.

        The thread is what the next reply correlates against, so it is written
        after the message has been processed rather than before: a message that
        failed to extract has not been seen, and should not silently become a
        correlation anchor.
        """
        existing = next((t for t in self._store.all_threads() if t.request_id == request_id), None)
        known = existing.message_ids if existing else ()
        if message_id in known:
            return  # the same message processed twice adds no new anchor
        self._store.save_thread(Thread(request_id=request_id, message_ids=(*known, message_id)))
