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
        {
            RequestState.VALIDATED,
            RequestState.NEEDS_INFO,
            RequestState.FAILED,
            # A required field the client has explicitly *denied* leaves nothing
            # to ask and nothing the record can take. There is no automated way
            # forward, so it is handed to a person rather than parked silently at
            # EXTRACTED (the ``is_stuck`` dead-end this edge closes).
            RequestState.MANUAL_REVIEW,
        }
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
            # A stated place that cannot be resolved to an airport without
            # guessing is only discovered after validation, in the rate-search
            # step — so a validated request can still need a client clarification.
            RequestState.NEEDS_INFO,
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

    operator_goods_type: str | None = None
    """An operator's chosen WebCargo Goods Type label, persisted so a restart
    applies it without asking again. Optional with a ``None`` default so a
    request written before this field existed loads unchanged."""
    operator_goods_type_fingerprint: str | None = None
    """The fingerprint of (commodity, cargo_type, is_chemical) the pick was made
    against; the pick is discarded if the record later changes. Optional/``None``
    for the same backward-load reason."""
    operator_goods_type_by: str | None = None
    """The operator who picked, persisted so the enqueue audit still names them
    after a restart. Optional/``None`` for the same backward-load reason."""
