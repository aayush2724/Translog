"""Unit conversion for the two measured shipment fields: dimensions and weight.

The canonical record holds dimensions in inches (``dimensions_in``) and weight
in kilograms (``weight_kg``), because that is what WebCargo's search form is
filled with. Clients write whatever they use: centimetres, millimetres, metres,
feet, pounds, tonnes. The extraction model reports the numbers *exactly as
written* together with the unit it saw; this module does the arithmetic.

Why here and not in the model: a language model asked to convert is a language
model asked to do arithmetic, and a slip there is silent — a box declared at a
third of its real volume prices wrong and nobody notices until the airline
re-weighs it. Conversion in code is exact, tested, and the same every time.

Rounding is always **up**, to one decimal place, and only when a conversion
actually happened. Declaring a package fractionally larger than measured costs
nothing; declaring it smaller is a misdeclaration. A value already in the
canonical unit is passed through untouched.

A unit this module does not recognise — or no unit at all — is never guessed.
``None`` comes back and the caller asks the client.
"""

from __future__ import annotations

import math
import re

#: Inches per one unit. Keys are canonical unit names.
_INCHES_PER: dict[str, float] = {
    "in": 1.0,
    "cm": 1 / 2.54,
    "mm": 1 / 25.4,
    "m": 100 / 2.54,
    "ft": 12.0,
}

#: Kilograms per one unit.
_KG_PER: dict[str, float] = {
    "kg": 1.0,
    "g": 0.001,
    "lb": 0.45359237,
    "t": 1000.0,
}

#: Spellings clients actually use, folded to a canonical unit. Matched after
#: lower-casing and removing spaces and dots, so "Cms.", "CM" and "c.m." all hit.
#: Deliberately absent: "ton"/"tons" (short ton, long ton or tonne? — asked, never
#: guessed).
_LENGTH_ALIASES: dict[str, str] = {
    **dict.fromkeys(("in", "inch", "inches", '"', "″", "''"), "in"),
    **dict.fromkeys(("cm", "cms", "centimeter", "centimeters", "centimetre", "centimetres"), "cm"),
    **dict.fromkeys(("mm", "mms", "millimeter", "millimeters", "millimetre", "millimetres"), "mm"),
    **dict.fromkeys(("m", "mtr", "mtrs", "meter", "meters", "metre", "metres"), "m"),
    **dict.fromkeys(("ft", "foot", "feet", "'", "′"), "ft"),
}

_WEIGHT_ALIASES: dict[str, str] = {
    **dict.fromkeys(
        ("kg", "kgs", "kilo", "kilos", "kilogram", "kilograms", "kilogramme", "kilogrammes"),
        "kg",
    ),
    **dict.fromkeys(("g", "gm", "gms", "gram", "grams", "gramme", "grammes"), "g"),
    **dict.fromkeys(("lb", "lbs", "pound", "pounds"), "lb"),
    **dict.fromkeys(("t", "mt", "tonne", "tonnes", "metricton", "metrictons", "metrictonne"), "t"),
}

_SQUEEZE = re.compile(r"[\s.]+")


def _fold(raw: str) -> str:
    return _SQUEEZE.sub("", raw.strip().lower())


def length_unit(raw: str | None) -> str | None:
    """The canonical length unit for a client's spelling, or ``None`` when the
    spelling is absent or not one this module will convert."""
    if raw is None:
        return None
    return _LENGTH_ALIASES.get(_fold(raw))


def weight_unit(raw: str | None) -> str | None:
    """The canonical weight unit for a client's spelling, or ``None``."""
    if raw is None:
        return None
    return _WEIGHT_ALIASES.get(_fold(raw))


def _round_up(value: float) -> float:
    # The small epsilon keeps an exact result (25.4 mm -> 1.0 in) from being
    # pushed up a tenth by floating-point noise (9.999999999 * 10 -> ceil 100).
    return math.ceil(value * 10 - 1e-9) / 10


def to_inches(value: float, unit: str) -> float:
    """``value`` in canonical length ``unit``, expressed in inches.

    Raises ``KeyError`` for a unit that is not canonical — resolve it with
    `length_unit` first."""
    if unit == "in":
        return float(value)
    return _round_up(value * _INCHES_PER[unit])


def to_kg(value: float, unit: str) -> float:
    """``value`` in canonical weight ``unit``, expressed in kilograms."""
    if unit == "kg":
        return float(value)
    return _round_up(value * _KG_PER[unit])


#: Note markers for a measured field the client gave in a form we cannot use:
#: numbers with no unit, or a unit this module will not convert. Both are
#: *correctable* — the client knows the unit — so the field is recorded as
#: INVALID (re-asked, never handed to a person), and the clarification layer
#: recognises these markers to ask for the unit rather than "a valid value".
#: The text after ": " is what the client wrote, quoted back in the question.
UNIT_MISSING_MARKER = "given without a unit"
UNIT_UNSUPPORTED_MARKER = "given in a unit that cannot be converted"


