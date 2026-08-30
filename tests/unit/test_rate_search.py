"""Rate search: filtering, selection, and the search stage.

No network anywhere. The mock adapter makes no call by construction, and the
mapper is exercised against literal payloads.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from translog_quote.adapters.routing import StatedLocationResolver, WebCargoLocationResolver
from translog_quote.adapters.webcargo import (
    DEMO_RATES,
    MockWebCargoAdapter,
    RealRateMapper,
    RealWebCargoAdapter,
)
from translog_quote.domain.rates import (
    FASTEST_ELIGIBLE,
    ExclusionReason,
    Rate,
    RateQuery,
    RateRestrictions,
    SelectionStrategy,
    SortField,
    SortKey,
    TransitTime,
    TransitUnit,
    filter_rates,
    select_rate,
)
from translog_quote.domain.shipment import CargoDimensions, RequestSource, ShipmentRecord
from translog_quote.domain.workflow import RequestState
from translog_quote.errors import (
    ContractViolation,
    PermanentFailure,
    UnresolvedFieldMapping,
    UnresolvedLocation,
)
from translog_quote.pipeline import RateSearchStage, build_query

DIMS = CargoDimensions(length=34, width=24, height=6)
RESOLVER = StatedLocationResolver()
WHEN = date(2026, 9, 2)


def rate(code: str, total: str | None, days: int | None, *, liquids: bool | None = None) -> Rate:
    return Rate(
        carrier_code=code,
        carrier_name=f"{code} Airways",
        product="GEN",
        total_amount=Decimal(total) if total else None,
        currency="INR" if total else None,
        transit=TransitTime(value=days, unit=TransitUnit.DAYS) if days else None,
        restrictions=RateRestrictions(accepts_liquids=liquids),
    )


def record(**overrides: object) -> ShipmentRecord:
    base: dict[str, object] = {
        "request_id": "R-1",
        "source": RequestSource.EMAIL,
        "origin": "Ahmedabad",
        "destination": "Bahrain",
        "weight_kg": 500.0,
        "dimensions_in": DIMS,
    }
    base.update(overrides)
    return ShipmentRecord(**base)  # type: ignore[arg-type]


# --- 1. request construction --------------------------------------------------


def test_a_query_is_built_from_the_shipment() -> None:
    query = build_query(record(), on_date=WHEN, resolver=RESOLVER)

    assert query.origin.stated == "Ahmedabad"
    assert query.destination.stated == "Bahrain"
    assert query.origin.code is None
    assert query.weight_kg == 500.0
    assert query.date == WHEN


def test_place_decoration_is_handled() -> None:
    query = build_query(
        record(destination="Bahrain (Hidd Industrial Area)"), on_date=WHEN, resolver=RESOLVER
    )

    assert query.destination.stated == "Bahrain (Hidd Industrial Area)"


def test_an_unfamiliar_place_is_accepted_rather_than_refused() -> None:
    """The whitelist is gone: any place a client names reaches the provider.

    It used to raise for anything outside a nineteen-entry table, which is what
    stopped legitimate enquiries. What must never happen is a *code* being
    invented for it — asserted here, not just the absence of an exception.
    """
    query = build_query(record(origin="Timbuktu"), on_date=WHEN, resolver=RESOLVER)

    assert query.origin.stated == "Timbuktu"
    assert query.origin.code is None
    assert query.origin.resolved_by is None


def test_a_resolver_that_cannot_resolve_refuses_rather_than_guessing() -> None:
    """A guessed airport code searches the wrong lane and looks successful."""
    with pytest.raises(UnresolvedLocation):
        build_query(record(origin="Dubai"), on_date=WHEN, resolver=WebCargoLocationResolver())


def test_a_query_needs_weight_and_dimensions() -> None:
    with pytest.raises(ContractViolation, match="weight"):
        build_query(record(weight_kg=None), on_date=WHEN, resolver=RESOLVER)
    with pytest.raises(ContractViolation, match="dimensions"):
        build_query(record(dimensions_in=None), on_date=WHEN, resolver=RESOLVER)


def test_the_query_date_has_no_default() -> None:
    """AMB-8: nothing invents a search date. The caller states it."""
    with pytest.raises(TypeError):
        build_query(record())  # type: ignore[call-arg]


def test_the_query_cannot_carry_a_client_identity() -> None:
    """BR-13, enforced by the type's shape."""
    assert "client" not in " ".join(RateQuery.model_fields)
    with pytest.raises(ValidationError):
        RateQuery(  # type: ignore[call-arg]
            origin_iata="AMD",
            destination_iata="BAH",
            weight_kg=1.0,
            dimensions_in=DIMS,
            date=WHEN,
            client_email="x@y.example",
        )


