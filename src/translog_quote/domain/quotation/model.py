"""Quotation types.

The approval gate is modelled as a halt, not a callback and not a timeout. There
is no elapsed-time path from PENDING_APPROVAL to QUOTATION_SENT (BR-11): the send
function takes an ``Approved`` as a required argument, so "send without approval"
is not a code path waiting to be tested — it is a signature that cannot be called.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from translog_quote.domain.rates import (
    FilterOutcome,
    RateOptions,
    RatePreference,
    RateStrategy,
    Selection,
)
from translog_quote.domain.shipment import ShipmentRecord
from translog_quote.domain.validation import ValidationResult

#: The longest note an approving person may add to a quotation (to the client or
#: internal). Enforced by the session before anything is recorded or sent.
NOTE_MAX_CHARS = 1000


class ReviewPacket(BaseModel):
    """Everything the quotation maker sees before deciding.

    The extracted record, what was missing, whether a clarification went out, every
    rate with its exclusion reason, and the recommendation with its generated
    reason. Excluded rates are included deliberately — the maker needs to see why a
    carrier is absent.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str
    record: ShipmentRecord
    validation: ValidationResult
    clarification_sent: bool
    rates: FilterOutcome
    selection: Selection

    options: RateOptions | None = None
    """The fastest and the cheapest eligible rate this packet chose between, so
    the review can label the selected rate and its alternative. ``None`` for a
    packet built without options (the scripted demos); the review then keeps
    its single-recommendation layout."""
    selected_option: RateStrategy | None = None
    """Which of ``options`` ``selection`` is."""
    client_preference: RatePreference | None = None
    """The client's stated speed-or-price priority, shown to the approver."""
    client_note: str = ""
    """An optional note the approving person writes to the client. Sent in the
    quotation under "NOTE FROM TRANSLOG", after the figures, exactly as typed —
    never generated. Empty: the quotation is sent as before."""
    internal_note: str = ""
    """An optional note for the approver email and the audit only. Never part
    of anything sent to the client."""


class Approved(BaseModel):
    """The quotation maker approved sending. Required to call send."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    by: str
    at: datetime


class Rejected(BaseModel):
    """The quotation maker declined to send."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    by: str
    at: datetime
    reason: str = ""


ApprovalDecision = Approved | Rejected


class Quotation(BaseModel):
    """What the client receives. Exactly one rate, never a list (BR-10).

    Showing several options would rebuild the manual process this system exists to
    remove.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str
    record: ShipmentRecord
    selection: Selection
    body_text: str
    approved: Approved
