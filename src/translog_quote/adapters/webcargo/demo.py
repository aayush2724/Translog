"""DemoRateProvider — simulated WebCargo rates for the client-facing demo.

**This adapter never contacts WebCargo, and its rates are invented.** They are
priced from the shipment in front of it so a demo tells a coherent story, but
they are not market data and must never be presented as live rates. Every
result it returns is flagged ``is_simulated=True``.

It is deliberately shaped like the real adapter will be:

    real:  transport.get(...)      -> mapper.map_rows(payload) -> Rate[]
    demo:  simulate_response(...)  -> map_rows(payload)        -> Rate[]

Same two stages, same intermediate payload, same mapping step. When the
WebCargo partner contract arrives, only the first stage is replaced — the
mapping, the filtering, the ranking and the selection are already the ones
production will use. That is the point of building the demo this way rather
than returning ``Rate`` objects directly.

**What is simulated, and what is real behaviour:**

- *Real behaviour*: air freight is priced per kilo of **chargeable weight**,
  the greater of gross and volumetric weight. A light, bulky shipment costs
  more here than its scale weight suggests, exactly as it would with a carrier.
- *Simulated*: the carrier roster, their per-kilo rates, and their transit
  times. Fixed and documented, not modelled from any market.

Prices deliberately do **not** vary by lane. Inventing lane pricing would mean
modelling a market this project has no data for — the same refusal
``MockWebCargoAdapter`` makes. Weight is the one input that genuinely drives an
air rate, so weight is the one input that drives these.

The carrier set reproduces the roster in ``docs/architecture.md`` §13, so the
demo still demonstrates all three exclusion reasons and still ends on the
counter-intuitive result: the winner is the *most expensive* survivor, because
BR-1 ranks on transit and the cheapest option is excluded by a hard filter.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any

from translog_quote.domain.rates import (
    Rate,
    RateRestrictions,
    RateSearchResult,
    TransitTime,
    TransitUnit,
)
from translog_quote.domain.shipment import IATA_AIR_KG_PER_CBM, chargeable_weight
from translog_quote.errors import ContractViolation

if TYPE_CHECKING:
    from translog_quote.domain.rates import RateQuery

ADAPTER_ID = "demo-webcargo"

#: The disclosure carried on every payload this adapter produces. It lives in
#: the raw payload so that an audit trail reconstructed later still says, in
#: the data itself, that no WebCargo request was made.
DISCLOSURE = "SIMULATED WEBCARGO DATA — DEMO ONLY. No WebCargo request was made."

#: The dimensional factor the demo prices with. A stated assumption, not a
#: resolved business rule: the real ratio is a commercial term per carrier
#: agreement (see `domain.shipment.weight`). Named here so the demo's pricing
#: is reproducible and inspectable rather than buried in a calculation.
DEMO_KG_PER_CBM = IATA_AIR_KG_PER_CBM


@dataclass(frozen=True, slots=True)
class _DemoCarrier:
    """One simulated carrier's standing offer.

    ``inr_per_kg`` is a demo constant. At the reference shipment's 500 kg
    chargeable weight these reproduce the exact figures in the architecture
    document, so the familiar demo numbers still appear — and they now scale
    with the shipment instead of ignoring it.
    """

    code: str
    name: str
    product: str
    inr_per_kg: str | None  # None models a carrier that returned no price
    transit_days: int | None  # None models a carrier that returned no transit
    accepts_liquids: bool | None = None


#: Six carriers, three of which must be excluded for three different reasons.
DEMO_CARRIERS: tuple[_DemoCarrier, ...] = (
    # Fastest AND cheapest — and excluded for liquids, which is what proves
    # that filtering runs before ranking rather than after it.
    _DemoCarrier("TK", "Turkish Cargo", "GEN", "33.80", 1, accepts_liquids=False),
    # No price: not an offer (BR-4).
    _DemoCarrier("HY", "Uzbekistan Airways", "HY-250", None, 3),
    # No transit: unrankable under BR-1.
    _DemoCarrier("UL", "SriLankan Cargo", "GEN", "34.40", None),
    _DemoCarrier("EY", "Etihad Airways", "General Cargo", "36.68", 4, accepts_liquids=False),
    _DemoCarrier("QR", "Qatar Airways", "GEN", "39.76", 3, accepts_liquids=True),
    # The most expensive survivor, and the fastest eligible one. Wins on BR-1.
    _DemoCarrier("EK", "Emirates", "GEN", "41.52420", 2, accepts_liquids=True),
)


def simulate_response(query: RateQuery) -> dict[str, Any]:
    """Build the payload a WebCargo-like provider would return for this query.

    Stage one of two. Returns plain JSON-shaped data — dicts, strings, numbers
    — and no domain types, because that is what a real HTTP response is. The
    mapping into ``Rate`` happens in ``map_rows``.
    """
    weights = chargeable_weight(
        gross_kg=query.weight_kg,
        dimensions=query.dimensions_in,
        kg_per_cbm=DEMO_KG_PER_CBM,
    )

    rows: list[dict[str, Any]] = []
    for carrier in DEMO_CARRIERS:
        total = (
            None
            if carrier.inr_per_kg is None
            else str(
                (Decimal(carrier.inr_per_kg) * Decimal(str(weights.chargeable_kg))).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            )
        )
        rows.append(
            {
                "carrierCode": carrier.code,
                "airline": carrier.name,
                "product": carrier.product,
                "total": total,
                "currency": None if total is None else "INR",
                "transitDays": carrier.transit_days,
                "acceptsLiquids": carrier.accepts_liquids,
            }
        )

    return {
        "disclosure": DISCLOSURE,
        "origin": query.origin_iata,
        "destination": query.destination_iata,
        "chargeableWeightKg": round(weights.chargeable_kg, 2),
        "chargeableWeightBasis": weights.basis.value,
        "grossWeightKg": weights.gross_kg,
        "volumetricWeightKg": round(weights.volumetric_kg, 2),
        "volumetricRatioKgPerCbm": weights.kg_per_cbm,
        "rates": rows,
    }


def map_rows(payload: dict[str, Any]) -> tuple[Rate, ...]:
    """Stage two: the simulated payload into normalised ``Rate`` values.

    Mirrors ``RealRateMapper``'s contract: it never drops a row and never
    reorders. A row missing a price or a transit becomes a ``Rate`` with null
    fields, and the *filter* excludes it later with a reason — so the demo
    shows the same exclusion behaviour production will.

    This is a mapper for the **simulated** shape. It is deliberately separate
    from ``RealRateMapper``, whose ``map_transit`` still refuses because the
    real WebCargo transit field is unverified (AMB-1). Nothing here weakens
    that: a shape we invented is one we are allowed to read.
    """
    rows = payload.get("rates")
    if not isinstance(rows, list):
        raise ContractViolation("simulated payload has no 'rates' list")

    rates: list[Rate] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ContractViolation(f"rate row was {type(row).__name__}, expected an object")

        total = row.get("total")
        transit_days = row.get("transitDays")
        accepts = row.get("acceptsLiquids")
        currency = row.get("currency")

        rates.append(
            Rate(
                carrier_code=str(row["carrierCode"]),
                carrier_name=str(row["airline"]),
                product=str(row["product"]),
                total_amount=None if total is None else Decimal(str(total)),
                currency=currency if isinstance(currency, str) else None,
                transit=None
                if not isinstance(transit_days, int)
                else TransitTime(value=transit_days, unit=TransitUnit.DAYS),
                restrictions=RateRestrictions(
                    accepts_liquids=accepts if isinstance(accepts, bool) else None
                ),
                source_ref=f"{ADAPTER_ID}:{row['carrierCode']}",
            )
        )

    return tuple(rates)


class DemoRateProvider:
    """A `RateSearchPort` returning simulated rates priced for the shipment.

    Satisfies the same port as the mock and the eventual real adapter, so the
    quotation workflow above it is identical in all three cases.
    """

    adapter_id = ADAPTER_ID

    def search(self, query: RateQuery) -> RateSearchResult:
        payload = simulate_response(query)
        return RateSearchResult(
            rates=map_rows(payload),
            adapter_id=ADAPTER_ID,
            raw_payload=payload,
            is_simulated=True,
        )
