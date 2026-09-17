"""Mapping an extraction result into the canonical record's field shape.

Structural conversion only. This function performs no validation, applies no
business rule, and reaches no conclusion — it narrows a four-state-per-field
extraction into the canonical two-state (known / null) shape and stops there.

In particular it does **not** notice that ``is_chemical=True`` arrived without
an MSDS. That observation belongs to `domain.validation.validate_shipment`,
which runs afterwards and says so with a rule identifier.

The one exception is ``msds_attached`` (see ``to_extracted_fields``): it is a
yes/no field, so a ``DENIED`` on it is an explicit "no", which the extraction
contract already defines as ``STATED False``. Narrowing that ``DENIED`` to
``None`` would silently discard an answer and strand the request; it is carried
as ``False`` instead. Confined to ``msds_attached`` — no other field, and never
``is_chemical`` (a denied chemical status must never read as "not chemical").
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from translog_quote.domain.extraction.model import FieldStatus
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
    "ship_date",
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

    The sole exception is ``msds_attached``. It is a yes/no field, so a
    ``DENIED`` ("we have no MSDS", "not available") is not an absence at all — it
    is the client answering "no", which the contract records as ``STATED False``.
    A model that emits ``DENIED`` here has stated that same "no" in the wrong
    shape; it is carried as ``False`` so VR-8 reads it as answered rather than
    mistaking it for an unanswerable gap. Confined to ``msds_attached``: a
    ``DENIED`` ``is_chemical`` must never become ``False`` ("not a chemical").
    """
    values = {
        name: getattr(result, name).value if getattr(result, name).is_stated else None
        for name in _MAPPED_FIELDS
    }
    if result.msds_attached.status is FieldStatus.DENIED:
        values["msds_attached"] = False
    return ExtractedFields(**values)
