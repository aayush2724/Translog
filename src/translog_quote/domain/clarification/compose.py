"""Deciding what is still unresolved, and writing the one message that asks.

Wholly deterministic. No model is consulted here: the validator already knows
which fields are missing, the extraction result already knows which were
unrepresentable, and the merge already knows which disagree. Asking a model any
of that would replace three certain answers with one uncertain one.

Nothing in this module can invent a shipment value. It reads three inputs and
emits questions; it has no write access to `ShipmentRecord` and no way to
produce a field value at all.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from translog_quote.domain.clarification.model import (
    ClarificationMessage,
    UnresolvedField,
    UnresolvedReason,
)
from translog_quote.domain.clarification.questions import (
    ambiguous_question,
    conflict_question,
    missing_question,
)
from translog_quote.domain.extraction import ExtractionResult, FieldStatus
from translog_quote.domain.shipment import FieldConflict, FieldName
from translog_quote.domain.validation import ValidationResult, ValidationSeverity

_BLOCKING = frozenset({ValidationSeverity.MISSING, ValidationSeverity.INVALID})

DEFAULT_SUBJECT = "Additional details needed for your rate request"

_OPENING = "Thank you for your enquiry."
_ASK_LEAD = "To prepare an accurate quotation, could you please confirm the following:"
_CONFLICT_LEAD = (
    "We have also received two different values for the following. "
    "Could you please confirm which is correct:"
)
_CLOSING = "Once we have these details we will come back to you with the rate."
_SIGN_OFF = "Kind regards,\nTranslog Express"


class UnresolvedAnalysis(BaseModel):
    """What still stands between this shipment and a quotation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unresolved: tuple[UnresolvedField, ...] = ()
    blocked_by_denial: tuple[FieldName, ...] = ()
    """Required fields the client has explicitly said they cannot supply.

    Asking again would be rude and useless — they answered. These need a person,
    not another round trip, so the workflow routes them to manual review rather
    than looping.
    """

    @property
    def needs_clarification(self) -> bool:
        return bool(self.unresolved)

    @property
    def is_stuck(self) -> bool:
        """Nothing left to ask, but the shipment still is not usable."""
        return not self.unresolved and bool(self.blocked_by_denial)


def identify_unresolved(
    validation: ValidationResult,
    extraction: ExtractionResult | None = None,
    conflicts: tuple[FieldConflict, ...] = (),
) -> UnresolvedAnalysis:
    """Work out what to ask, from what deterministic code already knows.

    ``validation`` is the authority on what is required and absent — which is
    what keeps the system from over-asking. A field the client has already
    given never appears in a `ValidationResult`, so it can never reach a
    question.

    ``extraction`` refines *why* a field is absent. The canonical record cannot
    tell "never mentioned" from "said, but in centimetres" — both are null — so
    the latest extraction is consulted to pick the right question. Passing it is
    optional; without it every gap is treated as simply missing, which is the
    safe reading.

    ``conflicts`` come from the merge and are invisible to validation: a
    conflicting merge leaves the existing value in place, so the record still
    validates. They are added here because a shipment that validates on a value
    two messages disagree about is not one to quote.
    """
    conflicted = {c.field for c in conflicts}
    unresolved: list[UnresolvedField] = []
    denied: list[FieldName] = []

    for issue in validation.issues:
        if issue.severity not in _BLOCKING or issue.field in conflicted:
            continue

        status = _status_of(extraction, issue.field)

        if status is FieldStatus.DENIED:
            denied.append(issue.field)
            continue

        if status is FieldStatus.AMBIGUOUS:
            unresolved.append(
                UnresolvedField(
                    field=issue.field,
                    reason=UnresolvedReason.AMBIGUOUS,
                    question=ambiguous_question(issue.field),
                    detail=_ambiguity_detail(extraction, issue.field),
                )
            )
            continue

        unresolved.append(
            UnresolvedField(
                field=issue.field,
                reason=UnresolvedReason.MISSING,
                question=missing_question(issue.field),
            )
        )

    for conflict in conflicts:
        unresolved.append(
            UnresolvedField(
                field=conflict.field,
                reason=UnresolvedReason.CONFLICT,
                question=conflict_question(
                    conflict.field, conflict.existing_value, conflict.new_value
                ),
                detail=f"{conflict.existing_value} vs {conflict.new_value}",
            )
        )

    # Canonical field order, so two runs over the same state read identically.
    order = list(FieldName)
    unresolved.sort(key=lambda u: (order.index(u.field), u.reason.value))

    return UnresolvedAnalysis(unresolved=tuple(unresolved), blocked_by_denial=tuple(denied))


def _status_of(extraction: ExtractionResult | None, field: FieldName) -> FieldStatus | None:
    if extraction is None:
        return None
    value = getattr(extraction, field.value, None)
    return None if value is None else value.status


def _ambiguity_detail(extraction: ExtractionResult | None, field: FieldName) -> str:
    if extraction is None:
        return ""
    value = getattr(extraction, field.value, None)
    return (value.note or "") if value is not None else ""


def compose_clarification(
    request_id: str,
    analysis: UnresolvedAnalysis,
    *,
    subject: str = DEFAULT_SUBJECT,
) -> ClarificationMessage | None:
    """One message covering everything outstanding. ``None`` when nothing is.

    Conflicts are asked in their own paragraph. Mixed into a list of things we
    are missing, "we have two weights" reads as though we lost one.
    """
    if not analysis.unresolved:
        return None

    asks = [u for u in analysis.unresolved if u.reason is not UnresolvedReason.CONFLICT]
    clashes = [u for u in analysis.unresolved if u.reason is UnresolvedReason.CONFLICT]

    lines: list[str] = [_OPENING, ""]
    number = 1

    if asks:
        lines.append(_ASK_LEAD)
        lines.append("")
        for item in asks:
            lines.append(f"  {number}. {item.question}")
            number += 1
        lines.append("")

    if clashes:
        lines.append(_CONFLICT_LEAD)
        lines.append("")
        for item in clashes:
            lines.append(f"  {number}. {item.question}")
            number += 1
        lines.append("")

    lines.extend([_CLOSING, "", _SIGN_OFF])

    return ClarificationMessage(
        request_id=request_id,
        unresolved=analysis.unresolved,
        subject=subject,
        body_text="\n".join(lines),
    )
