"""States and the transition table.

The table is data, and it is the authority on what may happen. Code that disagrees
with it is wrong. Enforcement lives in `pipeline.state_machine`; the vocabulary
lives here because states are domain language, not orchestration detail.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from translog_quote.domain.shipment import ShipmentRecord


class RequestState(StrEnum):
    """Twelve states (docs/architecture.md §10). Six are terminal."""

    RECEIVED = "received"
    EXTRACTED = "extracted"
    NEEDS_INFO = "needs_info"
    CLARIFICATION_SENT = "clarification_sent"
    VALIDATED = "validated"
    RATE_SELECTED = "rate_selected"
    PENDING_APPROVAL = "pending_approval"
    QUOTATION_SENT = "quotation_sent"

    # Terminal
    ACCEPTED = "accepted"
    DECLINED = "declined"
    NO_ELIGIBLE_RATE = "no_eligible_rate"
    MAKER_REJECTED = "maker_rejected"
    FAILED = "failed"
    MANUAL_REVIEW = "manual_review"


TERMINAL_STATES: frozenset[RequestState] = frozenset(
    {
        RequestState.ACCEPTED,
        RequestState.DECLINED,
        RequestState.NO_ELIGIBLE_RATE,
        RequestState.MAKER_REJECTED,
        RequestState.FAILED,
        RequestState.MANUAL_REVIEW,
    }
)


TRANSITIONS: dict[RequestState, frozenset[RequestState]] = {
    RequestState.RECEIVED: frozenset({RequestState.EXTRACTED, RequestState.FAILED}),
    RequestState.EXTRACTED: frozenset(
        {RequestState.VALIDATED, RequestState.NEEDS_INFO, RequestState.FAILED}
    ),
    RequestState.NEEDS_INFO: frozenset({RequestState.CLARIFICATION_SENT}),
    # The one loop in scope. A request may traverse it any number of times while
    # information is still missing.
    RequestState.CLARIFICATION_SENT: frozenset(
        {RequestState.EXTRACTED, RequestState.MANUAL_REVIEW}
    ),
    RequestState.VALIDATED: frozenset(
        {
            RequestState.RATE_SELECTED,
            RequestState.NO_ELIGIBLE_RATE,
            RequestState.FAILED,
        }
    ),
    RequestState.RATE_SELECTED: frozenset({RequestState.PENDING_APPROVAL}),
    # No automatic exit. No timer, no default, no retry escalation. Both
    # transitions out require an explicit ApprovalDecision (BR-11).
    RequestState.PENDING_APPROVAL: frozenset(
        {RequestState.QUOTATION_SENT, RequestState.MAKER_REJECTED}
    ),
    RequestState.QUOTATION_SENT: frozenset(
        {RequestState.ACCEPTED, RequestState.DECLINED, RequestState.MANUAL_REVIEW}
    ),
    # DECLINED is terminal pending AMB-4. If the specification's next-best loop is
    # confirmed, it becomes one edge DECLINED -> RATE_SELECTED with an exclusion
    # set and a repeat cap. Nothing else changes.
    RequestState.ACCEPTED: frozenset(),
    RequestState.DECLINED: frozenset(),
    RequestState.NO_ELIGIBLE_RATE: frozenset(),
    RequestState.MAKER_REJECTED: frozenset(),
    RequestState.FAILED: frozenset(),
    RequestState.MANUAL_REVIEW: frozenset(),
}


class QuotationRequest(BaseModel):
    """One request travelling through the workflow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str
    state: RequestState
    record: ShipmentRecord
    client_address: str