# --- 8. eligibility filtering ---------------------------------------------------


def test_a_rate_with_no_price_is_not_an_offer() -> None:
    outcome = filter_rates((rate("HY", None, 3),))

    assert outcome.eligible == ()
    assert outcome.excluded[0].reason is ExclusionReason.INCOMPLETE_RATE


def test_a_rate_with_no_transit_is_unrankable() -> None:
    """Transit is the primary key, so its absence is disqualifying, not merely
    unattractive."""
    outcome = filter_rates((rate("UL", "17200", None),))

    assert outcome.excluded[0].reason is ExclusionReason.UNRANKABLE_NO_TRANSIT


def test_a_restricted_carrier_is_excluded_only_when_the_cargo_is_known_liquid() -> None:
    restricted = rate("TK", "16900", 1, liquids=False)

    unknown = filter_rates((restricted,), cargo_is_liquid=None)
    not_liquid = filter_rates((restricted,), cargo_is_liquid=False)
    liquid = filter_rates((restricted,), cargo_is_liquid=True)

    assert unknown.eligible == (restricted,), "an unknown fact excludes nothing"
    assert not_liquid.eligible == (restricted,)
    assert liquid.excluded[0].reason is ExclusionReason.CARRIER_RESTRICTED


def test_filtering_preserves_order_and_never_rewrites_a_rate() -> None:
    rates = (rate("A", "100", 2), rate("B", None, 1), rate("C", "300", 3))

    outcome = filter_rates(rates)

    assert [r.carrier_code for r in outcome.eligible] == ["A", "C"]
    assert outcome.eligible[0] is rates[0]


def test_every_exclusion_carries_a_reason() -> None:
    outcome = filter_rates(DEMO_RATES, cargo_is_liquid=True)

    assert all(e.reason and e.detail for e in outcome.excluded)


# --- 9. fastest-rate selection ---------------------------------------------------


def test_the_fastest_eligible_rate_wins_even_when_it_is_dearest() -> None:
    """BR-1. The brief's own example: A 20k/2d, B 16k/4d, C 18k/3d -> A."""
    rates = (rate("B", "16000", 4), rate("C", "18000", 3), rate("A", "20000", 2))

    selection = select_rate(rates, FASTEST_ELIGIBLE)

    assert selection is not None
    assert selection.rate.carrier_code == "A"
    assert [r.carrier_code for r in selection.runners_up] == ["C", "B"]


def test_transit_compares_in_hours_not_raw_numbers() -> None:
    """ "2 days" and "2 hours" are the same integer, twelve times apart."""
    fast = Rate(
        carrier_code="FA",
        carrier_name="Fast",
        product="GEN",
        total_amount=Decimal("999"),
        currency="INR",
        transit=TransitTime(value=20, unit=TransitUnit.HOURS),
    )
    slow = Rate(
        carrier_code="SL",
        carrier_name="Slow",
        product="GEN",
        total_amount=Decimal("1"),
        currency="INR",
        transit=TransitTime(value=2, unit=TransitUnit.DAYS),
    )

    selection = select_rate((slow, fast), FASTEST_ELIGIBLE)

    assert selection is not None
    assert selection.rate.carrier_code == "FA"


# --- 10. deterministic tie-breaking ----------------------------------------------


def test_equal_transit_breaks_on_price() -> None:
    """BR-2, and only after transit has tied."""
    selection = select_rate((rate("Z", "900", 2), rate("Y", "500", 2)), FASTEST_ELIGIBLE)

    assert selection is not None
    assert selection.rate.carrier_code == "Y"


