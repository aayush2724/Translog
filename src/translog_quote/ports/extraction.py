"""The language-model boundary."""

from __future__ import annotations

from typing import Protocol

from translog_quote.domain.decision import ClientIntent
from translog_quote.domain.shipment import ExtractedFields


class ExtractionPort(Protocol):
    """Turning unstructured email into structured fields.

    The only boundary in the system behind which a language model is called. Both
    methods report what the text *says*; neither decides what happens next.

    Implementations must never fill a field the email did not state (BR-7), and
    must raise ContractViolation rather than return a partial parse: half a
    shipment record is more dangerous than none, because validation would pass it.
    """

    def extract_shipment(self, text: str) -> ExtractedFields: ...

    def read_client_intent(self, text: str) -> ClientIntent: ...
