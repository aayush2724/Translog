"""Transit time in client quotations reads as a person writes it.

WebCargo states transit to the minute, so live quotations printed
"Transit time: 1940 minutes". The emails now use the same formatter as the
dashboard (``TransitTime.spelled``): "32h 20m". Hour and day transits read as
before, and a missing transit says so instead of printing a number.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from tests.unit.test_quotation_compose import packet_for

from translog_quote.domain.quotation import compose_quotation_body, compose_review_body
from translog_quote.domain.rates import Rate, Selection, TransitTime, TransitUnit


def _selection(transit: TransitTime | None) -> Selection:
    rate = Rate(
        carrier_code="AY",
        carrier_name="Finnair",
        product="General Cargo",
        total_amount=Decimal("20920.45"),
        currency="Rs",
        transit=transit,
        departure_date_label="29/10/2026",
    )
    return Selection(rate=rate, reason="Finnair General Cargo — test")


def _quotation(transit: TransitTime | None) -> str:
    return compose_quotation_body(
        packet_for().record, _selection(transit), reference="R-1", is_simulated=False
    )


@pytest.mark.parametrize(
    ("transit", "shown"),
    [
        (TransitTime(value=1940, unit=TransitUnit.MINUTES), "32h 20m"),
        (TransitTime(value=1290, unit=TransitUnit.MINUTES), "21h 30m"),
        (TransitTime(value=1590, unit=TransitUnit.MINUTES), "26h 30m"),
        (TransitTime(value=120, unit=TransitUnit.MINUTES), "2h"),
        (TransitTime(value=45, unit=TransitUnit.MINUTES), "45m"),
        (TransitTime(value=26, unit=TransitUnit.HOURS), "26 hours"),
        (TransitTime(value=1, unit=TransitUnit.DAYS), "1 day"),
        (TransitTime(value=2, unit=TransitUnit.DAYS), "2 days"),
    ],
)
def test_the_client_quotation_spells_the_transit(transit: TransitTime, shown: str) -> None:
    body = _quotation(transit)

    line = next(line for line in body.splitlines() if "Transit time:" in line)
    assert line.split("Transit time:")[1].strip() == shown
    assert "minutes" not in line


def test_a_missing_transit_says_so_rather_than_printing_a_number() -> None:
    line = next(line for line in _quotation(None).splitlines() if "Transit time:" in line)
    assert line.split("Transit time:")[1].strip() == "Not specified"


def test_the_stored_value_is_unchanged() -> None:
    transit = TransitTime(value=1940, unit=TransitUnit.MINUTES)
    _quotation(transit)
    assert (transit.value, transit.unit, transit.minutes) == (1940, TransitUnit.MINUTES, 1940)


def test_the_approver_review_uses_the_same_wording() -> None:
    selection = _selection(TransitTime(value=1940, unit=TransitUnit.MINUTES))
    packet = packet_for().model_copy(update={"selection": selection})

    body = compose_review_body(packet, is_simulated=False)

    assert "Transit : 32h 20m" in body
    assert "1940 minutes" not in body
