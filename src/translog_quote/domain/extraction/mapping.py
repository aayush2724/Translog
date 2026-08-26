"""Mapping an extraction result into the canonical record's field shape.

Structural conversion only. This function performs no validation, applies no
business rule, and reaches no conclusion — it narrows a four-state-per-field
extraction into the canonical two-state (known / null) shape and stops there.

In particular it does **not** notice that ``is_chemical=True`` arrived without
an MSDS. That observation belongs to `domain.validation.validate_shipment`,
which runs afterwards and says so with a rule identifier.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from translog_quote.domain.shipment import ExtractedFields

if TYPE_CHECKING:
    from translog_quote.domain.extraction.model import ExtractionResult

_MAPPED_FIELDS: tuple[str, ...] = (
    "origin",
    "destination",
    "weight_kg",
    "dimensions_in",
    "commodity",
    "cargo_type",
    "is_chemical",
    "msds_attached",
    "pcs",
    "delivery_type",
    "delivery_address",
)


def to_extracted_fields(result: ExtractionResult) -> ExtractedFields:
    """Narrow an ``ExtractionResult`` to the canonical ``ExtractedFields``.

    A field is carried across only when its status is ``STATED``. Everything
    else — silent, denied, ambiguous — becomes ``None``, because ``None`` is the
    only thing the canonical record can say about a field it does not know.

    **This narrowing is lossy, and deliberately so.** Three distinct reasons for
    absence collapse into one null. The ``ExtractionResult`` is the record that
    keeps them apart; hold on to it if you need to know *why* a field is empty
    (for an audit trail, or to avoid re-asking a client something they already
    answered). Do not try to recover the reason from the canonical record — it
    is not in there.
    """
    values = {
        name: getattr(result, name).value if getattr(result, name).is_stated else None
        for name in _MAPPED_FIELDS
    }
    return ExtractedFields(**values)
