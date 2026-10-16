"""Grouping equivalent rates into offers for the "Other rates" column.

An offer is one airline + service + price + currency + transit; the same offer
on other departure dates is listed on its card instead of becoming more cards.
A different service, price or transit (transit being the routing evidence a
``Rate`` carries) is a different offer. A "trade-off" is an offer no other
offer matches or beats on both price and transit — a tie is not a trade-off.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from tests.unit import test_web_live as web
from tests.unit.test_rate_strategy_options import _session, only_request

from translog_quote.domain.rates import (
    Rate,
    RateRestrictions,
    TransitTime,
    TransitUnit,
    rate_options,
)
from translog_quote.domain.rates.offers import group_offers, offer_for, other_offers
from translog_quote.interface.web import live_serialize

if TYPE_CHECKING:
    from translog_quote.adapters.email import CollectingEmailSink
    from translog_quote.config import Settings


#: The live-view suite's offline settings and collecting sink, reused as-is.
settings = web.settings
sink = web.sink


def _r(
    code: str,
    service: str,
    amount: str,
    minutes: int,
    departs: str,
    ref: str,
    *,
    currency: str = "Rs",
    name: str | None = None,
) -> Rate:
    return Rate(
        carrier_code=code,
        carrier_name=name or code,
        product=service,
        total_amount=Decimal(amount),
        currency=currency,
        transit=TransitTime(value=minutes, unit=TransitUnit.MINUTES),
        restrictions=RateRestrictions(),
        source_ref=ref,
        departure_date_label=departs,
    )


# The shape of live test 5 (DEL-SIN, 33 rows), reduced to the rows that matter.
FASTEST = _r("QR", "QR General", "32877", 1590, "28/10/2026", "qr#6", name="Qatar Airways")
FASTEST_29 = _r("QR", "QR General", "32877", 1590, "29/10/2026", "qr#19", name="Qatar Airways")
FASTEST_30 = _r("QR", "QR General", "32877", 1590, "30/10/2026", "qr#30", name="Qatar Airways")
CHEAPEST = _r("Finnair", "General Cargo", "19993.15", 1940, "30/10/2026", "ay#28")
FIRST_SAME_FARE = _r("Finnair", "First", "19993.15", 1940, "30/10/2026", "ay#29")
SLOWER_ROUTING = _r("Finnair", "General Cargo", "19993.15", 3380, "29/10/2026", "ay#14")
FIRST_SLOWER = _r("Finnair", "First", "19993.15", 3380, "29/10/2026", "ay#15")
DEARER_FARE = _r("Finnair", "General Cargo", "20920.45", 1940, "29/10/2026", "ay#17")
LIVE = (
    FASTEST,
    SLOWER_ROUTING,
    FIRST_SLOWER,
    DEARER_FARE,
    FASTEST_29,
    CHEAPEST,
    FIRST_SAME_FARE,
    FASTEST_30,
)


def test_every_rate_is_in_exactly_one_offer() -> None:
    groups = group_offers(LIVE)
    members = [rate for group in groups for rate in group]
    assert sorted(r.source_ref for r in members) == sorted(r.source_ref for r in LIVE)


def test_the_same_fare_on_other_days_is_one_offer_on_the_option_card() -> None:
    options = rate_options(LIVE)
    assert options is not None
    assert options.fastest.rate == FASTEST
    assert options.cheapest is not None and options.cheapest.rate == CHEAPEST

    fastest_offer = offer_for(FASTEST, LIVE)

    assert fastest_offer.also_departs == ("29/10/2026", "30/10/2026")
    listed, _ = other_offers(options, LIVE)
    listed_refs = {offer.rate.source_ref for offer in listed}
    assert not listed_refs & {"qr#6", "qr#19", "qr#30", "ay#28"}  # no option, on any day


def test_live_shape_keeps_distinct_services_routings_and_fares_and_calls_none_a_trade_off() -> None:
    """Live test 5 tagged three rates TRADE-OFF that only tied an option. None of
    these offers sits between the fastest and the cheapest, so none is one."""
    listed, total = other_offers(rate_options(LIVE), LIVE)

    assert [offer.rate.source_ref for offer in listed] == [
        "ay#29",  # First, same fare as the cheapest: a different service, kept
        "ay#14",  # slower routing of the cheapest fare (19993.15, 56h20m)
        "ay#15",  # its First counterpart
        "ay#17",  # a different (dearer) fare
    ]
    assert total == 4
    assert not any(offer.tradeoff for offer in listed)


def test_a_rate_strictly_between_the_options_is_a_trade_off_and_a_tie_is_not() -> None:
    fast = _r("QR", "GEN", "30000", 1200, "28/10/2026", "fast")
    cheap = _r("AY", "GEN", "18000", 3000, "28/10/2026", "cheap")
    middle = _r("EK", "GEN", "24000", 1800, "28/10/2026", "middle")
    middle_twin = _r("EK", "PRIORITY", "24000", 1800, "28/10/2026", "twin")  # ties middle
    beaten = _r("SQ", "GEN", "26000", 2400, "28/10/2026", "beaten")  # worse than middle on both
    alone = _r("TK", "GEN", "21000", 2200, "28/10/2026", "alone")

    eligible = (fast, cheap, middle, middle_twin, beaten, alone)
    listed, _ = other_offers(rate_options(eligible), eligible)
    tradeoff = {offer.rate.source_ref: offer.tradeoff for offer in listed}

    assert tradeoff == {"alone": True, "middle": False, "twin": False, "beaten": False}
    assert [offer.rate.source_ref for offer in listed][0] == "alone"  # trade-offs first

    # Without its twin, the middle fare is a genuine trade-off again.
    eligible = (fast, cheap, middle, beaten)
    listed, _ = other_offers(rate_options(eligible), eligible)
    assert {o.rate.source_ref: o.tradeoff for o in listed} == {"middle": True, "beaten": False}


def test_an_other_offer_lists_its_other_dates_and_quotes_the_earliest() -> None:
    fast = _r("QR", "GEN", "30000", 1200, "28/10/2026", "fast")
    cheap = _r("AY", "GEN", "18000", 3000, "28/10/2026", "cheap")
    later = _r("EK", "GEN", "24000", 1800, "31/10/2026", "ek-31")
    earlier = _r("EK", "GEN", "24000", 1800, "29/10/2026", "ek-29")

    eligible = (fast, cheap, later, earlier)
    listed, total = other_offers(rate_options(eligible), eligible)

    assert total == 1
    assert listed[0].rate.source_ref == "ek-29"
    assert listed[0].also_departs == ("31/10/2026",)


def test_two_same_fare_flights_on_one_day_are_one_offer() -> None:
    """Departure time is not on a Rate, so two same-key flights on one date
    cannot be told apart on a card: they are one offer, quoted by the first
    row in the provider's order, and add no extra date."""
    fast = _r("QR", "GEN", "30000", 1200, "28/10/2026", "fast")
    cheap = _r("AY", "GEN", "18000", 3000, "28/10/2026", "cheap")
    morning = _r("EK", "GEN", "24000", 1800, "29/10/2026", "ek-am")
    evening = _r("EK", "GEN", "24000", 1800, "29/10/2026", "ek-pm")

    eligible = (fast, cheap, morning, evening)
    listed, total = other_offers(rate_options(eligible), eligible)

    assert total == 1
    assert listed[0].rate.source_ref == "ek-am"
    assert [rate.source_ref for rate in listed[0].also] == ["ek-pm"]
    assert listed[0].also_departs == ()  # the second flight is on the card's own date
    # Its own second flight is not a rival: it is still a genuine trade-off.
    assert listed[0].tradeoff is True


