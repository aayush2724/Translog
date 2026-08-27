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

from translog_quote.domain.shipment import FieldName

#: What to ask when the client has said nothing about a field.
MISSING_QUESTIONS: dict[FieldName, str] = {
    FieldName.ORIGIN: "The origin — the city or airport the cargo ships from",
    FieldName.DESTINATION: "The destination — the city or airport the cargo is going to",
    FieldName.WEIGHT_KG: "The total shipment weight in kg",
    FieldName.DIMENSIONS_IN: "The package dimensions in inches (length x width x height)",
    FieldName.COMMODITY: "The commodity being shipped",
    FieldName.CARGO_TYPE: "The cargo type — for example general cargo, non-hazardous, or hazardous",
    FieldName.IS_CHEMICAL: "Whether the cargo is a chemical product",
    FieldName.MSDS_ATTACHED: "The MSDS for this cargo, or confirmation that one is not available",
    FieldName.PCS: "The number of pieces, and how they are packed",
    FieldName.DELIVERY_TYPE: "Whether you need door delivery or airport-to-airport",
    FieldName.DELIVERY_ADDRESS: "The full delivery address",
}

#: What to ask when the client stated something we cannot use as-is. The ask is
#: for the same fact in the form the quote needs — never a conversion on our side.
AMBIGUOUS_QUESTIONS: dict[FieldName, str] = {
    FieldName.WEIGHT_KG: "The total shipment weight in kg",
    FieldName.DIMENSIONS_IN: "The package dimensions in inches (length x width x height)",
    FieldName.PCS: "The total number of pieces for this shipment",
    FieldName.ORIGIN: "The single origin airport or city for this shipment",
    FieldName.DESTINATION: "The single destination airport or city for this shipment",
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
}


def missing_question(field: FieldName) -> str:
    return MISSING_QUESTIONS[field]


def ambiguous_question(field: FieldName) -> str:
    """Fall back to the missing wording when a field has no special ambiguity
    phrasing — asking for it plainly is always a sensible question."""
    return AMBIGUOUS_QUESTIONS.get(field, MISSING_QUESTIONS[field])


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
