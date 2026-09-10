"""The two rules the rate pipeline may not break.

1. **Official data only.** A value the provider did not give us is never
   invented, approximated, or derived from an unrelated field.
2. **Fastest means the shortest actual shipment duration.** Price is a
   tie-break and nothing more.

Both are asserted here against the real domain code rather than described in a
document, so a change that violates either fails a test.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from translog_quote.domain.rates import (
    FASTEST_ELIGIBLE,
    ExclusionReason,
    Rate,
    TransitTime,
    TransitUnit,
    UndeterminableTransit,
    elapsed_transit,
    filter_rates,
    select_rate,
)

IST = timezone(timedelta(hours=5, minutes=30))  # Bangalore
PHT = timezone(timedelta(hours=8))  # Manila


def _rate(
    code: str,
    *,
    transit: TransitTime | None,
    total: str | None = "1000",
) -> Rate:
    """A rate carrying only what the provider stated. No field is defaulted."""
    return Rate(
        carrier_code=code,
        carrier_name=f"{code} Airways",
        product="GEN",
        total_amount=None if total is None else Decimal(total),
        currency=None if total is None else "INR",
        transit=transit,
    )


def _minutes(value: int) -> TransitTime:
    return TransitTime(value=value, unit=TransitUnit.MINUTES)


class TestTimezoneAwareDuration:
    """`elapsed_transit` computes across zones, and refuses when it cannot."""

    def test_uses_airport_timezones_not_displayed_clock_times(self) -> None:
        """The observed WebCargo example: BLR 06:00 IST -> MNL 21:50 next day PHT.

        The correct answer is 37h20m. Subtracting the *displayed* readings
        gives 39h50m — wrong by exactly the 2h30m IST/PHT offset. Asserting
        both pins the rule rather than the arithmetic.
        """
        departure = datetime(2026, 9, 9, 6, 0, tzinfo=IST)
        arrival = datetime(2026, 9, 10, 21, 50, tzinfo=PHT)

        transit = elapsed_transit(departure, arrival)

        assert transit.minutes == 37 * 60 + 20  # 2240
        naive_difference = datetime(2026, 9, 10, 21, 50) - datetime(2026, 9, 9, 6, 0)  # noqa: DTZ001
        assert naive_difference == timedelta(hours=39, minutes=50)
        assert transit.minutes != naive_difference.total_seconds() / 60

    def test_naive_departure_is_refused(self) -> None:
        with pytest.raises(UndeterminableTransit, match="departure"):
            elapsed_transit(
                datetime(2026, 9, 9, 6, 0),  # noqa: DTZ001 - the point of the test
                datetime(2026, 9, 10, 21, 50, tzinfo=PHT),
            )

    def test_naive_arrival_is_refused(self) -> None:
        with pytest.raises(UndeterminableTransit, match="arrival"):
            elapsed_transit(
                datetime(2026, 9, 9, 6, 0, tzinfo=IST),
                datetime(2026, 9, 10, 21, 50),  # noqa: DTZ001 - the point of the test
            )

    def test_arrival_before_departure_is_refused_not_negated(self) -> None:
        with pytest.raises(UndeterminableTransit, match="does not follow"):
            elapsed_transit(
                datetime(2026, 9, 10, 21, 50, tzinfo=PHT),
                datetime(2026, 9, 9, 6, 0, tzinfo=IST),
            )

    def test_minute_precision_survives_into_the_ranking_key(self) -> None:
        """The ranking key keeps minutes; `hours` truncates and is display-only."""
        transit = elapsed_transit(
            datetime(2026, 9, 9, 6, 0, tzinfo=IST),
            datetime(2026, 9, 10, 21, 50, tzinfo=PHT),
        )
        assert transit.unit is TransitUnit.MINUTES
        assert transit.minutes == 2240
        assert transit.hours == 37  # 20 minutes dropped — never ranked on

    def test_units_are_comparable_across_the_enum(self) -> None:
        assert TransitTime(value=1, unit=TransitUnit.DAYS).minutes == 1440
        assert TransitTime(value=24, unit=TransitUnit.HOURS).minutes == 1440
        assert _minutes(1440).minutes == 1440


class TestNothingIsFabricated:
    """Missing provider data stays missing, and says so."""

    def test_absent_transit_stays_none(self) -> None:
        assert _rate("AI", transit=None).transit is None

    def test_rate_without_transit_is_excluded_with_a_reason(self) -> None:
        outcome = filter_rates((_rate("AI", transit=None),))

        assert outcome.eligible == ()
        assert len(outcome.excluded) == 1
        assert outcome.excluded[0].reason is ExclusionReason.UNRANKABLE_NO_TRANSIT
        assert outcome.excluded[0].detail  # the reason is stated, not silent

    def test_a_rate_without_transit_never_reaches_selection(self) -> None:
        outcome = filter_rates((_rate("AI", transit=None),))
        assert select_rate(outcome.eligible, FASTEST_ELIGIBLE) is None

    def test_missing_transit_does_not_block_the_rates_that_have_one(self) -> None:
        """One rate's missing data is that rate's problem, not the search's."""
        outcome = filter_rates((_rate("AI", transit=None), _rate("EK", transit=_minutes(600))))

        selection = select_rate(outcome.eligible, FASTEST_ELIGIBLE)
        assert selection is not None
        assert selection.rate.carrier_code == "EK"


