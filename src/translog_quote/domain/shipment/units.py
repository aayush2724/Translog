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