# Live test 7 (DEL-SIN 150 kg): Qatar's 03:45 and 09:55 flights, same fare and
# 26h 30m transit, on three dates — the fastest offer.
QR_28_AM = _r("QR", "QR General", "35100", 1590, "28/10/2026", "qr#8")
QR_28_PM = _r("QR", "QR General", "35100", 1590, "28/10/2026", "qr#9")
QR_29_AM = _r("QR", "QR General", "35100", 1590, "29/10/2026", "qr#23")
QR_29_PM = _r("QR", "QR General", "35100", 1590, "29/10/2026", "qr#24")
QR_30_AM = _r("QR", "QR General", "35100", 1590, "30/10/2026", "qr#36")
QR_30_PM = _r("QR", "QR General", "35100", 1590, "30/10/2026", "qr#37")
QR_SLOWER = _r("QR", "QR General", "35100", 1960, "28/10/2026", "qr#10")  # 32h 40m routing
AY_CHEAP = _r("Finnair", "General Cargo", "20070", 1940, "31/10/2026", "ay#40")
AY_CHEAP_2ND = _r("Finnair", "General Cargo", "20070", 1940, "31/10/2026", "ay#41")
LIVE_7 = (
    QR_28_AM,
    QR_28_PM,
    QR_SLOWER,
    QR_29_AM,
    QR_29_PM,
    QR_30_AM,
    QR_30_PM,
    AY_CHEAP,
    AY_CHEAP_2ND,
)


