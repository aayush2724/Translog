"""The language-model boundary."""

from __future__ import annotations

from typing import Protocol

from translog_quote.domain.decision import ClientIntent
from translog_quote.domain.extraction import ExtractionResult


class ExtractionPort(Protocol):
    """Turning unstructured email into structured fields.

    The only boundary in the system behind which a language model is called. Both
    methods report what the text *says*; neither decides what happens next.

    ``extract_shipment`` returns an ``ExtractionResult`` rather than the canonical
    ``ExtractedFields``. The extra width is the point: the result records *why* a
    field is empty — silent, denied, ambiguous — and the canonical record cannot.
    `domain.extraction.to_extracted_fields` narrows one into the other when the
    caller is ready to lose that distinction.

    Implementations must never fill a field the email did not state (BR-7), and
    must raise ContractViolation rather than return a partial parse: half a
    shipment record is more dangerous than none, because validation would pass it.
    Model output that cannot be parsed into an ``ExtractionResult`` is a failure
    of the model, not an empty extraction, and must not be reported as one.
    """

    def extract_shipment(self, text: str) -> ExtractionResult: ...

    def read_client_intent(self, text: str) -> ClientIntent: ...
