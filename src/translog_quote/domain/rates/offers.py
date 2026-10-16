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
different transit is a different offer. Two same-key rows on the *same* date
are kept apart too — they may be different flights or routings that day, and
nothing on the rate can tell them apart, so merging them would hide one.

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
    """The same offer departing on other dates, earliest first. Listed, not
    separately selectable: picking the card quotes ``rate``."""
    tradeoff: bool = False
    """No other offer is at least as cheap *and* at least as fast. A tie on
    both counts as beaten (see `_weakly_dominated`)."""

    @property
    def also_departs(self) -> tuple[str, ...]:
        return tuple(rate.departure_date_label for rate in self.also if rate.departure_date_label)


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

    A rate joins the first earlier group with the same key that does not
    already have a rate on its date; otherwise it starts a new group. Groups
    keep the provider's order of first appearance.
    """
    groups: list[list[Rate]] = []
    for rate in eligible:
        key = _offer_key(rate)
        for group in groups:
            if _offer_key(group[0]) == key and all(
                member.departure_date_label != rate.departure_date_label for member in group
            ):
                group.append(rate)
                break
        else:
            groups.append([rate])
    return groups


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

    # One representative per group, the option offers included, so an offer is
    # judged only against genuinely different offers — never against itself on
    # another day, nor against a second same-day flight with the same key.
    representatives = [group[0] for group in groups]
    marked = [
        Offer(
            rate=offer.rate,
            also=offer.also,
            tradeoff=not _weakly_dominated(
                offer.rate,
                [rep for rep in representatives if _offer_key(rep) != _offer_key(offer.rate)],
            ),
        )
        for offer in offers
    ]
    marked.sort(key=lambda offer: (not offer.tradeoff, offer.rate.total_amount, *by_transit(offer)))
    return marked[:limit], len(marked)