def test_equal_transit_and_price_break_deterministically_on_carrier() -> None:
    """Not a commercial preference — a guarantee that two runs agree."""
    a, b = rate("ZZ", "500", 2), rate("AA", "500", 2)

    first = select_rate((a, b), FASTEST_ELIGIBLE)
    second = select_rate((b, a), FASTEST_ELIGIBLE)

    assert first is not None and second is not None
    assert first.rate.carrier_code == second.rate.carrier_code == "AA"


def test_the_strategy_is_data_so_cheapest_needs_no_code_change() -> None:
    """BR-3."""
    cheapest = SelectionStrategy(
        name="cheapest",
        keys=(
            SortKey(field=SortField.TOTAL_AMOUNT),
            SortKey(field=SortField.TRANSIT),
            SortKey(field=SortField.CARRIER_CODE),
        ),
    )
    rates = (rate("A", "20000", 2), rate("B", "16000", 4))

    assert select_rate(rates, FASTEST_ELIGIBLE).rate.carrier_code == "A"  # type: ignore[union-attr]
    assert select_rate(rates, cheapest).rate.carrier_code == "B"  # type: ignore[union-attr]


def test_exactly_one_rate_is_ever_selected() -> None:
    """BR-10 — never a list."""
    selection = select_rate((rate("A", "1", 1), rate("B", "2", 2)), FASTEST_ELIGIBLE)

    assert selection is not None
    assert isinstance(selection.rate, Rate)


# --- 7 & 11. empty and all-ineligible --------------------------------------------


def test_no_rates_at_all_selects_nothing() -> None:
    assert select_rate((), FASTEST_ELIGIBLE) is None


def test_all_rates_rejected_reaches_no_eligible_rate() -> None:
    stage = RateSearchStage(
        provider=MockWebCargoAdapter(rates=(rate("HY", None, 3),)), resolver=RESOLVER
    )

    outcome = stage.run("R-1", record(), on_date=WHEN)

    assert outcome.state is RequestState.NO_ELIGIBLE_RATE
    assert outcome.selection is None
    assert outcome.filtered.excluded


# --- the stage, end to end --------------------------------------------------------


def test_the_documented_demo_fixture_selects_the_dearest_survivor() -> None:
    """The fixture is built so that ranking on price gives the wrong answer."""
    stage = RateSearchStage(provider=MockWebCargoAdapter(), resolver=RESOLVER)

    outcome = stage.run("R-1", record(), on_date=WHEN, cargo_is_liquid=True)

    assert outcome.state is RequestState.RATE_SELECTED
    assert outcome.selection is not None
    assert outcome.selection.rate.carrier_code == "EK"
    # ...and it is the most expensive of the ones that survived.
    survivors = [r.total_amount for r in outcome.filtered.eligible]
    assert outcome.selection.rate.total_amount == max(survivors)  # type: ignore[type-var]


def test_the_fixture_excludes_three_rates_for_three_different_reasons() -> None:
    stage = RateSearchStage(provider=MockWebCargoAdapter(), resolver=RESOLVER)

    outcome = stage.run("R-1", record(), on_date=WHEN, cargo_is_liquid=True)

    assert {e.reason for e in outcome.filtered.excluded} == {
        ExclusionReason.INCOMPLETE_RATE,
        ExclusionReason.UNRANKABLE_NO_TRANSIT,
        ExclusionReason.CARRIER_RESTRICTED,
    }


def test_filtering_demonstrably_runs_before_ranking() -> None:
    """The cheapest AND fastest option overall is excluded. If ranking ran
    first it would have won."""
    stage = RateSearchStage(provider=MockWebCargoAdapter(), resolver=RESOLVER)

    outcome = stage.run("R-1", record(), on_date=WHEN, cargo_is_liquid=True)

    excluded_codes = {e.rate.carrier_code for e in outcome.filtered.excluded}
    assert "TK" in excluded_codes
    assert outcome.selection is not None
    assert outcome.selection.rate.carrier_code != "TK"