def test_a_second_same_day_flight_of_the_fastest_never_appears_as_another_card() -> None:
    options = rate_options(LIVE_7)
    assert options is not None
    assert options.fastest.rate == QR_28_AM  # provider order breaks the full tie

    listed, total = other_offers(options, LIVE_7)

    refs = {offer.rate.source_ref for offer in listed}
    assert not refs & {"qr#8", "qr#9", "qr#23", "qr#24", "qr#36", "qr#37"}
    assert not refs & {"ay#40", "ay#41"}  # the cheapest's second flight neither
    assert refs == {"qr#10"}  # the genuinely different (slower) Qatar routing stays
    assert total == 1
    assert offer_for(QR_28_AM, LIVE_7).also_departs == ("29/10/2026", "30/10/2026")
    assert offer_for(AY_CHEAP, LIVE_7).also_departs == ()


def test_an_offer_with_flights_on_several_days_lists_each_other_date_once() -> None:
    fast = _r("QR", "GEN", "30000", 1200, "28/10/2026", "fast")
    cheap = _r("AY", "GEN", "18000", 3000, "28/10/2026", "cheap")
    rows = (
        _r("EK", "GEN", "24000", 1800, "31/10/2026", "ek-31"),
        _r("EK", "GEN", "24000", 1800, "29/10/2026", "ek-29-a"),
        _r("EK", "GEN", "24000", 1800, "30/10/2026", "ek-30-a"),
        _r("EK", "GEN", "24000", 1800, "29/10/2026", "ek-29-b"),
        _r("EK", "GEN", "24000", 1800, "30/10/2026", "ek-30-b"),
    )
    eligible = (fast, cheap, *rows)

    listed, total = other_offers(rate_options(eligible), eligible)

    assert total == 1
    offer = listed[0]
    # Earliest date, then the provider's order within it: deterministic.
    assert offer.rate.source_ref == "ek-29-a"
    assert offer.also_departs == ("30/10/2026", "31/10/2026")
    # Same answer whatever order the other days arrive in.
    reordered = (fast, cheap, rows[2], rows[0], rows[4], rows[1], rows[3])
    again, _ = other_offers(rate_options(reordered), reordered)
    assert again[0].rate.source_ref == "ek-29-a"
    assert again[0].also_departs == ("30/10/2026", "31/10/2026")


def test_grouped_rows_are_never_rivals_but_a_genuinely_different_offer_is() -> None:
    fast = _r("QR", "GEN", "30000", 1200, "28/10/2026", "fast")
    cheap = _r("AY", "GEN", "18000", 3000, "28/10/2026", "cheap")
    middle_am = _r("EK", "GEN", "24000", 1800, "29/10/2026", "ek-am")
    middle_pm = _r("EK", "GEN", "24000", 1800, "29/10/2026", "ek-pm")
    middle_next_day = _r("EK", "GEN", "24000", 1800, "30/10/2026", "ek-30")
    eligible = (fast, cheap, middle_am, middle_pm, middle_next_day)

    listed, _ = other_offers(rate_options(eligible), eligible)
    assert [(o.rate.source_ref, o.tradeoff) for o in listed] == [("ek-am", True)]

    # A different airline tying it on price and transit does make it a non-trade-off.
    rival = _r("SQ", "GEN", "24000", 1800, "29/10/2026", "sq")
    eligible = (*eligible, rival)
    listed, _ = other_offers(rate_options(eligible), eligible)
    assert {o.rate.source_ref: o.tradeoff for o in listed} == {"ek-am": False, "sq": False}


