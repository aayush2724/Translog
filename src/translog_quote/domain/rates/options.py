"""The fastest and the cheapest eligible rate, from one candidate set.

Both options are chosen from the *same* filtered tuple by `select_rate`, so
they share every eligibility rule by construction: an option can only be a
rate that survived filtering, and neither can reach a rate the other could not.
No second search runs — the candidates are the ones the provider already
returned.

The cheapest option is withheld, with a reason, whenever prices cannot be
compared honestly: a rate without a price (filtering already removes these, so
that branch is defensive) or rates quoted in different currencies. Ranking 100
USD against 8,000 INR on the bare number would pick a winner by accident, and
no exchange rate is documented anywhere in this project. The fastest option is
unaffected — transit is comparable whatever the currency.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from translog_quote.domain.rates.selection import select_rate
from translog_quote.domain.rates.strategy import (
    CHEAPEST_ELIGIBLE,
    FASTEST_ELIGIBLE,
    RateStrategy,
    Selection,
)

if TYPE_CHECKING:
    from translog_quote.domain.rates.model import Rate


class RateOptions(BaseModel):
    """The two candidate selections an operator chooses between."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fastest: Selection
    cheapest: Selection | None = None
    """``None`` when prices cannot be compared; ``cheapest_unavailable`` says why."""
    cheapest_unavailable: str | None = None

    @property
    def same_rate(self) -> bool:
        """The fastest rate is also the cheapest, so there is one option, not two."""
        return self.cheapest is not None and self.cheapest.rate == self.fastest.rate

    def selection_for(self, strategy: RateStrategy) -> Selection | None:
        """The selection a quotation would be built from under ``strategy``.

        ``None`` only for CHEAPEST when the cheapest could not be determined —
        never a silent substitution of the fastest rate.
        """
        if strategy is RateStrategy.FASTEST:
            return self.fastest
        if strategy is RateStrategy.CHEAPEST:
            return self.cheapest
        return None  # OTHER is a specific rate, not one of the two options


def _cheapest_unavailable(eligible: tuple[Rate, ...]) -> str | None:
    """Why prices in this set cannot be ranked, or ``None`` when they can."""
    if any(rate.total_amount is None or rate.currency is None for rate in eligible):
        return "a rate has no stated price, so the cheapest cannot be determined"
    currencies = sorted({rate.currency for rate in eligible if rate.currency is not None})
    if len(currencies) > 1:
        return (
            f"rates are quoted in different currencies ({', '.join(currencies)}), "
            "so their prices cannot be compared"
        )
    return None


def rate_options(eligible: tuple[Rate, ...]) -> RateOptions | None:
    """Both options over the eligible rates, or ``None`` when there are none."""
    fastest = select_rate(eligible, FASTEST_ELIGIBLE)
    if fastest is None:
        return None
    unavailable = _cheapest_unavailable(eligible)
    if unavailable is not None:
        return RateOptions(fastest=fastest, cheapest=None, cheapest_unavailable=unavailable)
    return RateOptions(fastest=fastest, cheapest=select_rate(eligible, CHEAPEST_ELIGIBLE))
