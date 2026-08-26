"""Deterministic normalization of extracted fields.

No LLM, no inference, no guessed defaults. This is formatting cleanup only —
whitespace and blank-string collapsing — never a change in meaning.

What this module does NOT do, and why:

- It does not parse free text into numbers or units (e.g. turning the string
  "500 Kgs" into ``weight_kg = 500.0``). ``ExtractedFields`` types ``weight_kg``,
  ``pcs`` and ``dimensions_in`` as numeric already — producing those numbers from
  raw email text is the extraction adapter's job under ``ExtractionPort``
  (Phase 5), not domain normalization. By the time an ``ExtractedFields`` instance
  exists, that parsing has already happened.
- It does not convert units (e.g. lb to kg). The canonical schema has exactly one
  unit per measurement (``weight_kg``, inches for dimensions), so there is no unit
  ambiguity left for this layer to resolve.
- It never infers a value that was not stated (BR-7).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from translog_quote.domain.shipment.model import ExtractedFields

_WHITESPACE_RUN = re.compile(r"\s+")

_STRING_FIELDS: tuple[str, ...] = (
    "origin",
    "destination",
    "commodity",
    "cargo_type",
    "delivery_address",
)


def _clean_text(value: str) -> str | None:
    """Collapse whitespace; a blank result is treated as absent, not stated."""
    collapsed = _WHITESPACE_RUN.sub(" ", value).strip()
    return collapsed or None


def normalize_extracted_fields(fields: ExtractedFields) -> ExtractedFields:
    """Return a copy of ``fields`` with free-text fields whitespace-normalized.

    A field the email stated as only whitespace (or that arrives as an empty
    string) is treated as not stated at all, so it does not masquerade as a known
    value further down the pipeline.
    """
    updates: dict[str, str | None] = {}
    for name in _STRING_FIELDS:
        current = getattr(fields, name)
        if isinstance(current, str):
            updates[name] = _clean_text(current)

    return fields.model_copy(update=updates) if updates else fields
