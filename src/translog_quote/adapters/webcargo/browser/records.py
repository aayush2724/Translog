"""What WebCargo actually displayed, captured verbatim.

Provider-shaped and deliberately raw: every field is the string the UI
showed, unparsed, so the capture is an evidence record rather than an
interpretation. Parsing into the canonical `Rate` happens in `mapper.py`,
where every transformation is individually testable — and refusable.

UI concepts stay separate, exactly as the surfaces are separate:

    WebCargoRateRecord   — one result row (the rate: pricing, Duration)
    FlightLegRecord      — one leg from the expanded Flight Information
                           (supplementary/audit; never a transit source
                           when the row already states Duration)
    WebCargoResultSet    — everything one search showed, plus the
                           provider's own candidate-set statement
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class FlightLegRecord(BaseModel):
    """One row of the expanded Flight Information panel. Audit data."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    carrier: str = ""
    origin: str = ""
    destination: str = ""
    departure: str = ""  # e.g. "15/09/2026 04:00" — local clock, as shown
    arrival: str = ""
    duration: str = ""  # the leg's own figure, e.g. "4h 00m"
    aircraft: str = ""  # e.g. "35H"
    flight_number: str = ""  # e.g. "QR573"


class WebCargoRateRecord(BaseModel):
    """One eBooking result row, as displayed. All strings, all verbatim.

    ``duration`` is WebCargo's own rate-associated transit (e.g. "34h 00m")
    and is the ONLY transit source. ``itinerary`` is routing — Via — and the
    mapper gives it no path into a duration.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    company: str = ""  # "Qatar Airways"
    itinerary: str = ""  # "BOMISTDXB" / "BLR DOH MNL" — routing, never transit
    departure: str = ""  # "Tue 15 Sep - 04:00"
    arrival: str = ""  # "Wed 16 Sep - 16:30"
    duration: str = ""  # "34h 00m" — provider-stated; empty means NOT STATED
    service: str = ""  # "QR General" / "TK URGENT"
    rate: str = ""  # "167.65 Rs/kg" (possibly label-prefixed by the UI)
    surcharges: str = ""  # "All-in" / "All-in(+)"
    price: str = ""  # the price block, e.g. "400.00 Rs/kg/ 40,000 Rs"
    date_tab: str = ""  # which date bucket showed this row, e.g. "15/09/2026"

    legs: tuple[FlightLegRecord, ...] = ()


class WebCargoResultSet(BaseModel):
    """Everything one search displayed, with the provider's own count.

    ``stated_count`` comes from WebCargo's "Showing the N lowest rates" (or
    "We found the N cheapest rates") sentence; ``stated_phrase`` keeps the
    sentence verbatim. The adapter guarantees ``len(records) == stated_count``
    or refuses loudly — a mismatch means rows were missed, and missed rows
    presented as a complete set is exactly the lie this type exists to make
    impossible. The phrase also travels into `RateSearchResult.completeness`:
    "lowest" is the provider's wording, and no consumer may promote this
    candidate set into "all rates that exist".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    stated_count: int = Field(ge=0)
    stated_phrase: str
    records: tuple[WebCargoRateRecord, ...]
