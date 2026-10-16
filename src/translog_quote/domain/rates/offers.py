"""Equivalent offers, grouped — what the "Other rates" column lists.

WebCargo returns one row per flight per date tab, so the same fare (same
airline, same service, same price, same transit) appears once for every day
it runs. Shown as separate cards, a 33-row search fills the column with
copies. Here those rows become one *offer* with its other departure dates
listed, and only genuinely different offers get their own card.

What makes two rates the same offer is decided from what a ``Rate`` actually
carries (see ``adapters/webcargo/browser/mapper.py``): airline code, service
(``product``), price, currency and transit. Routing ("Via") and departure
time are not on a ``Rate``; transit is the evidence of routing that is, so a
different transit is a different offer.

Rows with the same key are one offer *whatever their date or time*, including
two flights on the same day. Live data showed why: Qatar runs 03:45 and 09:55
departures at the same fare and the same 26h 30m transit, and with the time
absent from the rate the second flight rendered as a card identical to the
Fastest one. Nothing quoted to the client differs between such rows.

Known limitation, by design of the data: because departure time is not on a
``Rate``, the rows in one offer cannot be told apart, so one of them is chosen
deterministically to represent it — the earliest date tab, then the provider's
own row order (WebCargo lists a day's flights in departure order). Selecting
the card quotes that row; a second same-day flight is not separately
selectable. Departure times are never inferred. Showing them would need the
WebCargo adapter to carry the time onto ``Rate`` (a worker change).

Pure and deterministic: grouping never changes membership (every eligible
rate is in exactly one offer), never invents a rate, and orders offers by
the first time they appear in the provider's own order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from decimal import Decimal

    from translog_quote.domain.rates.model import Rate
    from translog_quote.domain.rates.options import RateOptions

#: How many offers the "Other rates" column lists.
OTHER_OFFERS_LIMIT = 8


@dataclass(frozen=True, slots=True)
class Offer:
    """One distinct offer: the rate a card quotes, and the same offer on other days."""

    rate: Rate
    """The representative — what selecting the card quotes."""
    also: tuple[Rate, ...] = ()
    """The other rows of the same offer — other dates, and other flights on the
    same date — earliest first. Listed, not separately selectable: picking the
    card quotes ``rate``."""
    tradeoff: bool = False
    """No other offer is at least as cheap *and* at least as fast. A tie on
    both counts as beaten (see `_weakly_dominated`)."""

    @property
    def also_departs(self) -> tuple[str, ...]:
        """The other dates this offer departs, each once, never the card's own
        date (a second flight that day adds no date to show)."""
        seen = {self.rate.departure_date_label}
        days: list[str] = []
        for rate in self.also:
            label = rate.departure_date_label
            if label and label not in seen:
                seen.add(label)
                days.append(label)
        return tuple(days)


def _offer_key(rate: Rate) -> tuple[object, ...]:
    """What makes two rows the same offer. Departure date is deliberately absent."""
    return (
        rate.carrier_code,
        rate.product,
        rate.total_amount,
        rate.currency,
        rate.transit.minutes if rate.transit is not None else None,
    )


def _departure(rate: Rate) -> date | None:
    """The date tab as a date, when it is WebCargo's dd/mm/yyyy; else None."""
    try:
        # A calendar date with no time of day: no zone applies.
        return datetime.strptime(rate.departure_date_label.strip(), "%d/%m/%Y").date()  # noqa: DTZ007
    except ValueError:
        return None


def group_offers(eligible: tuple[Rate, ...]) -> list[list[Rate]]:
    """Every eligible rate in exactly one group of equivalent rates.

    One group per offer key, whatever the date or the (unrecorded) time of
    departure — so two same-day flights at the same fare and transit are one
    group. Groups, and the rows inside them, keep the provider's order.
    """
    groups: dict[tuple[object, ...], list[Rate]] = {}
    for rate in eligible:
        groups.setdefault(_offer_key(rate), []).append(rate)
    return list(groups.values())


def _by_departure(rates: list[Rate]) -> list[Rate]:
    """Earliest departure first; unparseable dates keep their order, after."""
    dated = [(i, rate, _departure(rate)) for i, rate in enumerate(rates)]
    dated.sort(key=lambda item: (item[2] is None, item[2] or date.min, item[0]))
    return [rate for _, rate, _ in dated]


def _price_transit(rate: Rate) -> tuple[Decimal, int]:
    assert rate.total_amount is not None and rate.transit is not None  # noqa: S101 - eligible
    return rate.total_amount, rate.transit.minutes


def _weakly_dominated(rate: Rate, rivals: list[Rate]) -> bool:
    """Some rival offer is at least as cheap *and* at least as fast.

    Weak, on purpose: an offer that only *ties* another on both price and
    transit is not a trade-off — it is an alternative to that offer, not a
    middle ground between the fastest and the cheapest.
    """
    price, minutes = _price_transit(rate)
    for rival in rivals:
        rival_price, rival_minutes = _price_transit(rival)
        if rival_price <= price and rival_minutes <= minutes:
            return True
    return False


def offer_for(rate: Rate, eligible: tuple[Rate, ...]) -> Offer:
    """The offer a given rate represents: the rate itself, plus its other dates.

    For the fastest and cheapest cards, whose representative is the rate the
    domain selected rather than the earliest date.
    """
    for group in group_offers(eligible):
        if rate in group:
            others = [member for member in group if member != rate]
            return Offer(rate=rate, also=tuple(_by_departure(others)))
    return Offer(rate=rate)


def other_offers(
    options: RateOptions | None, eligible: tuple[Rate, ...], *, limit: int = OTHER_OFFERS_LIMIT
) -> tuple[list[Offer], int]:
    """Up to ``limit`` offers other than the fastest and the cheapest, and how many exist.

    Never the fastest or cheapest *offer* (on any of its dates — those dates
    are listed on the option's own card), never the same offer twice.
    Trade-offs first, then the rest by price and transit. When prices cannot
    be compared (the cheapest is undeterminable, e.g. mixed currencies)
    nothing is ranked by price and nothing is called a trade-off: offers keep
    fastest-first order.
    """
    if options is None:
        return [], 0
    groups = group_offers(eligible)
    chosen = [options.fastest.rate]
    if options.cheapest is not None:
        chosen.append(options.cheapest.rate)

    offers: list[Offer] = []
    for group in groups:
        if any(option in group for option in chosen):
            continue
        ordered = _by_departure(group)
        offers.append(Offer(rate=ordered[0], also=tuple(ordered[1:])))

    def by_transit(offer: Offer) -> tuple[int, str]:
        transit = offer.rate.transit
        return (transit.minutes if transit is not None else 0, offer.rate.carrier_code)

    if options.cheapest is None:
        return sorted(offers, key=by_transit)[:limit], len(offers)

    # One representative per group, the option offers included, and never the
    # offer's own group: an offer is judged only against genuinely different
    # offers — never against its own rows on another day or another same-day
    # flight, which share its key and would otherwise "tie" it out of being a
    # trade-off.
    representatives = [group[0] for group in groups]
    marked = [
        Offer(
            rate=offer.rate,
            also=offer.also,
            tradeoff=not _weakly_dominated(
                offer.rate,
                [rival for rival in representatives if _offer_key(rival) != _offer_key(offer.rate)],
            ),
        )
        for offer in offers
    ]
    marked.sort(key=lambda offer: (not offer.tradeoff, offer.rate.total_amount, *by_transit(offer)))
    return marked[:limit], len(marked)
