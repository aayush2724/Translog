"""The WebCargo extraction layer: parsing, matching, and the search flow.

Everything runs against scripted fakes — the flow is exercised through the
`BrowserDriver` protocol exactly as the real Playwright driver would be
called, and every parser is fed the verbatim strings the Phase-3 inspection
captured from the live UI.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from translog_quote.adapters.webcargo.browser import pages
from translog_quote.adapters.webcargo.browser.mapper import (
    carrier_code_of,
    map_record,
    map_records,
    parse_duration,
    parse_price,
)
from translog_quote.adapters.webcargo.browser.pages import (
    WebCargoSessionLost,
    run_rate_search,
)
from translog_quote.adapters.webcargo.browser.records import (
    FlightLegRecord,
    WebCargoRateRecord,
)
from translog_quote.domain.rates import (
    FASTEST_ELIGIBLE,
    ExclusionReason,
    LocationRef,
    RateQuery,
    filter_rates,
    select_rate,
)
from translog_quote.domain.shipment import CargoDimensions
from translog_quote.errors import ContractViolation, PermanentFailure, UnresolvedLocation

QUERY = RateQuery(
    origin=LocationRef(stated="Bangalore"),
    destination=LocationRef(stated="Manila"),
    weight_kg=500.0,
    dimensions_in=CargoDimensions(length=34, width=24, height=6),
    date=date(2026, 9, 15),
    commodity="General Cargo",
)


def record(**overrides: object) -> WebCargoRateRecord:
    base: dict[str, object] = {
        "company": "Qatar Airways",
        "itinerary": "BLRDOHMNL",
        "departure": "Tue 15 Sep - 04:00",
        "arrival": "Wed 16 Sep - 16:30",
        "duration": "34h 00m",
        "service": "QR General",
        "rate": "167.65 Rs/kg",
        "surcharges": "All-in",
        "price": "167.65 Rs/kg/ 83,825 Rs",
        "date_tab": "15/09/2026",
    }
    base.update(overrides)
    return WebCargoRateRecord(**base)  # type: ignore[arg-type]


# --- Duration: the provider's figure or nothing -----------------------------------


@pytest.mark.parametrize(
    ("shown", "minutes"),
    [
        ("34h 00m", 2040),
        ("18h 45m", 1125),
        ("51h 20m", 3080),
        ("75h 20m", 4520),
        ("24h 30m", 1470),
        ("9h 25m", 565),
        ("34h", 2040),  # a bare hour figure is still the provider's figure
    ],
)
def test_provider_durations_parse_to_minutes(shown: str, minutes: int) -> None:
    transit = parse_duration(shown)
    assert transit is not None
    assert transit.minutes == minutes


@pytest.mark.parametrize(
    "shown",
    ["", "-", "N/A", "2 days", "AUH-MNL", "BLR DOH MNL", "0h 0m", "0h", "h m", "34h 75m"],
)
def test_anything_else_is_no_duration_never_a_guess(shown: str) -> None:
    assert parse_duration(shown) is None


def test_a_missing_duration_becomes_a_reasoned_exclusion() -> None:
    rate = map_record(record(duration=""))

    assert rate.transit is None
    outcome = filter_rates((rate,))
    assert outcome.excluded[0].reason is ExclusionReason.UNRANKABLE_NO_TRANSIT


def test_via_cannot_become_transit() -> None:
    """A record with rich routing and no duration stays unrankable: the
    itinerary has no path into the transit field."""
    rate = map_record(
        record(
            duration="",
            itinerary="BLRDOHMNL",
            legs=(
                FlightLegRecord(departure="15/09/2026 04:00", arrival="15/09/2026 05:30"),
            ),
        )
    )

    assert rate.transit is None


# --- money ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("block", "total", "currency"),
    [
        ("167.65 Rs/kg/ 83,825 Rs", Decimal("83825"), "Rs"),
        ("400.00 Rs/kg/ 40,000 Rs", Decimal("40000"), "Rs"),
        ("400.00 Rs/kg/ 1,23,456 Rs", Decimal("123456"), "Rs"),  # Indian grouping
        ("601.83 Rs/kg/ 60,183 Rs (+)", Decimal("60183"), "Rs"),
        ("100.00 USD/kg/ 10,000 USD", Decimal("10000"), "USD"),
    ],
)
def test_the_total_and_currency_come_from_the_price_block(
    block: str, total: Decimal, currency: str
) -> None:
    assert parse_price(block) == (total, currency)


@pytest.mark.parametrize("block", ["", "-", "Contact us", "kg"])
def test_an_unreadable_price_is_none_and_br4_excludes_it(block: str) -> None:
    assert parse_price(block) == (None, None)

    rate = map_record(record(price=block))
    outcome = filter_rates((rate,))
    assert outcome.excluded[0].reason is ExclusionReason.INCOMPLETE_RATE


# --- carrier identification --------------------------------------------------------


def test_the_carrier_code_comes_from_the_service_name() -> None:
    assert carrier_code_of(record(service="QR General")) == "QR"
    assert carrier_code_of(record(service="TK URGENT")) == "TK"


def test_the_carrier_code_falls_back_to_flight_numbers_then_name() -> None:
    with_legs = record(service="General", legs=(FlightLegRecord(flight_number="QR573"),))
    assert carrier_code_of(with_legs) == "QR"

    nameless = record(service="General", legs=())
    assert carrier_code_of(nameless) == "Qatar Airways"  # verbatim, not invented


# --- mapping ----------------------------------------------------------------------


def test_a_row_maps_verbatim_into_the_canonical_rate() -> None:
    rate = map_record(record())

    assert rate.carrier_code == "QR"
    assert rate.carrier_name == "Qatar Airways"
    assert rate.product == "QR General"
    assert rate.total_amount == Decimal("83825")
    assert rate.currency == "Rs"
    assert rate.transit is not None
    assert rate.transit.minutes == 2040
    assert rate.restrictions.accepts_liquids is None  # undeclared stays undeclared


def test_mapping_never_drops_or_reorders() -> None:
    records = tuple(record(service=f"C{i} Svc") for i in range(7))

    mapped = map_records(records)

    assert [rate.product for rate in mapped] == [f"C{i} Svc" for i in range(7)]


def test_the_domain_picks_fastest_over_cheaper_from_mapped_rows() -> None:
    cheap_slow = record(service="TK URGENT", duration="51h 20m", price="a/kg/ 10,000 Rs")
    dear_fast = record(service="QR General", duration="18h 45m", price="a/kg/ 99,000 Rs")

    outcome = filter_rates(map_records((cheap_slow, dear_fast)))
    selection = select_rate(outcome.eligible, FASTEST_ELIGIBLE)

    assert selection is not None
    assert selection.rate.carrier_code == "QR"  # fastest, though 10x the price


# --- choosing provider options -----------------------------------------------------


def test_an_exact_code_option_wins() -> None:
    chosen = pages._choose_option("BLR", ["BLR - Bangalore", "BLA - Somewhere"])
    assert chosen == "BLR - Bangalore"


def test_a_single_containing_option_wins() -> None:
    chosen = pages._choose_option("General Cargo", ["0000 - General Cargo"])
    assert chosen == "0000 - General Cargo"


@pytest.mark.parametrize(
    ("stated", "options"),
    [
        ("general", ["0000 - General Cargo", "7306 - general category steel"]),
        ("Dubai", []),
        ("XYZ", ["AAA - Anaa", "AAE - Annaba"]),
    ],
)
def test_no_single_match_is_a_refusal_listing_the_options(
    stated: str, options: list[str]
) -> None:
    with pytest.raises(LookupError):
        pages._choose_option(stated, options)


# --- the search flow, against a scripted driver -------------------------------------


def rows_payload(count: int, *, tabs: int = 1) -> list[dict[str, object]]:
    """`count` raw rows as the in-page extractor returns them, spread over
    date tabs the way the real strip buckets them."""
    labels = [f"{11 + i:02d}/09/2026" for i in range(tabs)]
    return [
        {
            "company": "Qatar Airways",
            "itinerary": "BLRDOHMNL",
            "departure": f"Tue 15 Sep - {i % 24:02d}:00",
            "arrival": "Wed 16 Sep - 16:30",
            "duration": "34h 00m",
            "service": "QR General",
            "rate": "167.65 Rs/kg",
            "surcharges": "All-in",
            "price": "167.65 Rs/kg/ 83,825 Rs",
            "date_tab": labels[i % tabs],
            "legs": [],
        }
        for i in range(count)
    ]


class FakeDriver:
    """Scripted `BrowserDriver`: records every operation, answers from a plan."""

    def __init__(
        self,
        *,
        rows: list[dict[str, object]] | None = None,
        stated_count: int | None = None,
        authenticated: bool = True,
        empty: bool = False,
        options: dict[str, list[str]] | None = None,
        dimension_unit: str = "IN",
        weight_unit: str = "KG",
        never_settles: bool = False,
    ) -> None:
        self.rows = rows if rows is not None else []
        count = stated_count if stated_count is not None else len(self.rows)
        self.phrase = f"Showing the {count} lowest rates"
        self.authenticated = authenticated
        self.empty = empty
        self.options = options or {
            "Bangalore": ["BLR - Bangalore"],
            "Manila": ["MNL - Manila"],
            "General Cargo": ["0000 - General Cargo"],
        }
        self.dimension_unit = dimension_unit
        self.weight_unit = weight_unit
        self.never_settles = never_settles
        self.calls: list[tuple[str, str]] = []
        self._pending_options: list[str] = []

    # -- protocol --------------------------------------------------------

    def goto(self, url: str) -> None:
        self.calls.append(("goto", url))

    def click(self, selector: str) -> None:
        self.calls.append(("click", selector))

    def fill(self, selector: str, text: str) -> None:
        self.calls.append(("fill", f"{selector}={text}"))
        self._pending_options = self.options.get(text, [])

    def press(self, selector: str, key: str) -> None:
        self.calls.append(("press", f"{selector}:{key}"))

    def wait_visible(self, selector: str, timeout_seconds: float) -> bool:
        if selector == pages.AUTHENTICATED_MARKER:
            return self.authenticated
        return bool(self._pending_options)

    def option_texts(self, selector: str) -> list[str]:
        return list(self._pending_options)

    def click_option(self, selector: str, text: str) -> None:
        self.calls.append(("option", text))

    def evaluate(self, script: str, argument: object = None) -> object:
        if "hasPassword" in script:
            return {"hasPassword": True, "hasSearchForm": False, "onApp": True}
        if "CM|IN" in script:
            return {"ok": True, "unit": self.dimension_unit}
        if "KG|LB" in script:
            return {"ok": True, "unit": self.weight_unit, "totalMode": True}
        if "settled" in script:  # _JS_RESULTS_STATE (wait-for-settle)
            if self.never_settles:
                return {"onResults": True, "settled": False, "loading": True, "empty": False}
            if self.empty:
                return {"onResults": True, "settled": False, "loading": False, "empty": True}
            return {"onResults": True, "settled": True, "loading": False, "empty": False}
        if "view_selector_radio_buttons" in script:  # _JS_ENSURE_FULL_LIST
            return {"ok": True, "how": "radio"}
        if "lowest rates" in script:  # _JS_LOWEST_PHRASE
            return self.phrase
        if "captureLegs" in script:  # _JS_EXTRACT_ALL
            return self.rows
        raise AssertionError(f"unexpected script: {script[:60]}")


def search_with(driver: FakeDriver, query: RateQuery = QUERY) -> object:
    return run_rate_search(
        driver,
        query,
        base_url="https://example.invalid/app/",
        search_timeout_seconds=0.05,
        navigation_timeout_seconds=1,
        poll_interval_seconds=0.001,
    )


@pytest.mark.parametrize("count", [0, 1, 5, 50, 200, 237])
def test_every_result_count_is_extracted_completely(count: int) -> None:
    """Zero to well past two hundred: the stated count and the captured rows
    must agree, and nothing is hard-coded around five."""
    driver = FakeDriver(rows=rows_payload(count, tabs=max(1, min(count, 6))))

    result = search_with(driver)

    assert result.stated_count == count  # type: ignore[attr-defined]
    assert len(result.records) == count  # type: ignore[attr-defined]


def test_rows_keep_their_date_tab_buckets() -> None:
    driver = FakeDriver(rows=rows_payload(6, tabs=3))

    result = search_with(driver)

    tabs = {r.date_tab for r in result.records}  # type: ignore[attr-defined]
    assert tabs == {"11/09/2026", "12/09/2026", "13/09/2026"}


def test_zero_results_is_a_valid_outcome_not_an_error() -> None:
    driver = FakeDriver(rows=[], empty=True)

    result = search_with(driver)

    assert result.stated_count == 0  # type: ignore[attr-defined]
    assert result.records == ()  # type: ignore[attr-defined]


def test_a_count_mismatch_refuses_rather_than_underreporting() -> None:
    """WebCargo says 60, we captured 59: that is missed data, not a result."""
    driver = FakeDriver(rows=rows_payload(59), stated_count=60)

    with pytest.raises(ContractViolation, match="incomplete candidate set"):
        search_with(driver)


def test_the_providers_own_wording_travels_with_the_result() -> None:
    driver = FakeDriver(rows=rows_payload(3))

    result = search_with(driver)

    assert result.stated_phrase == "Showing the 3 lowest rates"  # type: ignore[attr-defined]


def test_the_flow_sets_units_dates_and_escapes_the_dropdown() -> None:
    driver = FakeDriver(rows=rows_payload(1))

    search_with(driver)

    assert ("fill", f"{pages.DATE_INPUT}=15/09/2026") in driver.calls
    assert ("press", f"{pages.ORIGIN_INPUT}:Escape") in driver.calls
    assert ("fill", f"{pages.UNITS_INPUT}=1") in driver.calls
    assert ("fill", f"{pages.WEIGHT_INPUT}=500") in driver.calls
    assert ("option", "BLR - Bangalore") in driver.calls
    assert ("option", "MNL - Manila") in driver.calls
    assert ("option", "0000 - General Cargo") in driver.calls


def test_a_wrong_dimension_unit_stops_the_search() -> None:
    driver = FakeDriver(rows=rows_payload(1), dimension_unit="CM")

    with pytest.raises(ContractViolation, match="dimension unit"):
        search_with(driver)


def test_a_wrong_weight_unit_stops_the_search() -> None:
    driver = FakeDriver(rows=rows_payload(1), weight_unit="LB")

    with pytest.raises(ContractViolation, match="weight unit"):
        search_with(driver)


def test_a_query_without_commodity_never_reaches_the_browser() -> None:
    no_commodity = QUERY.model_copy(update={"commodity": None})
    driver = FakeDriver(rows=rows_payload(1))

    with pytest.raises(PermanentFailure, match="commodity"):
        search_with(driver, no_commodity)

    assert driver.calls == []  # refused before touching the page


def test_an_unknown_place_is_an_unresolved_location() -> None:
    driver = FakeDriver(rows=rows_payload(1), options={"General Cargo": ["0000 - General Cargo"]})

    with pytest.raises(UnresolvedLocation):
        search_with(driver)


def test_an_ambiguous_commodity_is_a_refusal_naming_the_options() -> None:
    driver = FakeDriver(
        rows=rows_payload(1),
        options={
            "Bangalore": ["BLR - Bangalore"],
            "Manila": ["MNL - Manila"],
            "General Cargo": ["0000 - General Cargo", "9999 - General Cargo (other)"],
        },
    )

    with pytest.raises(PermanentFailure, match="did not match exactly one"):
        search_with(driver)


def test_an_expired_session_is_reported_not_logged_into() -> None:
    driver = FakeDriver(rows=rows_payload(1), authenticated=False)

    with pytest.raises(WebCargoSessionLost, match="expired"):
        search_with(driver)

    filled = [c for c in driver.calls if c[0] == "fill"]
    assert filled == []  # no form was touched, no login was attempted


def test_a_results_page_that_never_settles_fails_loudly() -> None:
    driver = FakeDriver(rows=rows_payload(1), never_settles=True)

    with pytest.raises(ContractViolation, match="did not settle"):
        search_with(driver)


def test_flight_legs_are_captured_as_audit_data() -> None:
    rows = rows_payload(1)
    rows[0]["legs"] = [
        {
            "carrier": "Qatar Airways",
            "origin": "BLR",
            "destination": "DOH",
            "departure": "15/09/2026 04:00",
            "arrival": "15/09/2026 05:30",
            "duration": "4h 00m",
            "aircraft": "35H",
            "flight_number": "QR573",
        }
    ]
    driver = FakeDriver(rows=rows)

    result = search_with(driver)

    leg = result.records[0].legs[0]  # type: ignore[attr-defined]
    assert leg.flight_number == "QR573"
    assert leg.aircraft == "35H"
    # ...and the rate's transit still came from the row, not from the legs:
    assert result.records[0].duration == "34h 00m"  # type: ignore[attr-defined]
