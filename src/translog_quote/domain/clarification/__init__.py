"""The batched clarification (BR-9): what is still unresolved, and how to ask.

    ValidationResult  ─┐
    ExtractionResult  ─┼─> identify_unresolved -> compose_clarification -> message
    merge conflicts   ─┘

Deterministic end to end. The validator decides what is required and absent,
the extraction result explains why, and the merge reports disagreement — no
model is asked any of it, and nothing here can produce a shipment value.
"""

from translog_quote.domain.clarification.compose import (
    DEFAULT_SUBJECT,
    UnresolvedAnalysis,
    compose_clarification,
    identify_unresolved,
)
from translog_quote.domain.clarification.model import (
    ClarificationMessage,
    UnresolvedField,
    UnresolvedReason,
)

__all__ = [
    "DEFAULT_SUBJECT",
    "ClarificationMessage",
    "UnresolvedAnalysis",
    "UnresolvedField",
    "UnresolvedReason",
    "compose_clarification",
    "identify_unresolved",
]
