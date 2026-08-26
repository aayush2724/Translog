"""The extraction contract: what a language model may report about one email.

    RawEmail -> [ExtractionPort] -> ExtractionResult -> ExtractedFields
                                                     -> merge -> ShipmentRecord
                                                              -> validation

This package holds the contract and the mapping. It holds no provider, no HTTP,
no prompt-to-response plumbing and no model name — an adapter implementing
`ports.ExtractionPort` supplies those in a later phase, and nothing here changes
when it does.
"""

from translog_quote.domain.extraction.mapping import to_extracted_fields
from translog_quote.domain.extraction.model import (
    ExtractedValue,
    ExtractionResult,
    FieldStatus,
)
from translog_quote.domain.extraction.prompt import (
    EXTRACTION_SCHEMA_GUIDE,
    EXTRACTION_SYSTEM_PROMPT,
    build_extraction_messages,
)

__all__ = [
    "EXTRACTION_SCHEMA_GUIDE",
    "EXTRACTION_SYSTEM_PROMPT",
    "ExtractedValue",
    "ExtractionResult",
    "FieldStatus",
    "build_extraction_messages",
    "to_extracted_fields",
]