def test_the_representative_is_what_the_operator_quotes(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.test_rate_strategy_options import APPROVER, CHEAP, FAST, _quotation_body

    # INR like the shared FAST/CHEAP fixtures, so prices are comparable.
    am = _r("EK", "GEN", "24000", 1800, "20/09/2026", "ref:EK-am", currency="INR", name="Emirates")
    pm = _r("EK", "GEN", "24000", 1800, "20/09/2026", "ref:EK-pm", currency="INR", name="Emirates")
    fast_pm = FAST.model_copy(update={"source_ref": "ref:QR-pm"})
    session = _session(settings, sink, monkeypatch, rates=(FAST, fast_pm, CHEAP, am, pm))
    request = only_request(session)

    card = live_serialize.approval_json(
        request.packet,  # type: ignore[attr-defined]
        request.rates,  # type: ignore[attr-defined]
        approver="x",
        options=request.rate_options,  # type: ignore[attr-defined]
        recommended=request.recommended_option,  # type: ignore[attr-defined]
    )
    assert [r["rate_ref"] for r in card["other_rates"]] == ["ref:EK-am"]  # type: ignore[attr-defined]
    assert card["other_rates"][0]["also_departs"] == []  # type: ignore[index]

    decided = session.decide(
        request.request_id,  # type: ignore[attr-defined]
        choice="approve",
        by=APPROVER,
        rate_ref="ref:EK-am",
    )

    assert decided.packet.selection.rate.source_ref == "ref:EK-am"  # type: ignore[union-attr]
    assert "Emirates (EK)" in _quotation_body(sink)


def test_a_different_airline_at_the_same_price_is_kept() -> None:
    fast = _r("QR", "GEN", "30000", 1200, "28/10/2026", "fast")
    cheap = _r("AY", "GEN", "18000", 3000, "28/10/2026", "cheap")
    ek = _r("EK", "GEN", "24000", 1800, "29/10/2026", "ek")
    sq = _r("SQ", "GEN", "24000", 1800, "29/10/2026", "sq")

    eligible = (fast, cheap, ek, sq)
    listed, total = other_offers(rate_options(eligible), eligible)

    assert total == 2
    assert {offer.rate.carrier_code for offer in listed} == {"EK", "SQ"}


def test_offers_are_capped_and_the_total_counts_distinct_offers() -> None:
    fast = _r("QR", "GEN", "40000", 1000, "28/10/2026", "fast")
    cheap = _r("AY", "GEN", "10000", 9000, "28/10/2026", "cheap")
    many = tuple(
        _r(f"C{i}", "GEN", str(20000 + i * 100), 2000 + i * 10, day, f"c{i}-{day}")
        for i in range(12)
        for day in ("28/10/2026", "29/10/2026")
    )
    eligible = (fast, cheap, *many)

    listed, total = other_offers(rate_options(eligible), eligible, limit=8)

    assert len(listed) == 8
    assert total == 12  # 24 rows, 12 distinct offers
    assert all(offer.also_departs == ("29/10/2026",) for offer in listed)


def test_mixed_currencies_rank_by_transit_and_claim_no_trade_off() -> None:
    fast = _r("QR", "GEN", "30000", 1200, "28/10/2026", "fast")
    cheap = _r("AY", "GEN", "18000", 3000, "28/10/2026", "cheap")
    usd = _r("UA", "GEN", "350", 1700, "29/10/2026", "ua", currency="USD")
    ek = _r("EK", "GEN", "24000", 1800, "29/10/2026", "ek")

    eligible = (fast, cheap, usd, ek)
    options = rate_options(eligible)
    assert options is not None and options.cheapest is None

    listed, _ = other_offers(options, eligible)

    # Fastest-first; the cheapest *rate* is not an option here, so it is listed.
    assert [offer.rate.source_ref for offer in listed] == ["ua", "ek", "cheap"]
    assert not any(offer.tradeoff for offer in listed)


def test_no_other_offers_is_empty() -> None:
    fast = _r("QR", "GEN", "30000", 1200, "28/10/2026", "fast")
    fast_tomorrow = _r("QR", "GEN", "30000", 1200, "29/10/2026", "fast-2")
    cheap = _r("AY", "GEN", "18000", 3000, "28/10/2026", "cheap")
    eligible = (fast, fast_tomorrow, cheap)

    assert other_offers(rate_options(eligible), eligible) == ([], 0)
    assert other_offers(None, eligible) == ([], 0)


def test_the_approval_payload_lists_other_dates_on_cards(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.test_rate_strategy_options import CHEAP, FAST, MIDDLE

    fast_later = FAST.model_copy(
        update={"departure_date_label": "22/09/2026", "source_ref": "ref:QR-22"}
    )
    middle_later = MIDDLE.model_copy(
        update={"departure_date_label": "23/09/2026", "source_ref": "ref:EK-23"}
    )
    session = _session(
        settings, sink, monkeypatch, rates=(FAST, CHEAP, MIDDLE, fast_later, middle_later)
    )
    request = only_request(session)

    card = live_serialize.approval_json(
        request.packet,  # type: ignore[attr-defined]
        request.rates,  # type: ignore[attr-defined]
        approver="x",
        options=request.rate_options,  # type: ignore[attr-defined]
        recommended=request.recommended_option,  # type: ignore[attr-defined]
    )

    fastest = next(o for o in card["options"] if o["key"] == "fastest")  # type: ignore[attr-defined]
    assert fastest["also_departs"] == ["22/09/2026"]
    assert [r["rate_ref"] for r in card["other_rates"]] == ["ref:EK"]  # type: ignore[attr-defined]
    assert card["other_rates"][0]["also_departs"] == ["23/09/2026"]  # type: ignore[index]
    assert card["other_rates_total"] == 1
