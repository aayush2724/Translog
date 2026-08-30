"""The demo rate provider: simulated rates, priced for the actual shipment.

Two things matter here. First, that the rates respond to the shipment being
quoted rather than ignoring it — that is the whole difference from the fixture
adapter. Second, and more important, that nothing anywhere can present them as
live provider rates.

Everything below runs offline. The provider makes no network call and has no
way to make one.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

import pytest

from translog_quote.adapters.routing import StatedLocationResolver
from translog_quote.adapters.webcargo import (
    DISCLOSURE,
    DemoRateProvider,
    map_rows,
    simulate_response,
)
from translog_quote.domain.rates import (
    FASTEST_ELIGIBLE,
    ExclusionReason,
    LocationRef,
    RateQuery,
    RateSearchResult,
    filter_rates,
    select_rate,
)
from translog_quote.domain.shipment import CargoDimensions, RequestSource, ShipmentRecord
from translog_quote.errors import ContractViolation
from translog_quote.pipeline import RateSearchStage

WHEN = datetime.date(2026, 9, 15)

#: The reference shipment: dense, so it charges on gross weight.
REFERENCE = RateQuery(
    origin=LocationRef(stated="Ahmedabad"),
    destination=LocationRef(stated="Bahrain"),
    weight_kg=500.0,
    dimensions_in=CargoDimensions(length=34, width=24, height=6),
    date=WHEN,
)

#: Same lane, light and bulky — charges on volumetric weight instead.
BULKY = RateQuery(
    origin=LocationRef(stated="Ahmedabad"),
    destination=LocationRef(stated="Bahrain"),
    weight_kg=30.0,
    dimensions_in=CargoDimensions(length=48, width=40, height=40),
    date=WHEN,
)


def search(query: RateQuery = REFERENCE) -> RateSearchResult:
    return DemoRateProvider().search(query)


# --- disclosure: the rule that must never break ---------------------------------


def test_every_result_declares_itself_simulated() -> None:
    assert search().is_simulated is True


def test_the_adapter_id_names_it_as_demo_data() -> None:
    assert DemoRateProvider().adapter_id == "demo-webcargo"
    assert search().adapter_id == "demo-webcargo"


def test_the_payload_itself_carries_the_disclosure() -> None:
    """An audit trail reconstructed later must still say, in the data, that no
    WebCargo request was made."""
    payload = search().raw_payload

    assert payload["disclosure"] == DISCLOSURE
    assert "SIMULATED" in payload["disclosure"]
    assert "No WebCargo request was made" in payload["disclosure"]


def test_the_pipeline_reports_it_as_simulated_not_as_provider_data() -> None:
    """The regression this guards: provenance used to be inferred from the
    adapter's *name* (`startswith("mock")`), which would have labelled
    `demo-webcargo` as real provider data and dropped the warning flag."""
    record = ShipmentRecord(
        request_id="R-DEMO",
        source=RequestSource.EMAIL,
        origin="Ahmedabad",
        destination="Bahrain",
        weight_kg=500.0,
        dimensions_in=CargoDimensions(length=34, width=24, height=6),
    )
    outcome = RateSearchStage(provider=DemoRateProvider(), resolver=StatedLocationResolver()).run(
        "R-DEMO", record, on_date=WHEN, cargo_is_liquid=False
    )

    assert outcome.is_simulated is True
    assert outcome.uses_mock_data is True  # "do not present as live"


# --- responsive to the shipment -------------------------------------------------


def test_prices_are_computed_from_the_shipment_not_fixed() -> None:
    """The difference from the fixture adapter: double the weight, double the
    price."""
    heavier = REFERENCE.model_copy(update={"weight_kg": 1000.0})

    at_500 = search(REFERENCE).rates[0].total_amount
    at_1000 = search(heavier).rates[0].total_amount

    assert at_500 is not None and at_1000 is not None
    assert at_1000 == pytest.approx(float(at_500) * 2)


def test_the_reference_shipment_reproduces_the_documented_figures() -> None:
    """At 500 kg chargeable the demo still shows the numbers the architecture
    document uses, so the familiar walkthrough is unchanged."""
    by_carrier = {r.carrier_code: r.total_amount for r in search(REFERENCE).rates}

    assert by_carrier["TK"] == Decimal("16900.00")
    assert by_carrier["EK"] == Decimal("20762.10")
    assert by_carrier["HY"] is None  # no price returned


def test_light_bulky_cargo_is_priced_on_volumetric_weight() -> None:
    """30 kg in a 48x40x40 inch carton bills as ~210 kg. Pricing on scale
    weight would under-quote it sevenfold."""
    payload = search(BULKY).raw_payload

    assert payload["chargeableWeightBasis"] == "volumetric"
    assert payload["chargeableWeightKg"] > payload["grossWeightKg"]
    assert payload["chargeableWeightKg"] == pytest.approx(210, abs=5)


def test_the_payload_shows_its_working() -> None:
    payload = search(REFERENCE).raw_payload

    assert payload["origin"] == "Ahmedabad"
    assert payload["destination"] == "Bahrain"
    assert payload["grossWeightKg"] == 500.0
    assert payload["chargeableWeightBasis"] == "gross"
    assert payload["volumetricRatioKgPerCbm"] == 167.0


def test_the_same_query_always_gives_the_same_rates() -> None:
    """Demos that drift are not demonstrations."""
    assert search(REFERENCE).rates == search(REFERENCE).rates


# --- the shape a real adapter will have -----------------------------------------


def test_the_provider_is_two_stages_payload_then_mapping() -> None:
    """`simulate_response` stands where an HTTP call will stand; `map_rows` is
    the mapping a real adapter keeps. Composing them by hand must give exactly
    what `search` gives."""
    assert map_rows(simulate_response(REFERENCE)) == search(REFERENCE).rates


def test_the_payload_is_plain_json_shaped_data() -> None:
    """What crosses the simulated wire carries no domain types, because a real
    HTTP response would not."""
    payload = simulate_response(REFERENCE)

    assert isinstance(payload, dict)
    for row in payload["rates"]:
        for value in row.values():
            assert value is None or isinstance(value, str | int | float | bool)


def test_the_mapper_never_drops_a_row() -> None:
    """Rows with no price and no transit still become rates; the *filter*
    excludes them, with a reason."""
    payload = simulate_response(REFERENCE)

    assert len(map_rows(payload)) == len(payload["rates"])


@pytest.mark.parametrize("bad", [{"rates": "not a list"}, {}, {"rates": [42]}])
def test_a_malformed_payload_is_a_contract_violation(bad: dict[str, Any]) -> None:
    with pytest.raises(ContractViolation):
        map_rows(bad)


# --- the existing business logic, unchanged -------------------------------------


def test_all_three_exclusion_reasons_are_still_demonstrated() -> None:
    outcome = filter_rates(search(REFERENCE).rates, cargo_is_liquid=True)

    reasons = {excluded.reason for excluded in outcome.excluded}
    assert reasons == {
        ExclusionReason.INCOMPLETE_RATE,
        ExclusionReason.UNRANKABLE_NO_TRANSIT,
        ExclusionReason.CARRIER_RESTRICTED,
    }


def test_the_fastest_eligible_rate_still_wins_and_is_the_dearest() -> None:
    """The point of the demo: ranking on price would pick the wrong carrier,
    and the cheapest, fastest option is excluded before ranking runs."""
    eligible = filter_rates(search(REFERENCE).rates, cargo_is_liquid=True).eligible
    selection = select_rate(eligible, FASTEST_ELIGIBLE)

    assert selection is not None
    assert selection.rate.carrier_code == "EK"
    assert selection.rate.total_amount is not None
    assert all(
        selection.rate.total_amount >= other.total_amount
        for other in eligible
        if other.total_amount is not None
    )


def test_selection_cannot_see_that_the_data_is_simulated() -> None:
    """Provenance is not on `Rate`, so no business rule can branch on it."""
    rate = search(REFERENCE).rates[0]

    assert "simulated" not in rate.model_dump_json().lower()
    assert not hasattr(rate, "is_simulated")


def test_the_provider_makes_no_network_call() -> None:
    import inspect

    from translog_quote.adapters.webcargo import demo

    source = inspect.getsource(demo).lower()
    for forbidden in ("import httpx", "import requests", "import urllib", "import socket"):
        assert forbidden not in source