def unit_note(field_label: str, marker: str, seen: str) -> str:
    """The note recorded on a field whose unit is missing or unsupported."""
    return f"{field_label} {marker}: {seen}"


def is_unit_note(note: str | None) -> bool:
    """Whether ``note`` was written by `unit_note` — i.e. the unit is the problem."""
    if not note:
        return False
    return UNIT_MISSING_MARKER in note or UNIT_UNSUPPORTED_MARKER in note


def quoted_from_note(note: str) -> str:
    """The client's own wording carried in a `unit_note`, or ``""``."""
    return note.split(": ", 1)[1] if ": " in note else ""


#: A weight written as "1.200" or "2.500" — a dot followed by exactly three
#: digits — is how much of Europe writes 1,200 and 2,500. Read naively it is
#: 1.2 kg and 2.5 kg: a thousand-fold under-declaration that prices wrong and
#: raises no alarm. Never guessed either way: the client is asked which they
#: meant. ("1.5" or "12.25" are ordinary decimals and are not touched.)
SEPARATOR_MARKER = "written with a dot that may be a thousands separator"

_DOT_THOUSANDS = re.compile(r"(?<![\d.,])(\d{1,3}(?:\.\d{3})+)(?![\d.,])")


def dot_thousands_in(text: str | None, value: float) -> str | None:
    """The number in ``text`` written like "1.200" that a model read as
    ``value`` (1.2), or ``None`` when there is no such number."""
    if not text:
        return None
    for match in _DOT_THOUSANDS.finditer(text):
        written = match.group(1)
        if math.isclose(float(written), value):
            return written
    return None


def thousands_readings(written: str) -> tuple[str, str]:
    """The two readings of a "1.200"-style number, as client-facing text:
    ("1,200", "1.2")."""
    as_thousands = f"{int(written.replace('.', '')):,}"
    as_decimal = f"{float(written):g}"
    return as_thousands, as_decimal


#: Feet-and-inches — "2'6\"", "2 ft 6 in", "2' 6''", "3 ft" — a compound
#: measurement no single factor converts. The model reports each side as
#: written with this unit, and `parse_feet_inches` does the arithmetic. The
#: result is exact (feet × 12 + inches), so it is not rounded.
FEET_INCHES = "ft-in"

_FEET_INCHES_ALIASES = ("ft-in", "ftin", "ft_in", "feet-inches", "feetinches", "ftandin")

#: A measured number as clients write it: "2", "2.5", "1 1/2", "1/2", "1½".
_NUM = r"\d+(?:\.\d+)?(?:\s*(?:\d+/\d+|[½¼¾]))?|\d+/\d+|[½¼¾]"
_VULGAR = {"½": 0.5, "¼": 0.25, "¾": 0.75}


def parse_number(text: str) -> float | None:
    """ "1 1/2" -> 1.5, "1½" -> 1.5, "3/4" -> 0.75, "2.5" -> 2.5; ``None`` for
    anything else. Whole text only — no unit."""
    match = re.fullmatch(
        r"\s*(?:(?P<whole>\d+(?:\.\d+)?)\s*)?(?:(?P<num>\d+)/(?P<den>\d+)|(?P<vulgar>[½¼¾]))?\s*",
        text,
    )
    if match is None or not (match.group("whole") or match.group("num") or match.group("vulgar")):
        return None
    total = float(match.group("whole") or 0)
    if match.group("num"):
        if match.group("whole") and "." in match.group("whole"):
            return None  # "1.5 1/2" is not a number anyone writes
        den = int(match.group("den"))
        if den == 0:
            return None
        total += int(match.group("num")) / den
    if match.group("vulgar"):
        total += _VULGAR[match.group("vulgar")]
    return total


_FEET_INCHES_TEXT = re.compile(
    rf"""^\s*
        (?P<feet>{_NUM})\s*(?:'|′|ft|feet|foot)\s*
        (?:(?P<inches>{_NUM})\s*(?:"|″|''|in|inch|inches)?)?
        \s*$""",
    re.IGNORECASE | re.VERBOSE,
)


def is_feet_inches(raw: str | None) -> bool:
    """Whether ``raw`` names the compound feet-and-inches unit."""
    return raw is not None and _fold(raw) in _FEET_INCHES_ALIASES


_INCHES_ONLY_TEXT = re.compile(rf'^\s*({_NUM})\s*(?:"|″|\'\'|in|inch|inches)\s*$', re.IGNORECASE)


