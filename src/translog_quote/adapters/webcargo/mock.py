"""MockWebCargoAdapter — deterministic demo rates. NOT WebCargo data.

**This adapter never contacts WebCargo.** Every rate it returns is invented
fixture data, written to exercise the filter and selection rules. It exists
because the project specification requires a mock adapter for the demo, and
because the real integration is blocked (see `real.py`).

It reports `adapter_id="mock-webcargo"` so that anything displaying a result can
say plainly where the numbers came from. Nothing downstream may treat these as
real rates.

The fixture is the one documented in `docs/architecture.md` §13, built so that
ranking on price gives the wrong answer: the winner is the *most expensive*
survivor, and the cheapest, fastest option overall is excluded by a hard filter.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from translog_quote.domain.rates import (
    Rate,
    RateRestrictions,
    RateSearchResult,
    TransitTime,
    TransitUnit,
)

if TYPE_CHECKING:
    from translog_quote.domain.rates import RateQuery

ADAPTER_ID = "mock-webcargo"

_DAYS = TransitUnit.DAYS


def _rate(
    code: str,
    name: str,
    product: str,
    total: str | None,
    transit_days: int | None,
    *,
    accepts_liquids: bool | None = None,
) -> Rate:
    return Rate(
        carrier_code=code,
        carrier_name=name,
        product=product,
        total_amount=Decimal(total) if total is not None else None,
        currency="INR" if total is not None else None,
        transit=TransitTime(value=transit_days, unit=_DAYS) if transit_days else None,
        restrictions=RateRestrictions(accepts_liquids=accepts_liquids),
        source_ref=f"{ADAPTER_ID}:{code}",
    )


#: Six rates, three of which must be excluded for three different reasons.
DEMO_RATES: tuple[Rate, ...] = (
    # Fastest AND cheapest overall — and excluded, which is what proves that
    # filtering runs before ranking rather than after it.
    _rate("TK", "Turkish Cargo", "GEN", "16900.00", 1, accepts_liquids=False),
    # No price: not an offer (BR-4).
    _rate("HY", "Uzbekistan Airways", "HY-250", None, 3),
    # No transit: unrankable under BR-1.
    _rate("UL", "SriLankan Cargo", "GEN", "17200.00", None),
    _rate("EY", "Etihad Airways", "General Cargo", "18340.00", 4, accepts_liquids=False),
    _rate("QR", "Qatar Airways", "GEN", "19880.00", 3, accepts_liquids=True),
    # The most expensive survivor, and the fastest eligible one. Wins on BR-1.
    _rate("EK", "Emirates", "GEN", "20762.10", 2, accepts_liquids=True),
)


class MockWebCargoAdapter:
    """A `RateSearchPort` returning fixture rates. Makes no network call."""

    adapter_id = ADAPTER_ID

    def __init__(self, rates: tuple[Rate, ...] = DEMO_RATES) -> None:
        self._rates = rates

    def search(self, query: RateQuery) -> RateSearchResult:
        """The same rates for every query.

        Deliberately query-independent: varying fixture rates by lane would
        invent market behaviour, and this adapter's job is to exercise the
        pipeline, not to model a market.
        """
        return RateSearchResult(
            rates=self._rates,
            adapter_id=ADAPTER_ID,
            raw_payload={"note": "fixture data; no WebCargo request was made"},
        )
