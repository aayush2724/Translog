"""Deterministic year resolution for an extracted shipment date.

Production case: a client replied "26th september" (no year anywhere) on
2026-09-23, the model returned 2024-09-26, and the past date reached WebCargo.
`resolve_ship_date` keeps the month and day and moves a past date to its nearest
occurrence on or after today. It cannot tell a yearless date from an explicitly
historical one — the extraction contract does not carry that — and these tests
pin the behaviour it does have, not the one it cannot.
"""

from __future__ import annotations

from datetime import date

import pytest

from translog_quote.domain.extraction import (
    ExtractedValue,
    ExtractionResult,
    resolve_ship_date,
    roll_forward_to_today,
)

TODAY = date(2026, 9, 23)


# --- roll_forward_to_today ------------------------------------------------------


@pytest.mark.parametrize(
    ("stated", "today", "expected"),
    [
        # The production case: model-invented year, same month/day this year.
        (date(2024, 9, 26), TODAY, date(2026, 9, 26)),
        # Month/day already passed this year -> next year.
        (date(2024, 3, 1), TODAY, date(2027, 3, 1)),
        (date(2026, 9, 22), TODAY, date(2027, 9, 22)),
        # Same month/day as today -> today (>= today, not strictly after).
        (date(2025, 9, 23), TODAY, TODAY),
        # Year-end boundary.
        (date(2025, 12, 31), TODAY, date(2026, 12, 31)),
        (date(2025, 1, 1), date(2026, 12, 31), date(2027, 1, 1)),
    ],
)
def test_a_past_date_moves_to_the_nearest_same_month_and_day(
    stated: date, today: date, expected: date
) -> None:
    assert roll_forward_to_today(stated, today) == expected


@pytest.mark.parametrize("stated", [TODAY, date(2026, 9, 24), date(2027, 1, 5)])
def test_today_or_a_future_date_is_unchanged(stated: date) -> None:
    assert roll_forward_to_today(stated, TODAY) == stated


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (TODAY, date(2028, 2, 29)),  # 2027 has no 29 Feb
        (date(2028, 2, 29), date(2028, 2, 29)),  # it is 29 Feb today
        (date(2028, 3, 1), date(2032, 2, 29)),  # just missed -> next leap year
        (date(2099, 3, 1), date(2104, 2, 29)),  # 2100 is not a leap year
    ],
)
def test_29_february_moves_to_the_next_leap_year_never_to_another_day(
    today: date, expected: date
) -> None:
    assert roll_forward_to_today(date(2024, 2, 29), today) == expected


# --- resolve_ship_date ------------------------------------------------------------


def _with_ship_date(value: ExtractedValue[date]) -> ExtractionResult:
    return ExtractionResult(origin=ExtractedValue[str].stated("Chennai"), ship_date=value)


def test_a_past_stated_date_is_rolled_forward_keeping_status_and_evidence() -> None:
    extraction = _with_ship_date(
        ExtractedValue[date].stated(date(2024, 9, 26), evidence="26th september")
    )

    resolved = resolve_ship_date(extraction, today=TODAY)

    assert resolved.ship_date.is_stated
    assert resolved.ship_date.value == date(2026, 9, 26)
    assert resolved.ship_date.evidence == "26th september"
    assert resolved.origin == extraction.origin  # nothing else touched
    assert extraction.ship_date.value == date(2024, 9, 26)  # input not mutated


def test_a_current_stated_date_returns_the_same_object() -> None:
    extraction = _with_ship_date(ExtractedValue[date].stated(date(2026, 9, 26)))

    assert resolve_ship_date(extraction, today=TODAY) is extraction


@pytest.mark.parametrize(
    "value",
    [
        ExtractedValue[date].not_stated(),
        ExtractedValue[date].ambiguous(note="at the earliest", evidence="at the earliest"),
        ExtractedValue[date].denied(evidence="no fixed date"),
    ],
    ids=["not_stated", "ambiguous", "denied"],
)
def test_a_field_without_a_stated_date_is_left_alone(value: ExtractedValue[date]) -> None:
    extraction = _with_ship_date(value)

    assert resolve_ship_date(extraction, today=TODAY) is extraction