def parse_feet_inches(text: str) -> float | None:
    """``"2'6\\""`` -> 30.0 inches; ``None`` when ``text`` is not feet-and-inches.

    A side written in inches alone (``20"``, ``20 in``) is accepted too, because
    clients mix them: ``2'6" x 20" x 16"``. A bare number with no mark is not
    parsed here — it carries no unit. Inches of 12 or more after feet
    ("2'14\\"") are refused rather than carried over: a typo there is more
    likely than a deliberate 14 inches."""
    inches_only = _INCHES_ONLY_TEXT.match(text)
    if inches_only is not None:
        value = parse_number(inches_only.group(1))
        return value if value is not None and value > 0 else None
    match = _FEET_INCHES_TEXT.match(text)
    if match is None:
        return None
    feet = parse_number(match.group("feet"))
    inches = parse_number(match.group("inches")) if match.group("inches") else 0.0
    if feet is None or inches is None or inches >= 12:
        return None
    total = feet * 12 + inches
    return total if total > 0 else None


#: A side written as text under an ordinary length unit — "1 1/2 ft", "45cm",
#: "2½" — is read as a number (fractions included) with the unit word, if any,
#: removed. The unit word must agree with the field's unit; a different one is
#: refused rather than mixed.
_TRAILING_UNIT = re.compile(r"^(?P<num>.*?)\s*(?P<unit>[a-z\"'″′.]+)?\s*$", re.IGNORECASE)


def parse_side(text: str, unit: str) -> float | None:
    """One dimension written as text, in canonical length ``unit``, to inches."""
    match = _TRAILING_UNIT.match(text)
    if match is None:
        return None
    written_unit = match.group("unit")
    if written_unit and length_unit(written_unit) != unit:
        return None
    value = parse_number(match.group("num"))
    if value is None or value <= 0:
        return None
    return to_inches(value, unit)


#: A weight given per piece ("50 kg each", "20 kg per carton", "15 kg/pc") is not
#: the shipment total WebCargo needs. With the piece count stated it is
#: multiplied out — exact arithmetic on two stated facts — otherwise the client
#: is asked for the total.
PER_PIECE_MARKER = "given per piece, not as the shipment total"

_PACKAGE = (
    r"(?:pieces?|pcs?|pces?|box(?:es)?|cartons?|ctns?|pallets?|plts?|bags?|drums?"
    r"|crates?|cases?|packages?|pkgs?|units?|parcels?|skids?)"
)
_PER_PIECE = re.compile(
    rf"\beach\b|\bea\b|\bper\s+{_PACKAGE}\b|/\s*{_PACKAGE}\b|\bapiece\b",
    re.IGNORECASE,
)
_TOTAL_WORD = re.compile(
    r"\b(?:total|totals|totalling|totaling|altogether|in\s+all|overall)\b", re.IGNORECASE
)


def is_per_piece(text: str | None) -> bool:
    """Whether a weight's evidence says it is per piece (and not also a total)."""
    if not text:
        return False
    return bool(_PER_PIECE.search(text)) and not _TOTAL_WORD.search(text)


def times_pieces(kg: float, pcs: int) -> float:
    """A per-piece weight in kg multiplied out to the shipment total."""
    return _round_up(kg * pcs)


#: "1.5k kg" or "2K KGS" is 1,500 / 2,000 kg. The model copies the number as
#: written (1.5), so the thousand is restored here rather than lost.
_KILO_SUFFIX = re.compile(
    r"(?<![\d.,])(\d+(?:\.\d+)?)\s*k\s*(?=(?:kgs?|kilos?|kilograms?|lbs?|pounds?)\b)",
    re.IGNORECASE,
)


def kilo_suffix_in(text: str | None, value: float) -> bool:
    """Whether ``text`` writes ``value`` with a thousands "k" ("1.5k kg")."""
    if not text:
        return False
    return any(math.isclose(float(m.group(1)), value) for m in _KILO_SUFFIX.finditer(text))


#: A chargeable or volumetric weight is the airline's billing figure, derived
#: from the dimensions — not the actual gross weight WebCargo searches with.
#: Taking it as the gross weight double-counts volume, so the client is asked
#: for the actual weight.
CHARGEABLE_MARKER = "given as a chargeable or volumetric weight, not the actual weight"

_CHARGEABLE = re.compile(
    r"\b(?:chargeable|charg\.|volumetric|volume\s+weight|vol\.?\s*(?:wt|weight)|dimensional\s+weight|dim\.?\s+weight)\b",
    re.IGNORECASE,
)
_ACTUAL_WORD = re.compile(r"\b(?:gross|actual|net|g\.?w\.?)\b", re.IGNORECASE)


def is_chargeable_only(text: str | None) -> bool:
    """Whether a weight's evidence calls it chargeable/volumetric and gives no
    actual (gross) weight alongside."""
    if not text:
        return False
    return bool(_CHARGEABLE.search(text)) and not _ACTUAL_WORD.search(text)
