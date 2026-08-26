"""Inbound and outbound message types."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class RawEmail(BaseModel):
    """One inbound message, as received. Understood as mail, not as cargo.

    ``in_reply_to`` and ``references`` are RFC 5322 headers and carry the thread
    correlation this system relies on (AMB-11). Subject matching alone would be
    unsafe here: in the reference thread the subject accumulated four concatenated
    titles across three days.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    message_id: str
    from_address: str
    subject: str
    body_text: str
    received_at: datetime
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()


class OutboundMessage(BaseModel):
    """One message we send — a clarification or a quotation.

    Composed deterministically from templates. Outbound copy is customer-facing and
    must be reviewable, diffable and identical across runs, so no model writes it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    to_address: str
    subject: str
    body_text: str
    in_reply_to: str | None = None
