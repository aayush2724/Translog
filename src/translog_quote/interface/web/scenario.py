"""The web POC's demonstration scenario, stated once.

The emails are the terminal POC's fictional Northgate Exports thread, imported
rather than copied so the two demos cannot drift apart. The extraction results
are scripted: they carry the same values the live model returns for these two
emails, so the rest of the pipeline — merge, validation, clarification wording,
filtering, selection — runs its real code with no network call and no API key.

Scripted extraction is a demonstration convenience, not a claim. The UI labels
the run as a POC, and the live path stays available through the terminal demo
(``python -m translog_quote.interface.demo poc``), which this module does not
replace.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from translog_quote.domain.extraction import ExtractedValue, ExtractionResult
from translog_quote.domain.shipment import CargoDimensions, DeliveryType
from translog_quote.errors import ContractViolation
from translog_quote.interface.demo.poc_demo import (
    CARGO_IS_LIQUID,
    CLIENT_REPLY,
    INITIAL_ENQUIRY,
    REQUEST_ID,
    SEARCH_DATE,
)

if TYPE_CHECKING:
    from translog_quote.domain.decision import ClientIntent

__all__ = [
    "CARGO_IS_LIQUID",
    "CLIENT_COMPANY",
    "CLIENT_NAME",
    "CLIENT_REPLY",
    "ENQUIRY_EXTRACTION",
    "INITIAL_ENQUIRY",
    "REPLY_EXTRACTION",
    "REQUEST_ID",
    "SEARCH_DATE",
    "ScriptedExtractor",
]

#: Fictional, like everything in the scenario. Shown on the dashboard card.
CLIENT_NAME = "Priya Nair"
CLIENT_COMPANY = "Northgate Exports"

#: What the initial enquiry states: five fields, four required ones short.
#: Evidence quotes are the lines of the fictional email they come from.
ENQUIRY_EXTRACTION = ExtractionResult(
    origin=ExtractedValue[str].stated("Ahmedabad", evidence="Origin: Ahmedabad"),
    destination=ExtractedValue[str].stated("Bahrain", evidence="Destination: Bahrain"),
    weight_kg=ExtractedValue[float].stated(500.0, evidence="Gross weight: 500 KG"),
    dimensions_in=ExtractedValue[CargoDimensions].stated(
        CargoDimensions(length=34, width=24, height=6),
        evidence="Dimensions: 24 (width) x 34 (length) x 6 (breadth) inches",
    ),
    cargo_type=ExtractedValue[str].stated("Non-Haz", evidence="Cargo type: Non-Haz"),
)

#: What the reply states: exactly the four gaps, and nothing else, so the merge
#: has to preserve the origin, destination, weight and dimensions itself.
REPLY_EXTRACTION = ExtractionResult(
    commodity=ExtractedValue[str].stated(
        "Engineering components", evidence="Commodity: Engineering components"
    ),
    is_chemical=ExtractedValue[bool].stated(value=False, evidence="Chemical: No"),
    pcs=ExtractedValue[int].stated(10, evidence="Pieces: 10 cartons"),
    delivery_type=ExtractedValue[DeliveryType].stated(
        DeliveryType.AIRPORT, evidence="Delivery: Airport to airport"
    ),
)


class ScriptedExtractor:
    """An `ExtractionPort` that replays prepared results, in order.

    The same stand-in the offline test suite drives the workflow with. It
    exists so the web demo exercises the real pipeline deterministically; a
    third call is a scripting error and refuses loudly rather than repeating.
    """

    def __init__(self, *results: ExtractionResult) -> None:
        self._results = list(results)

    def extract_shipment(self, text: str) -> ExtractionResult:
        if not self._results:
            raise ContractViolation("the scripted scenario has no further extraction to give")
        return self._results.pop(0)

    def read_client_intent(self, text: str) -> ClientIntent:
        raise NotImplementedError("the web POC scenario never reads client intent")
