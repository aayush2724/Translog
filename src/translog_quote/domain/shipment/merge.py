"""Merging a new extraction into an existing shipment record (BR-8).

    INITIAL EMAIL -> PARTIAL SHIPMENT -> CLARIFICATION REPLY
        -> NEW EXTRACTION -> MERGE INTO EXISTING SHIPMENT

A reply never starts a new shipment. Thread correlation (``domain.conversation``)
has already decided, before this module runs, that an incoming email belongs to
an existing ``request_id`` — merging only combines the data once that identity
question is settled, and it preserves ``request_id`` and ``source`` by construction
(they are never touched by the field loop below).

Three rules, applied independently per field:

    1. existing is missing, new is present   -> fill it.        (changed)
    2. existing and new are both present and equal -> keep it.  (unchanged)
    3. existing and new are both present and differ -> conflict. Neither value is
       chosen. The merged record keeps the existing value untouched — recording a
       conflict is not the same as resolving one, and no resolution policy is
       invented here (that is a human or a later, explicit decision).

A fourth case is not one of the three business rules but follows directly from
BR-8: if the new extraction says nothing about a field (``None``), the existing
value is carried forward as-is, whether that value is known or still missing.
Nothing is reported for it — reporting every untouched field on every merge would
bury the two or three fields that actually changed.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from translog_quote.domain.shipment.model import (
    ExtractedFields,
    FieldName,
    RequestSource,
    ShipmentRecord,
)
from translog_quote.domain.shipment.normalize import normalize_extracted_fields

FieldValue = object
"""A shipment field's value. Left loosely typed deliberately: the eleven fields
span ``str``, ``float``, ``int``, ``bool`` and two value types, and no single
narrower alias reads better than the field-specific types already declared on
``ShipmentRecord`` and ``ExtractedFields`` themselves.
"""


class FieldConflict(BaseModel):
    """One field where the existing and incoming values disagree.

    Both values are kept so a later, explicit handling layer (not built here) has
    what it needs to resolve the conflict. This type carries no opinion about
    which value is right.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    field: FieldName
    existing_value: FieldValue
    new_value: FieldValue


class MergeResult(BaseModel):
    """What happened when an extraction was merged into a shipment record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record: ShipmentRecord
    changed: tuple[FieldName, ...] = ()
    """Fields that went from missing to present."""

    unchanged: tuple[FieldName, ...] = ()
    """Fields where the new extraction confirmed the existing value exactly."""

    conflicts: tuple[FieldConflict, ...] = ()

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)


def merge_shipment(existing: ShipmentRecord, incoming: ExtractedFields) -> MergeResult:
    """Merge a new extraction into an existing record. Never raises.

    ``incoming`` is normalized before comparison, so whitespace differences never
    register as a change or a conflict.
    """
    incoming = normalize_extracted_fields(incoming)

    updates: dict[str, object] = {}
    changed: list[FieldName] = []
    unchanged: list[FieldName] = []
    conflicts: list[FieldConflict] = []

    for field in FieldName:
        existing_value = getattr(existing, field.value)
        new_value = getattr(incoming, field.value)

        if new_value is None:
            continue  # nothing stated; existing value (known or not) carries over

        if existing_value is None:
            updates[field.value] = new_value
            changed.append(field)
        elif existing_value == new_value:
            unchanged.append(field)
        else:
            conflicts.append(
                FieldConflict(field=field, existing_value=existing_value, new_value=new_value)
            )
            # Rule 3: neither value is chosen. The field is left out of `updates`,
            # so the existing value passes through `model_copy` unchanged.

    merged_record = existing.model_copy(update=updates) if updates else existing

    return MergeResult(
        record=merged_record,
        changed=tuple(changed),
        unchanged=tuple(unchanged),
        conflicts=tuple(conflicts),
    )


def build_initial_record(
    request_id: str, source: RequestSource, fields: ExtractedFields
) -> ShipmentRecord:
    """The first canonical record for a request, from its first extraction.

    Defined as a merge into a blank record: every field the extraction states is
    "filled", by the same Rule 1 that later replies use, so a request's first and
    every subsequent email are handled by one code path.
    """
    blank = ShipmentRecord(request_id=request_id, source=source)
    return merge_shipment(blank, fields).record
