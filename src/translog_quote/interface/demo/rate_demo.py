"""Validated shipment -> rate search -> fastest eligible rate.

Fully deterministic: no model call, no network. The rate provider is selected by
configuration, and whichever it is, the output says so plainly — fixture data is
never presented as a provider's.
"""

from __future__ import annotations

import datetime
import sys
from typing import TYPE_CHECKING, TextIO

from translog_quote import bootstrap
from translog_quote.config import load_settings
from translog_quote.domain.rates import FASTEST_ELIGIBLE
from translog_quote.domain.shipment import (
    CargoDimensions,
    DeliveryType,
    RequestSource,
    ShipmentRecord,
)
from translog_quote.domain.validation import validate_shipment
from translog_quote.errors import TranslogError
from translog_quote.interface.demo.formatting import RULE, THIN
from translog_quote.pipeline import RateSearchStage

if TYPE_CHECKING:
    from translog_quote.config import Settings

EXIT_OK = 0
EXIT_INVALID = 3
EXIT_SEARCH_FAILED = 4
EXIT_NO_RATE = 5

#: The shipment the clarification demo ends with, carried forward.
DEMO_SHIPMENT = ShipmentRecord(
    request_id="R-DEMO-RATES",
    source=RequestSource.EMAIL,
    origin="Ahmedabad",
    destination="Bahrain",
    weight_kg=500.0,
    dimensions_in=CargoDimensions(length=34, width=24, height=6),
    commodity="POLYISOBUTYLENE ADDITIVE",
    cargo_type="Non Haz",
    is_chemical=True,
    msds_attached=True,
    pcs=20,
    delivery_type=DeliveryType.AIRPORT,
)

#: Stated by the demo, not derived. The canonical record cannot express physical
#: form, and guessing "liquid" from a commodity name would put a wrong carrier on
#: a real quotation (AMB-3).
DEMO_CARGO_IS_LIQUID = True


def run_demo(
    *,
    settings: Settings | None = None,
    on_date: datetime.date | None = None,
    out: TextIO = sys.stdout,
) -> int:
    settings = settings or load_settings()
    # AMB-8: no approved source for the search date exists, so the demo states
    # one rather than letting anything downstream invent it.
    query_date = on_date or datetime.date(2026, 9, 2)

    print(RULE, file=out)
    print("TRANSLOG RATE SEARCH DEMO", file=out)
    print(RULE, file=out)

    provider = bootstrap.build_rate_provider(settings)
    stage = RateSearchStage(provider=provider, strategy=FASTEST_ELIGIBLE)

    validation = validate_shipment(DEMO_SHIPMENT)
    print(f"\nSHIPMENT\n{THIN}", file=out)
    print(f"  {DEMO_SHIPMENT.origin} -> {DEMO_SHIPMENT.destination}", file=out)
    print(
        f"  {DEMO_SHIPMENT.weight_kg:g} kg, {DEMO_SHIPMENT.pcs} pieces, {DEMO_SHIPMENT.commodity}",
        file=out,
    )
    print(f"  validation: {'VALID' if validation.is_valid else 'INVALID'}", file=out)
    if not validation.is_valid:
        print("\n  Refusing to search rates for an incomplete shipment.\n", file=out)
        return EXIT_INVALID

    try:
        outcome = stage.run(
            DEMO_SHIPMENT.request_id,
            DEMO_SHIPMENT,
            on_date=query_date,
            cargo_is_liquid=DEMO_CARGO_IS_LIQUID,
        )
    except TranslogError as exc:
        print(f"\n  RATE SEARCH FAILED: {type(exc).__name__}\n  {exc}\n", file=out)
        return EXIT_SEARCH_FAILED

    source = (
        "MOCK DEMO DATA — fixture rates, no WebCargo request was made"
        if outcome.uses_mock_data
        else f"REAL PROVIDER DATA — {outcome.adapter_id}"
    )
    print(f"\nRATE QUERY\n{THIN}", file=out)
    print(
        f"  {outcome.query.origin_iata} -> {outcome.query.destination_iata}  "
        f"{outcome.query.weight_kg:g} kg  on {outcome.query.date}",
        file=out,
    )
    print(f"  cargo declared liquid: {DEMO_CARGO_IS_LIQUID}  (stated by the demo, AMB-3)", file=out)
    print(f"  source: {source}", file=out)

    print(f"\nRATES RETURNED ({outcome.returned})\n{THIN}", file=out)
    for r in (*outcome.filtered.eligible, *(e.rate for e in outcome.filtered.excluded)):
        transit = f"{r.transit.value} {r.transit.unit.value}" if r.transit else "—"
        total = f"{r.total_amount} {r.currency}" if r.total_amount else "—"
        print(f"  {r.carrier_code:<4} {r.carrier_name:<20} {total:>16}  {transit:>8}", file=out)

    print(f"\nELIGIBLE ({len(outcome.filtered.eligible)})\n{THIN}", file=out)
    for r in outcome.filtered.eligible:
        print(f"  {r.carrier_code:<4} {r.carrier_name}", file=out)

    print(f"\nREJECTED ({len(outcome.filtered.excluded)})\n{THIN}", file=out)
    for e in outcome.filtered.excluded:
        print(f"  {e.rate.carrier_code:<4} {e.reason.value:<24} {e.detail}", file=out)

    if outcome.selection is None:
        print(f"\nRESULT\n{THIN}\n  NO ELIGIBLE RATE  ({outcome.state.value})", file=out)
        print(RULE, file=out)
        return EXIT_NO_RATE

    chosen = outcome.selection.rate
    print(f"\nSELECTED — FASTEST ELIGIBLE\n{THIN}", file=out)
    print(f"  Carrier      {chosen.carrier_name} ({chosen.carrier_code})", file=out)
    print(f"  Service      {chosen.product}", file=out)
    print(f"  Transit      {chosen.transit.value} {chosen.transit.unit.value}", file=out)  # type: ignore[union-attr]
    print(f"  Total        {chosen.total_amount} {chosen.currency}", file=out)
    print(f"  Why          {outcome.selection.reason}", file=out)
    cheaper = [
        r
        for r in outcome.selection.runners_up
        if r.total_amount is not None
        and chosen.total_amount is not None
        and r.total_amount < chosen.total_amount
    ]
    if cheaper:
        print(
            f"\n  Note: {len(cheaper)} eligible rate(s) cost less and were not chosen —"
            "\n  the ranking criterion is transit time, not price.",
            file=out,
        )
    print(f"\n  state: {outcome.state.value}", file=out)
    print(RULE, file=out)
    return EXIT_OK
