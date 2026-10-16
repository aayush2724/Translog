"""Resolve the year of an extracted shipment date against today.

A client writes "26th September" and means the next 26 September. The model has
to hand back a full calendar date, and with no year in the text it sometimes
picks one from its own sense of "now" — which is not ours — and returns a date
in the past. Production case: "26th september" extracted as 2024-09-26 on
2026-09-23, which then reached WebCargo as a past departure date.

This is the deterministic correction: a stated date that is already in the past
keeps its month and day and moves to the nearest occurrence on or after today.
Pure — ``today`` is passed in (from the injected clock), never read here.

Known limitation, stated plainly rather than papered over: the extraction
contract carries only the calendar date, not whether the client's text named a
year. So this function cannot tell a yearless "26th September" from an explicit
"26 September 2024". It treats every past date as a year the model filled in,
which is the case seen in practice; a client who genuinely asks for a historical
date is not a shipment that can be quoted either way. VR-13 (SHIP_DATE_IN_PAST)
is the backstop for any past date that reaches validation by another path.
"""

from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from translog_quote.domain.extraction.model import ExtractionResult


def roll_forward_to_today(stated: date, today: date) -> date:
    """Return ``stated`` if it is on or after ``today``; otherwise the nearest
    date on or after ``today`` with the same month and day.

    29 February only exists in leap years, so it moves to the next leap year's
    29 February. It is never shifted to 28 February or 1 March, because that
    would be a different day from the one the client named.
    """
    if stated >= today:
        return stated
    year = today.year
    while True:
        try:
            candidate = stated.replace(year=year)
        except ValueError:  # 29 February in a non-leap year
            year += 1
            continue
        if candidate >= today:
            return candidate
        year += 1


def resolve_ship_date(extraction: ExtractionResult, *, today: date) -> ExtractionResult:
    """Return ``extraction`` with a past STATED ``ship_date`` rolled forward.

    Every other case comes back unchanged, and is the same object: a date on or
    after today, and any ``ship_date`` that is not STATED (NOT_STATED, AMBIGUOUS,
    DENIED, INVALID). Evidence is kept, so the audit trail still shows the text
    the date came from.
    """
    stated = extraction.ship_date
    if not stated.is_stated or stated.value is None:
        return extraction
    resolved = roll_forward_to_today(stated.value, today)
    if resolved == stated.value:
        return extraction
    return extraction.model_copy(
        update={"ship_date": stated.model_copy(update={"value": resolved})}
    )


#: A numeric date whose first two parts are both 12 or less ("05/11/2026") reads
#: as 5 November day-first and 11 May month-first. Day-first is the Indian
#: default, but a client who writes month-first would be quoted for the wrong
#: day with no warning, so such a date is confirmed with the client instead.
DATE_ORDER_MARKER = "written with day and month that could be either way round"

_NUMERIC_DATE = re.compile(r"(?<!\d)(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})(?!\d)")


def ambiguous_day_month(text: str | None, stated: date) -> tuple[str, date, date] | None:
    """When ``text`` holds a numeric date that could be either order and one of
    its readings is ``stated``, return (as written, day-first, month-first)."""
    if not text:
        return None
    for match in _NUMERIC_DATE.finditer(text):
        first, second, year = (int(g) for g in match.groups())
        if first > 12 or second > 12 or first == second:
            continue
        try:
            day_first = date(year, second, first)
            month_first = date(year, first, second)
        except ValueError:
            continue
        if stated in (day_first, month_first):
            return match.group(0), day_first, month_first
    return None
