"""What still needs asking, and the message that asks it.

A clarification refers to *unresolved fields*; it never carries a second copy of
shipment data. `ShipmentRecord` remains the only canonical shipment state, and
nothing here writes to it.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from translog_quote.domain.shipment import FieldName


class UnresolvedReason(StrEnum):
    """Why a field still needs the client's input.

    Three different situations that must not collapse into one. They produce
    different questions, and answering them wrongly wastes a round trip:

    - ``MISSING`` — the client has said nothing about this field. Ask for it.
    - ``AMBIGUOUS`` — the client said something we cannot represent, usually a
      measurement in a unit the canonical record does not carry. Ask for it in
      the form we need, and never convert it ourselves.
    - ``CONFLICT`` — two client messages disagree. Ask which is right; never
      pick.
    """

    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    CONFLICT = "conflict"


class UnresolvedField(BaseModel):
    """One field to ask about, why, and the question to ask."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: FieldName
    reason: UnresolvedReason
    question: str
    """Client-facing. No rule identifiers, no internal vocabulary."""

    detail: str = ""
    """Context the question needs — for a conflict, the values that disagree."""


class ClarificationMessage(BaseModel):
    """One message asking for everything outstanding, at once.

    ``unresolved`` is the whole set. There is no single-field constructor, so
    "ask one thing at a time" is not expressible — which is the point. In the
    reference thread this back-and-forth took four separate emails over three
    days.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str
    unresolved: tuple[UnresolvedField, ...]
    subject: str
    body_text: str

    @property
    def asked_for(self) -> tuple[FieldName, ...]:
        return tuple(u.field for u in self.unresolved)

    @property
    def reasons(self) -> frozenset[UnresolvedReason]:
        return frozenset(u.reason for u in self.unresolved)