def test_mock_results_are_labelled_as_mock() -> None:
    """Nothing may present fixture data as real WebCargo data."""
    outcome = RateSearchStage(provider=MockWebCargoAdapter(), resolver=RESOLVER).run(
        "R-1", record(), on_date=WHEN
    )

    assert outcome.adapter_id == "mock-webcargo"
    assert outcome.uses_mock_data is True


def test_the_mock_makes_no_network_call() -> None:
    """By construction: it holds no client, no URL and no credential."""
    adapter = MockWebCargoAdapter()

    assert not any(hasattr(adapter, attr) for attr in ("_client", "_url", "_api_key", "_password"))


# --- 4. the real adapter refuses ---------------------------------------------------


def test_the_real_adapter_refuses_with_the_reason() -> None:
    adapter = RealWebCargoAdapter()
    query = build_query(record(), on_date=WHEN, resolver=RESOLVER)

    with pytest.raises(PermanentFailure, match="not implemented"):
        adapter.search(query)


def test_the_real_adapter_accepts_no_credential() -> None:
    """There is nothing to authenticate against; a password parameter would
    invite one to be configured for an integration that does not exist."""
    with pytest.raises(TypeError):
        RealWebCargoAdapter(username="u", password="p")  # type: ignore[call-arg]


# --- 2, 3. mapping the documented response -------------------------------------


def test_the_mapper_reads_the_documented_fields() -> None:
    mapped = RealRateMapper().map_row(
        {
            "airline": "Emirates",
            "product": "GEN",
            "total": 20762.10,
            "currency": "INR",
            "accepts_liquids": True,
        }
    )

    assert mapped.carrier_name == "Emirates"
    assert mapped.total_amount == Decimal("20762.1")
    assert mapped.restrictions.accepts_liquids is True


def test_the_mapper_leaves_transit_unmapped_and_says_why() -> None:
    """AMB-1 as an executable blocker rather than a comment."""
    with pytest.raises(UnresolvedFieldMapping, match="transit-time source is unverified"):
        RealRateMapper().map_transit({"airline": "Emirates"})


def test_an_unmapped_transit_becomes_an_exclusion_not_a_crash() -> None:
    mapped = RealRateMapper().map_row(
        {"airline": "Emirates", "product": "GEN", "total": 1, "currency": "INR"}
    )

    assert mapped.transit is None
    assert filter_rates((mapped,)).excluded[0].reason is ExclusionReason.UNRANKABLE_NO_TRANSIT


@pytest.mark.parametrize(
    "row",
    [
        "not an object",
        {"product": "GEN"},
        {"airline": "", "product": "GEN"},
        {"airline": "Emirates"},
        {"airline": "Emirates", "product": "GEN", "total": "not-a-number"},
        {"airline": "Emirates", "product": "GEN", "total": True},
    ],
)
def test_a_malformed_row_is_a_contract_violation(row: object) -> None:
    """External data is validated before it enters the domain."""
    with pytest.raises(ContractViolation):
        RealRateMapper().map_row(row)  # type: ignore[arg-type]


def test_the_mapper_never_drops_a_row() -> None:
    """A mapper that discarded rows would hide the integration gaps this stage
    exists to surface."""
    rows = [{"airline": f"C{i}", "product": "GEN", "total": i + 1} for i in range(4)]

    mapped = [RealRateMapper().map_row(r) for r in rows]

    assert len(mapped) == len(rows)


# --- 12. no secret leakage ----------------------------------------------------------


def test_nothing_in_the_rate_path_carries_a_credential() -> None:
    outcome = RateSearchStage(provider=MockWebCargoAdapter(), resolver=RESOLVER).run(
        "R-1", record(), on_date=WHEN, cargo_is_liquid=True
    )

    dumped = repr(outcome)
    for secret in ("password", "api_key", "Bearer", "Authorization", "session="):
        assert secret.lower() not in dumped.lower()


def test_the_real_adapter_error_names_no_endpoint_or_credential() -> None:
    with pytest.raises(PermanentFailure) as excinfo:
        RealWebCargoAdapter().search(build_query(record(), on_date=WHEN, resolver=RESOLVER))

    message = str(excinfo.value)
    assert "webcargonet.com" not in message
    assert "password" not in message.lower()