class TestViaIsNotTransit:
    """Routing is not a duration, and the model gives it nowhere to hide."""

    def test_rate_has_no_routing_field_at_all(self) -> None:
        for routing_field in ("via", "route", "itinerary", "stops", "connections"):
            assert routing_field not in Rate.model_fields

    def test_a_routing_string_cannot_be_attached_to_a_rate(self) -> None:
        """`extra="forbid"`, so no adapter can smuggle routing in as transit."""
        with pytest.raises(ValidationError):
            Rate(
                carrier_code="EY",
                carrier_name="Etihad Airways",
                product="GEN",
                via="AUH-MNL",  # type: ignore[call-arg]
            )

    def test_transit_cannot_be_built_from_a_routing_string(self) -> None:
        """`TransitTime` takes an integer and a unit — a route is not coercible."""
        with pytest.raises(ValidationError):
            TransitTime(value="AUH-MNL", unit=TransitUnit.HOURS)  # type: ignore[arg-type]


class TestFastestIsShortestDuration:
    """Least actual shipment duration wins. Price only breaks ties."""

    def test_shortest_duration_wins_even_though_another_rate_is_far_cheaper(self) -> None:
        cheap_and_slow = _rate("QR", transit=_minutes(4320), total="10000")  # 3 days
        dear_and_fast = _rate("EK", transit=_minutes(1500), total="99000")  # 25 h

        selection = select_rate((cheap_and_slow, dear_and_fast), FASTEST_ELIGIBLE)

        assert selection is not None
        assert selection.rate.carrier_code == "EK"
        assert selection.runners_up[0].carrier_code == "QR"

    def test_price_breaks_the_tie_only_when_durations_are_equal(self) -> None:
        dearer = _rate("EY", transit=_minutes(2240), total="90000")
        cheaper = _rate("AI", transit=_minutes(2240), total="37500")

        selection = select_rate((dearer, cheaper), FASTEST_ELIGIBLE)

        assert selection is not None
        assert selection.rate.carrier_code == "AI"

    def test_a_shorter_duration_outranks_a_cheaper_one_by_a_single_minute(self) -> None:
        """Cheapness never overtakes speed, however small the speed margin."""
        faster_by_a_minute = _rate("EK", transit=_minutes(2239), total="99000")
        cheaper = _rate("AI", transit=_minutes(2240), total="1000")

        selection = select_rate((cheaper, faster_by_a_minute), FASTEST_ELIGIBLE)

        assert selection is not None
        assert selection.rate.carrier_code == "EK"

    def test_minutes_decide_where_whole_hours_would_have_tied(self) -> None:
        """37h20m and 37h55m both truncate to 37 hours; they must not tie."""
        faster = _rate("AI", transit=_minutes(2240), total="99000")  # 37h20m
        slower = _rate("EY", transit=_minutes(2275), total="1000")  # 37h55m
        assert faster.transit is not None
        assert slower.transit is not None
        assert faster.transit.hours == slower.transit.hours == 37

        selection = select_rate((slower, faster), FASTEST_ELIGIBLE)

        assert selection is not None
        assert selection.rate.carrier_code == "AI"

    def test_the_stated_reason_quotes_the_winning_duration(self) -> None:
        selection = select_rate((_rate("AI", transit=_minutes(2240)),), FASTEST_ELIGIBLE)

        assert selection is not None
        assert "fastest eligible transit" in selection.reason
        assert "37h 20m" in selection.reason

    def test_strategy_ranks_transit_before_price(self) -> None:
        """The rule is data: guard the order so a config edit cannot invert it."""
        fields = [key.field.value for key in FASTEST_ELIGIBLE.keys]
        assert fields[0] == "transit"
        assert fields.index("total_amount") > fields.index("transit")
