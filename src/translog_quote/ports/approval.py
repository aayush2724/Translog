"""The human approval boundary."""

from __future__ import annotations

from typing import Protocol

from translog_quote.domain.quotation import ApprovalDecision, ReviewPacket


class ApprovalPort(Protocol):
    """Where the quotation maker decides.

    This is a halt, not a callback. The pipeline enters PENDING_APPROVAL and stops;
    resuming requires an explicit decision. No implementation may return a default
    on a timeout (BR-11).
    """

    def request(self, review: ReviewPacket) -> ApprovalDecision: ...


class DeferredApprovalPort(ApprovalPort, Protocol):
    """An approval gate whose decision arrives before the halt is resolved.

    A terminal gate blocks *inside* ``request``. A gate driven by a user
    interface cannot: the decision arrives on one HTTP request and the pipeline
    runs on that same request, so the decision has to be placed and then
    consumed. This port names that capability rather than leaving callers to
    reach for a concrete class.

    It widens ``ApprovalPort``; it does not weaken it. ``request`` must still
    never return a default — an implementation consulted with nothing recorded
    is required to raise, because "nobody decided" and "somebody declined" are
    different facts and only one of them belongs in an audit trail.
    """

    def record(self, decision: ApprovalDecision) -> None: ...
