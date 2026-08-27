"""Choosing one rate. Stage four of the rate pipeline.

Selection changes order and nothing else. It cannot exclude a rate — anything
that reaches it has already survived filtering — and it cannot reach a rate that
filtering removed.

The comparator chain is data (`SelectionStrategy`), so switching the business
from fastest to cheapest is a reordering of keys rather than a code change
(BR-3).
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from translog_quote.domain.rates.strategy import (
    Selection,
    SelectionStrategy,
    SortField,
    SortOrder,
)

if TYPE_CHECKING:
    from translog_quote.domain.rates.model import Rate

#: Sorts before every real value, so a missing optional never wins by accident.
_ABSENT_NUMBER = Decimal("-1")


def _key_for(rate: Rate, field: SortField) -> object:
    """The comparable magnitude for one sort key.

    Transit compares in hours, never as a raw number: "2 days" and "2 hours"
    are the same integer and a factor of twelve apart. `TransitTime.hours` is
    the single canonical ordering, and nothing here reads `value` directly.
    """
    if field is SortField.TRANSIT:
        # Unrankable rates cannot reach selection — filtering removes them — so
        # a missing transit here would be a programming error, not a thin market.
        assert rate.transit is not None, "unrankable rate reached selection"
        return rate.transit.hours
    if field is SortField.TOTAL_AMOUNT:
        return rate.total_amount if rate.total_amount is not None else _ABSENT_NUMBER
    return rate.carrier_code


def _sort_key(rate: Rate, strategy: SelectionStrategy) -> tuple[object, ...]:
    parts: list[object] = []
    for key in strategy.keys:
        value = _key_for(rate, key.field)
        if key.order is SortOrder.DESC:
            value = _Reversed(value)
        parts.append(value)
    return tuple(parts)


class _Reversed:
    """Inverts one key's ordering without inverting the whole comparison."""

    __slots__ = ("value",)

    def __init__(self, value: object) -> None:
        self.value = value

    def __lt__(self, other: _Reversed) -> bool:
        return bool(other.value < self.value)  # type: ignore[operator]

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Reversed) and bool(self.value == other.value)

    def __hash__(self) -> int:
        return hash(self.value)


def _describe(winner: Rate, strategy: SelectionStrategy) -> str:
    """Why this rate won, generated from the comparison that chose it.

    Generated rather than written, so the explanation shown to the quotation
    maker cannot drift away from the logic that produced it.
    """
    if winner.transit is None:  # pragma: no cover - filtering prevents this
        return f"{winner.carrier_name} {winner.product}"

    lead = strategy.keys[0].field
    if lead is SortField.TRANSIT:
        headline = f"fastest eligible transit at {winner.transit.value} {winner.transit.unit.value}"
    elif lead is SortField.TOTAL_AMOUNT:
        headline = f"lowest eligible total at {winner.total_amount} {winner.currency}"
    else:
        headline = f"first eligible by {lead.value}"

    return f"{winner.carrier_name} {winner.product} — {headline}"


def select_rate(eligible: tuple[Rate, ...], strategy: SelectionStrategy) -> Selection | None:
    """The single best eligible rate, or ``None`` when there are none.

    Exactly one rate is ever returned. Showing several would rebuild the manual
    process this system exists to remove (BR-10).
    """
    if not eligible:
        return None

    ordered = sorted(eligible, key=lambda rate: _sort_key(rate, strategy))
    winner, *runners_up = ordered

    return Selection(
        rate=winner,
        reason=_describe(winner, strategy),
        runners_up=tuple(runners_up),
    )
