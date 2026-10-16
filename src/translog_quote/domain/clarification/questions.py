"""The wording. Deterministic templates, one per field and reason.

Client-facing copy, so it is written the way a freight desk writes: plain,
specific, and free of anything internal. No rule identifiers, no schema
vocabulary, no mention of models or validation. A client reading this should
not be able to tell that software wrote it.

Templating rather than generation, deliberately. Outbound copy must be
reviewable, diffable and identical across runs — and a generator asked for a
question is a generator that can invent a premise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from translog_quote.domain.extraction.ship_date import (
    DATE_ORDER_MARKER,
    WEEKDAY_MARKER,
    full_year,
)
from translog_quote.domain.shipment import FieldName
from translog_quote.domain.shipment.units import (
    CHARGEABLE_MARKER,
    PER_PIECE_MARKER,
    SEPARATOR_MARKER,
    UNIT_MISSING_MARKER,
    quoted_from_note,
    thousands_readings,
)

if TYPE_CHECKING:
    from datetime import date

#: What to ask when the client has said nothing about a field.
MISSING_QUESTIONS: dict[FieldName, str] = {
    FieldName.ORIGIN: "The origin — the city or airport the cargo ships from",
    FieldName.DESTINATION: "The destination — the city or airport the cargo is going to",
    FieldName.WEIGHT_KG: "The total shipment weight and its unit (for example kg)",
    FieldName.DIMENSIONS_IN: (
        "The package dimensions (length x width x height) and their unit, for example cm or inches"
    ),
    FieldName.COMMODITY: "The commodity being shipped",
    FieldName.CARGO_TYPE: "The cargo type — for example general cargo, non-hazardous, or hazardous",
    FieldName.IS_CHEMICAL: "Whether the cargo is a chemical product",
    FieldName.MSDS_ATTACHED: "The MSDS for this cargo, or confirmation that one is not available",
    FieldName.PCS: "The number of pieces, and how they are packed",
    FieldName.DELIVERY_TYPE: "Whether you need door delivery or airport-to-airport",
    FieldName.DELIVERY_ADDRESS: "The full delivery address",
    FieldName.SHIP_DATE: "The date you would like the shipment to be picked up or shipped",
}

#: What to ask when the client stated something we cannot use as-is. The ask is
#: for the same fact in the form the quote needs — never a conversion on our side.
AMBIGUOUS_QUESTIONS: dict[FieldName, str] = {
    FieldName.WEIGHT_KG: "The total shipment weight and its unit (for example kg)",
    FieldName.DIMENSIONS_IN: (
        "The package dimensions (length x width x height) and their unit, for example cm or inches"
    ),
    FieldName.PCS: "The total number of pieces for this shipment",
    FieldName.ORIGIN: "The single origin airport or city for this shipment",
    FieldName.DESTINATION: "The single destination airport or city for this shipment",
    # Typically a date given without its year ("26th September"). Asking for the
    # complete date is a different question from the plain "missing" one, which
    # is what lets an ambiguous date be re-asked rather than handed to a person.
    FieldName.SHIP_DATE: "The complete shipment date, including the day, month and year",
}

_CONFLICT = "{label} — we have received two different values, {first} and {second}"

#: Short, client-recognisable field labels for conflict wording.
FIELD_LABELS: dict[FieldName, str] = {
    FieldName.ORIGIN: "Origin",
    FieldName.DESTINATION: "Destination",
    FieldName.WEIGHT_KG: "Total weight",
    FieldName.DIMENSIONS_IN: "Dimensions",
    FieldName.COMMODITY: "Commodity",
    FieldName.CARGO_TYPE: "Cargo type",
    FieldName.IS_CHEMICAL: "Chemical status",
    FieldName.MSDS_ATTACHED: "MSDS",
    FieldName.PCS: "Number of pieces",
    FieldName.DELIVERY_TYPE: "Delivery type",
    FieldName.DELIVERY_ADDRESS: "Delivery address",
    FieldName.SHIP_DATE: "Shipment date",
}


def missing_question(field: FieldName) -> str:
    return MISSING_QUESTIONS[field]


def ambiguous_question(field: FieldName) -> str:
    """Fall back to the missing wording when a field has no special ambiguity
    phrasing — asking for it plainly is always a sensible question."""
    return AMBIGUOUS_QUESTIONS.get(field, MISSING_QUESTIONS[field])


#: What to ask when the client stated an out-of-range value (a non-positive
#: weight, piece count, or dimension). The value is understood and correctable,
#: so the wording names the constraint and asks for a valid value — plainly, the
#: way a freight desk would. Falls back to the plain "missing" wording for any
#: field without a special phrasing.
INVALID_QUESTIONS: dict[FieldName, str] = {
    FieldName.PCS: (
        "The number of pieces must be greater than 0. "
        "Please reply with the correct number of pieces."
    ),
    FieldName.WEIGHT_KG: (
        "The gross weight must be greater than 0 kg. Please reply with the correct gross weight."
    ),
    FieldName.DIMENSIONS_IN: (
        "Each dimension must be greater than 0. Please reply with the correct dimensions."
    ),
}


def invalid_question(field: FieldName) -> str:
    """Ask the client to correct an out-of-range value they stated."""
    return INVALID_QUESTIONS.get(field, MISSING_QUESTIONS[field])


def unit_question(field: FieldName, note: str) -> str:
    """Ask for the unit of a measurement the client gave without one, or in a
    unit that cannot be converted. ``note`` is the field's unit note; the
    client's own wording is quoted back so they can see exactly what is meant.
    """
    seen = quoted_from_note(note)
    missing = UNIT_MISSING_MARKER in note
    if field is FieldName.WEIGHT_KG:
        lead = f"The unit of the weight you gave ({seen})" if seen else "The unit of the weight"
        return (
            f'{lead}. Please reply with the weight and its unit, for example "100 kg" or "220 lb".'
        )
    example = seen if (missing and seen) else "40 x 30 x 24"
    lead = (
        f"The unit of the package dimensions you gave ({seen})"
        if seen
        else "The unit of the package dimensions"
    )
    return (
        f"{lead}. Please reply with the dimensions and their unit, "
        f'for example "{example} cm" or "{example} inches".'
    )


def separator_question(note: str) -> str:
    """Ask which of the two readings of a "1.200 kg"-style weight was meant."""
    written = quoted_from_note(note)
    as_thousands, as_decimal = thousands_readings(written)
    return (
        f'The total shipment weight — you wrote "{written} kg". Please confirm whether '
        f"that is {as_thousands} kg or {as_decimal} kg."
    )


def date_order_question(note: str) -> str:
    """Ask which way round a numeric date like 05/11/2026 was meant."""
    from datetime import date

    written = quoted_from_note(note)
    parts = [int(p) for p in written.replace(".", "/").replace("-", "/").split("/")]
    first, second, year = parts
    year = full_year(year)
    day_first = date(year, second, first)
    month_first = date(year, first, second)
    return (
        f"The shipment date — you wrote {written}. Please confirm whether that is "
        f"{day_first.day} {day_first:%B %Y} or {month_first.day} {month_first:%B %Y}."
    )


def per_piece_question(note: str) -> str:
    """Ask for the total weight when only a per-piece weight was given."""
    seen = quoted_from_note(note)
    lead = f"you gave {seen} per piece" if seen else "you gave the weight per piece"
    return (
        f"The total shipment weight — {lead}. Please reply with the total weight of "
        "all pieces together, and the number of pieces."
    )


def chargeable_question(note: str) -> str:
    """Ask for the actual gross weight when only a chargeable weight was given."""
    seen = quoted_from_note(note)
    lead = f"you gave the chargeable weight ({seen})" if seen else "you gave the chargeable weight"
    return (
        f"The actual gross weight of the shipment — {lead}. Please reply with the "
        "actual weight; the chargeable weight is worked out from it and the dimensions."
    )


def weekday_question(note: str, written: str | None) -> str:
    """Ask which was meant when the weekday written does not fall on the date."""
    from datetime import date

    named, iso = quoted_from_note(note).split(", ", 1)
    stated = date.fromisoformat(iso)
    shown = (
        f'you wrote "{written}"' if written else f"you wrote {named}, {stated.day} {stated:%B %Y}"
    )
    return (
        f"The shipment date — {shown}, but {stated.day} {stated:%B %Y} is a "
        f"{stated:%A}, not a {named}. Please confirm the date you would like the "
        "shipment to leave."
    )


def is_weekday_note(note: str | None) -> bool:
    return bool(note) and WEEKDAY_MARKER in (note or "")


def is_per_piece_note(note: str | None) -> bool:
    return bool(note) and PER_PIECE_MARKER in (note or "")


def is_chargeable_note(note: str | None) -> bool:
    return bool(note) and CHARGEABLE_MARKER in (note or "")


def is_separator_note(note: str | None) -> bool:
    return bool(note) and SEPARATOR_MARKER in (note or "")


def is_date_order_note(note: str | None) -> bool:
    return bool(note) and DATE_ORDER_MARKER in (note or "")


#: What to ask when a stated place cannot be resolved to a single airport without
#: guessing — discovered at rate-search time, not extraction. It names the place
#: exactly as the client wrote it. The example code is a fixed, neutral
#: illustration of the *format* (never the place being asked about), so a
#: question about Nairobi does not suggest Delhi any more than one about Delhi
#: does — and it must never be read back as, or written into the record as, the
#: answer.
_LOCATION_UNRESOLVED = (
    '{label} airport — we could not identify a single airport for "{stated}". '
    "Please confirm the airport, ideally with its three-letter code "
    '(for example, "Delhi (DEL)").'
)


_SAME_AIRPORT = (
    '{label} airport — the origin and destination both point to the same airport ("{stated}"). '
    "Please confirm the {end} airport, ideally with its three-letter code."
)


def location_question(field: FieldName, stated: str, *, same_as_other_end: bool = False) -> str:
    """Ask for the airport behind a place we could not resolve without guessing.

    ``field`` is ORIGIN or DESTINATION; ``stated`` is the client's own wording,
    quoted back verbatim. Unlike the static ``AMBIGUOUS_QUESTIONS`` entries this
    interpolates the stated place, so it is a function, not a table row.
    """
    if same_as_other_end:
        end = "origin" if field is FieldName.ORIGIN else "destination"
        return _SAME_AIRPORT.format(label=FIELD_LABELS[field], stated=stated, end=end)
    return _LOCATION_UNRESOLVED.format(label=FIELD_LABELS[field], stated=stated)


#: What to ask when the shipment date on a validated request is already in the
#: past — found at rate-search time, so a search would be refused. It quotes the
#: date we hold, so the client can see what is being corrected.
_PAST_SHIP_DATE = (
    "Shipment date — the date we have, {stated}, is already in the past. Please "
    "confirm the date you would like the shipment to leave, including the day, "
    "month and year."
)


def past_ship_date_question(stated: date) -> str:
    """Ask for a current shipment date in place of one that has already passed."""
    return _PAST_SHIP_DATE.format(stated=f"{stated.day} {stated:%B %Y}")


def conflict_question(field: FieldName, existing: object, incoming: object) -> str:
    return _CONFLICT.format(
        label=FIELD_LABELS[field],
        first=_render(field, existing),
        second=_render(field, incoming),
    )


def _render(field: FieldName, value: object) -> str:
    """Values as the client would recognise them, with their unit."""
    if value is None:
        return "not given"
    if field is FieldName.WEIGHT_KG:
        return f"{value} kg"
    if field is FieldName.PCS:
        return f"{value} pieces"
    if field is FieldName.IS_CHEMICAL:
        return "yes" if value else "no"
    return str(value)
