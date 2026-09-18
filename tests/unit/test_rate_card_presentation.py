"""Regression coverage for the two rate-card presentation bugs.

Bug A: the live view marked the SELECTED card by carrier_code, so every rate
from the winning carrier lit up SELECTED and inherited the winner's reason. The
fix serialises `source_ref` — the adapter's per-rate identity — so the browser
can mark exactly one card. This proves the field is present and unique.

Bug B: a live rate carries its transit in minutes, and the old formatter printed
the raw value ("1110 minutes") while the selection reason said "18h 30m". The
fix routes both through `TransitTime.spelled`, so the card chip and the reason
cannot disagree. This proves the spelling and the agreement.

The DOM-level proof that exactly one card is marked lives in the JavaScript
harness (`tests/js/live_ui.test.js`); these tests pin the data contract and the
shared formatter that the harness relies on.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from translog_quote.domain.rates import FASTEST_ELIGIBLE, select_rate
from translog_quote.domain.rates.model import (
    FilterOutcome,
    LocationRef,
    Rate,
    RateQuery,
    TransitTime,
    TransitUnit,
)
from translog_quote.domain.shipment import CargoDimensions
from translog_quote.domain.workflow import RequestState
from translog_quote.interface.demo.formatting import render_transit
from translog_quote.interface.web.live_serialize import rates_json
from translog_quote.pipeline import RateSearchOutcome

DIMS = CargoDimensions(length=34, width=24, height=6)


def _minutes(value: int) -> TransitTime:
    return TransitTime(value=value, unit=TransitUnit.MINUTES)


def _rate(*, source_ref: str, minutes: int, total: str) -> Rate:
    """A Qatar rate. Same carrier every time; only source_ref and transit vary,
    which is exactly the shape that made every QR card read SELECTED."""
    return Rate(
        carrier_code="QR",
        carrier_name="Qatar Airways",
        product="QR General",
        total_amount=Decimal(total),
        currency="Rs",
        transit=_minutes(minutes),
        source_ref=source_ref,
    )


def _outcome(eligible: tuple[Rate, ...]) -> RateSearchOutcome:
    selection = select_rate(eligible, FASTEST_ELIGIBLE)
    query = RateQuery(
        origin=LocationRef(stated="Bangalore"),
        destination=LocationRef(stated="Manila"),
        weight_kg=320.0,
        dimensions_in=DIMS,
        pieces=1,
        date=date(2026, 9, 25),
    )
    return RateSearchOutcome(
        request_id="R-1",
        state=RequestState.RATE_SELECTED,
        query=query,
        adapter_id="webcargo-browser",
        returned=len(eligible),
        filtered=FilterOutcome(eligible=eligible),
        selection=selection,
        is_simulated=False,
    )


# --- Bug B: the shared spelling ------------------------------------------


def test_minutes_are_spelled_as_hours_and_minutes() -> None:
    assert _minutes(1110).spelled == "18h 30m"
    assert _minutes(90).spelled == "1h 30m"
    assert _minutes(45).spelled == "45m"
    assert _minutes(120).spelled == "2h"


def test_other_units_keep_their_wording() -> None:
    assert TransitTime(value=2, unit=TransitUnit.DAYS).spelled == "2 days"
    assert TransitTime(value=1, unit=TransitUnit.DAYS).spelled == "1 day"
    assert TransitTime(value=5, unit=TransitUnit.HOURS).spelled == "5 hours"


def test_render_transit_delegates_to_the_shared_spelling() -> None:
    assert render_transit(_minutes(1110)) == "18h 30m"
    assert render_transit(_minutes(45)) == "45m"
    assert render_transit(None) == "—"


# --- Bug A: per-rate identity in the serialised rates --------------------


def test_serialised_rates_carry_source_ref() -> None:
    winner = _rate(
        source_ref="webcargo-browser:25/09:QR General:06:00", minutes=1110, total="185597"
    )
    other = _rate(
        source_ref="webcargo-browser:25/09:QR General:22:00", minutes=1350, total="90000"
    )

    payload = rates_json(_outcome((winner, other)))

    refs = [row["source_ref"] for row in payload["eligible"]]
    assert refs == [
        "webcargo-browser:25/09:QR General:06:00",
        "webcargo-browser:25/09:QR General:22:00",
    ]
    # The two share a carrier but not an identity — matching on carrier_code
    # would have flagged both; source_ref distinguishes them.
    assert len({str(ref) for ref in refs}) == 2
    assert payload["selection"]["source_ref"] == winner.source_ref


def test_only_the_winner_source_ref_matches_the_selection() -> None:
    """The identity the browser tests each card against picks out exactly one."""
    winner = _rate(source_ref="ref-fast", minutes=1110, total="185597")
    other = _rate(source_ref="ref-slow", minutes=1350, total="90000")

    payload = rates_json(_outcome((winner, other)))
    selected_ref = payload["selection"]["source_ref"]

    matches = [row for row in payload["eligible"] if row["source_ref"] == selected_ref]
    assert len(matches) == 1
    assert matches[0]["source_ref"] == "ref-fast"


# --- Bug A + B together: the chip agrees with the reason -----------------


def test_the_winner_chip_wording_agrees_with_the_selection_reason() -> None:
    winner = _rate(source_ref="ref-fast", minutes=1110, total="185597")
    other = _rate(source_ref="ref-slow", minutes=1350, total="90000")

    payload = rates_json(_outcome((winner, other)))
    selection = payload["selection"]

    # The chip on the winning card and the wording inside its reason are the
    # same spelling of the same duration — never "1110 minutes" beside "18h 30m".
    assert selection["transit"] == "18h 30m"
    assert selection["transit"] in selection["reason"]
    assert "minutes" not in selection["reason"]
