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
    pieces=8,
    date=date(2026, 9, 15),
    commodity="General Cargo",  # the description, no longer typed into Goods Type
    goods_type="0000 - General Cargo",  # decided before enqueue; selected exactly
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
            legs=(FlightLegRecord(departure="15/09/2026 04:00", arrival="15/09/2026 05:30"),),
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


def test_source_ref_is_unique_when_date_service_and_departure_collide() -> None:
    """The live identity collision, reproduced.

    WebCargo returned two distinct itineraries sharing a date tab, service and
    first-leg departure (``…:QR General:Sun 27 Sep - 13:20``), differing only in
    routing and transit (23h55m vs 29h25m, same price). The pre-``#index``
    source_ref was therefore identical, and the dashboard — which marks the
    selected card by source_ref equality — lit up BOTH cards. Mapping must now
    give them distinct refs, and exactly one may carry the winner's identity.
    """
    shared: dict[str, object] = {
        "date_tab": "27/09/2026",
        "service": "QR General",
        "departure": "Sun 27 Sep - 13:20",
    }
    winner_rec = record(duration="23h 55m", price="a/kg/ 525,000 Rs", **shared)
    slower_rec = record(duration="29h 25m", price="a/kg/ 525,000 Rs", **shared)

    mapped = map_records((winner_rec, slower_rec))

    # The semantic parts collide (this is the live bug); only the #index differs.
    prefixes = [rate.source_ref.rsplit("#", 1)[0] for rate in mapped]
    assert prefixes[0] == prefixes[1]  # same date tab, service, departure
    assert mapped[0].source_ref != mapped[1].source_ref  # …but the refs are distinct
    assert mapped[0].source_ref.endswith("#0")
    assert mapped[1].source_ref.endswith("#1")

    outcome = filter_rates(mapped)
    selection = select_rate(outcome.eligible, FASTEST_ELIGIBLE)
    assert selection is not None
    assert selection.rate.transit is not None
    assert selection.rate.transit.minutes == 23 * 60 + 55  # the faster of the two

    # Exactly one eligible rate carries the winner's identity — no double-SELECTED.
    matches = [r for r in outcome.eligible if r.source_ref == selection.rate.source_ref]
    assert len(matches) == 1
    assert matches[0].source_ref == mapped[0].source_ref  # the 23h55m rate, #0


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
def test_no_single_match_is_a_refusal_listing_the_options(stated: str, options: list[str]) -> None:
    with pytest.raises(LookupError):
        pages._choose_option(stated, options)


# --- querying WebCargo by the client-stated IATA code ------------------------------
#
# Live-observed WebCargo behaviour (2026-09-13): the airport autocomplete matches
# on the IATA CODE, and its options are labelled "CODE - City". Typing the code
# resolves; typing a bare city can return nothing ("Mumbai" -> [] because BOM is
# labelled "Bombay") or several ("Dubai" -> DWC/DXB/ZJF). So when the client
# themselves wrote the code in parentheses, that code is what we type.

#: The exact enabled options WebCargo returned for "BOM" and "DXB", verbatim.
_LIVE_BOM_OPTIONS = [
    "BOM - Bombay",
    "BMH - Bomai",
    "BOA - Boma",
    "LAZ - Bom Jesus da Lapa",
    "NMI - Bombay",
]
_LIVE_DUBAI_OPTIONS = ["DWC - Dubai", "DXB - Dubai", "ZJF - Dubai"]


@pytest.mark.parametrize(
    ("stated", "expected"),
    [
        ("Mumbai (BOM)", "BOM"),  # (1) parenthesized code is used as the query
        ("Dubai (DXB)", "DXB"),  # (2)
        ("BOM", "BOM"),  # (3) a bare code is unchanged
        ("DXB", "DXB"),  # (4)
        ("Mumbai", "Mumbai"),  # (5) a bare city is unchanged (and fails closed downstream)
        # No single unambiguous code -> unchanged, so it reaches WebCargo as-is
        # and fails closed there rather than picking one:
        ("Somewhere (BOM) (DXB)", "Somewhere (BOM) (DXB)"),
    ],
)
def test_a_stated_parenthesized_code_becomes_the_query(stated: str, expected: str) -> None:
    assert pages._location_query_token(stated) == expected


def test_the_stated_code_selects_the_providers_own_option() -> None:
    """(6) The unchanged exact-or-refuse matcher resolves the code to WebCargo's
    own "CODE - City" option, even when near-matches are also offered."""
    assert pages._choose_option("BOM", _LIVE_BOM_OPTIONS) == "BOM - Bombay"
    assert pages._choose_option("DXB", ["DXB - Dubai"]) == "DXB - Dubai"


def test_an_ambiguous_city_result_still_refuses() -> None:
    """(7) A bare city that returns several airports is refused, never guessed —
    the safety invariant is untouched by the code-query change."""
    with pytest.raises(LookupError):
        pages._choose_option("Dubai", _LIVE_DUBAI_OPTIONS)


def test_a_parenthesized_origin_is_typed_and_matched_as_the_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(1) end-to-end: "Mumbai (BOM)" types "BOM" into the origin field and
    selects WebCargo's "BOM - Bombay" — the verbatim city label is never typed."""
    monkeypatch.setattr(pages, "_OPTION_POLL_SECONDS", 0.0)
    driver = FakeDriver(options={"BOM": _LIVE_BOM_OPTIONS})

    chosen = pages._fill_location(driver, pages.ORIGIN_INPUT, "Mumbai (BOM)", timeout_seconds=5)

    assert chosen == "BOM - Bombay"
    assert ("fill", f"{pages.ORIGIN_INPUT}=BOM") in driver.calls
    assert ("fill", f"{pages.ORIGIN_INPUT}=Mumbai (BOM)") not in driver.calls


def test_a_parenthesized_destination_is_typed_and_matched_as_the_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(2) identical behaviour on the destination field: "Dubai (DXB)" -> "DXB"."""
    monkeypatch.setattr(pages, "_OPTION_POLL_SECONDS", 0.0)
    driver = FakeDriver(options={"DXB": ["DXB - Dubai"]})

    chosen = pages._fill_location(driver, pages.DESTINATION_INPUT, "Dubai (DXB)", timeout_seconds=5)

    assert chosen == "DXB - Dubai"
    assert ("fill", f"{pages.DESTINATION_INPUT}=DXB") in driver.calls


def test_a_bare_city_is_typed_unchanged_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(5) "Mumbai" (no code) is typed verbatim; WebCargo offers nothing, so it
    fails closed with UnresolvedLocation — no city->airport guess is made."""
    monkeypatch.setattr(pages, "_OPTION_POLL_SECONDS", 0.0)
    driver = FakeDriver(options={})  # WebCargo returns nothing for "Mumbai"

    with pytest.raises(UnresolvedLocation):
        pages._fill_location(driver, pages.ORIGIN_INPUT, "Mumbai", timeout_seconds=0.02)

    assert ("fill", f"{pages.ORIGIN_INPUT}=Mumbai") in driver.calls


# --- the debounced-autocomplete predicate wait -------------------------------------


class _DebouncedOptions:
    """A driver stub whose option list is empty (the disabled placeholder is
    excluded by the selector) for the first few reads, then yields the real
    option — reproducing the async airport lookup under headed Chromium/Xvfb."""

    def __init__(self, appears_after: int, options: list[str]) -> None:
        self._appears_after = appears_after
        self._options = options
        self.reads = 0

    def option_texts(self, selector: str) -> list[str]:
        self.reads += 1
        return list(self._options) if self.reads > self._appears_after else []


def test_the_wait_holds_through_the_debounce_then_returns_the_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It must not read the (excluded) placeholder as readiness: it polls until
    a matching ENABLED option appears, then returns it — no arbitrary sleep."""
    monkeypatch.setattr(pages, "_OPTION_POLL_SECONDS", 0.0)  # keep the test fast
    driver = _DebouncedOptions(appears_after=3, options=["MNL - Manila"])

    chosen = pages._choose_available_option(driver, pages.DROPDOWN_OPTION, "MNL", timeout_seconds=5)

    assert chosen == "MNL - Manila"
    assert driver.reads >= 4  # it genuinely waited past the empty reads


def test_the_wait_returns_immediately_when_the_option_is_already_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pages, "_OPTION_POLL_SECONDS", 0.0)
    driver = _DebouncedOptions(appears_after=0, options=["BLR - Bangalore"])

    chosen = pages._choose_available_option(driver, pages.DROPDOWN_OPTION, "BLR", timeout_seconds=5)

    assert chosen == "BLR - Bangalore"
    assert driver.reads == 1  # matched on the first read, no waiting


def test_the_wait_refuses_when_no_matching_option_ever_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The debounce tolerance must not become a guess: if only the placeholder
    (empty, here) is ever present, it raises rather than inventing a match."""
    monkeypatch.setattr(pages, "_OPTION_POLL_SECONDS", 0.0)
    driver = _DebouncedOptions(appears_after=10_000, options=["MNL - Manila"])

    with pytest.raises(LookupError):
        pages._choose_available_option(driver, pages.DROPDOWN_OPTION, "MNL", timeout_seconds=0.05)


# --- the commodity (Goods Type) selector -------------------------------------------


def test_commodity_targets_the_goods_type_select_not_a_generic_field() -> None:
    """The commodity control/field must be scoped to the Goods Type select's
    stable id hook — never a bare `.ant-select-search__field`, which also
    matches the left-nav search once origin/destination are chosen."""
    assert pages.COMMODITY_INPUT.startswith('[id^="goodsType"]')
    assert ".ant-select-search__field" in pages.COMMODITY_INPUT
    # not the old generic, id-excluding selector that resolved to 2 elements:
    assert ":not(#originAirport)" not in pages.COMMODITY_INPUT
    # the control opened is the Goods Type AntD select wrapper, scoped to the id:
    assert '[id^="goodsType"]' in pages.COMMODITY_CONTROL
    assert ".ant-select" in pages.COMMODITY_CONTROL

    driver = FakeDriver(rows=rows_payload(1))
    chosen = pages._select_goods_type(driver, "0000 - General Cargo", timeout_seconds=5)

    assert chosen == "0000 - General Cargo"
    assert ("click", pages.COMMODITY_CONTROL) in driver.calls  # opened the select first
    # the field is filtered by the CODE query, not the full "code - label"
    # string (which WebCargo returns "No Data" for):
    assert ("fill", f"{pages.COMMODITY_INPUT}=0000") in driver.calls
    assert ("fill", f"{pages.COMMODITY_INPUT}=0000 - General Cargo") not in driver.calls
    # never touches origin/destination fields:
    assert all(pages.ORIGIN_INPUT not in detail for _, detail in driver.calls)


def test_goods_type_opens_the_control_before_using_the_hidden_search_field() -> None:
    """The Goods Type search field is hidden until the select is opened, so the
    visible control must be engaged BEFORE any use of the search input."""
    driver = FakeDriver(rows=rows_payload(1))

    pages._select_goods_type(driver, "0000 - General Cargo", timeout_seconds=5)

    commodity_calls = [
        (kind, detail)
        for kind, detail in driver.calls
        if detail == pages.COMMODITY_CONTROL or detail.startswith(pages.COMMODITY_INPUT)
    ]
    assert commodity_calls[0] == ("click", pages.COMMODITY_CONTROL)  # opened first
    assert any(
        kind == "fill" and detail.startswith(pages.COMMODITY_INPUT)
        for kind, detail in commodity_calls
    )  # then the now-visible search field is typed into


def test_goods_type_refuses_when_the_search_field_never_becomes_visible() -> None:
    """If opening the select does not reveal the search field, refuse — the
    Goods Type is never guessed, skipped, or typed into a hidden field."""

    class _NeverOpens(FakeDriver):
        def click(self, selector: str) -> None:  # opening has no effect here
            self.calls.append(("click", selector))

    driver = _NeverOpens(rows=rows_payload(1))

    with pytest.raises(PermanentFailure, match="Goods Type"):
        pages._select_goods_type(driver, "0000 - General Cargo", timeout_seconds=0.01)

    # it never typed into the hidden field:
    assert all(not detail.startswith(f"{pages.COMMODITY_INPUT}=") for _, detail in driver.calls)


def test_goods_type_selects_the_exact_label() -> None:
    driver = FakeDriver(rows=rows_payload(1))

    chosen = pages._select_goods_type(driver, "0000 - General Cargo", timeout_seconds=5)

    assert chosen == "0000 - General Cargo"
    assert ("option", "0000 - General Cargo") in driver.calls


def test_goods_type_fails_loudly_when_the_label_is_not_offered() -> None:
    """The adapter never derives or guesses: a label WebCargo does not offer is
    a loud refusal naming it, not a fallback."""
    driver = FakeDriver(
        rows=rows_payload(1),
        # The code query "0000" surfaces an entry, but not the exact configured
        # label — a loud refusal naming it, never a fallback to the near-miss.
        options={"0000": ["0000 - General Cargo (other)"]},
    )

    with pytest.raises(PermanentFailure, match="did not offer the Goods Type"):
        pages._select_goods_type(driver, "0000 - General Cargo", timeout_seconds=0.2)


def test_goods_type_selects_only_the_exact_label_not_a_similar_one() -> None:
    """Live case: the dropdown shows '0000 - General Cargo' and
    '0000-90 - Vulnerable cargo'. Exact equality selects the former; the
    Vulnerable entry is never matched or clicked."""
    driver = FakeDriver(
        rows=rows_payload(1),
        # the code query "0000" surfaces both; exact equality picks the former:
        options={
            "0000": ["0000 - General Cargo", "0000-90 - Vulnerable cargo"],
        },
    )

    chosen = pages._select_goods_type(driver, "0000 - General Cargo", timeout_seconds=5)

    assert chosen == "0000 - General Cargo"
    assert ("option", "0000 - General Cargo") in driver.calls
    assert ("option", "0000-90 - Vulnerable cargo") not in driver.calls  # never matched


def test_the_grey_placeholder_is_not_treated_as_a_selection() -> None:
    """WebCargo's placeholder text is literally 'General Cargo'. If the click
    does not register (only the placeholder shows), the post-selection check
    reads the AntD *selected item* — empty — not the placeholder, and refuses."""

    class _ClickNeverCommits(FakeDriver):
        def click_option(self, selector: str, text: str) -> None:
            self.calls.append(("option", text))  # click issued; the item never commits

    driver = _ClickNeverCommits(rows=rows_payload(1))

    with pytest.raises(PermanentFailure, match="did not register as selected"):
        pages._select_goods_type(driver, "0000 - General Cargo", timeout_seconds=5)


def test_a_goods_type_offered_twice_is_a_loud_ambiguous_refusal() -> None:
    """Defensive: the same exact label appearing twice is refused as ambiguous,
    never resolved by picking the first."""
    driver = FakeDriver(
        rows=rows_payload(1),
        options={"0000": ["0000 - General Cargo", "0000 - General Cargo"]},
    )

    with pytest.raises(PermanentFailure, match="more than once"):
        pages._select_goods_type(driver, "0000 - General Cargo", timeout_seconds=1)


def test_goods_type_query_is_the_code_prefix_not_the_full_label() -> None:
    """The autocomplete filter query is the label's code (the text before the
    first ' - '), never the whole 'code - label' string (which returns 'No
    Data' live)."""
    assert pages._goods_type_query("0000 - General Cargo") == "0000"
    assert pages._goods_type_query("8506-3 - Lithium Metal Batteries Un3090 Section Ii") == "8506-3"
    assert pages._goods_type_query("29-1 - Organic Chemicals (passive Temp. Control)") == "29-1"
    # defensive: a label with no separator falls back to the whole (stripped) text
    assert pages._goods_type_query("General Cargo") == "General Cargo"


def test_a_suffix_coded_goods_type_filters_by_its_code_and_selects_exactly() -> None:
    """A suffixed code like '8506-3' is derived as the query; the code surfaces
    the whole family and exact equality selects the intended variant only."""
    label = "8506-3 - Lithium Metal Batteries"
    driver = FakeDriver(
        rows=rows_payload(1),
        options={
            "8506-3": [
                "8506 - Lithium Batteries",
                "8506-1 - Lithium Metal Batteries (Section Ia)",
                "8506-3 - Lithium Metal Batteries",
            ],
        },
    )

    chosen = pages._select_goods_type(driver, label, timeout_seconds=5)

    assert chosen == label
    assert ("fill", f"{pages.COMMODITY_INPUT}=8506-3") in driver.calls  # code query
    assert ("option", label) in driver.calls  # exact variant selected
    assert ("option", "8506 - Lithium Batteries") not in driver.calls  # family head never matched


def test_full_label_is_never_typed_as_the_filter_query() -> None:
    """Guard against a regression to typing the whole label: WebCargo returns
    'No Data' for it, so the field must be filled with the code query only."""
    driver = FakeDriver(rows=rows_payload(1))

    pages._select_goods_type(driver, "0000 - General Cargo", timeout_seconds=5)

    fills = [detail for kind, detail in driver.calls if kind == "fill"]
    assert f"{pages.COMMODITY_INPUT}=0000" in fills
    assert all("0000 - General Cargo" not in f for f in fills)


def test_selection_confirms_tolerates_code_removal_and_case_but_not_mismatch() -> None:
    """The committed selected-item display drops the code and may recase — a
    real match is accepted, but an empty, partial, or different value is not."""
    chosen = "30 - Pharmaceutical Products (no Temperature Control)"
    assert pages._selection_confirms(chosen, chosen)  # exact
    # live-observed: code dropped + recased
    assert pages._selection_confirms("Pharmaceutical Products (NO Temperature Control)", chosen)
    # whitespace/format differences are tolerated
    assert pages._selection_confirms(" Pharmaceutical  Products (NO Temperature Control) ", chosen)
    assert not pages._selection_confirms("", chosen)  # only the placeholder -> not a selection
    assert not pages._selection_confirms("Organic Chemicals", chosen)  # a different commodity
    # a partial name (not the whole name) is not enough:
    assert not pages._selection_confirms("Pharmaceutical Products", chosen)


def test_goods_type_selection_accepts_a_code_dropped_recased_display() -> None:
    """End to end: WebCargo commits the selection as the name only, uppercased
    (no code). The exact option was still clicked, so selection is accepted."""

    class _CommitsDisplayName(FakeDriver):
        def click_option(self, selector: str, text: str) -> None:
            self.calls.append(("option", text))
            name = text.split(" - ", 1)[1]
            self._goods_type_selected = name.upper()  # drops the code, recases

    driver = _CommitsDisplayName(rows=rows_payload(1))

    chosen = pages._select_goods_type(driver, "0000 - General Cargo", timeout_seconds=5)

    assert chosen == "0000 - General Cargo"
    assert ("option", "0000 - General Cargo") in driver.calls


def test_goods_type_selection_rejects_a_mismatched_committed_display() -> None:
    """If the committed selection is a DIFFERENT commodity, refuse loudly — the
    robust check must never accept one commodity as proof another was chosen."""

    class _CommitsWrongCommodity(FakeDriver):
        def click_option(self, selector: str, text: str) -> None:
            self.calls.append(("option", text))
            self._goods_type_selected = "Organic Chemicals"  # not what was clicked

    driver = _CommitsWrongCommodity(rows=rows_payload(1))

    with pytest.raises(PermanentFailure, match="did not register as selected"):
        pages._select_goods_type(driver, "0000 - General Cargo", timeout_seconds=5)


# --- the readonly AntD DatePicker interaction --------------------------------------


def test_departure_date_is_set_through_the_calendar_and_verified() -> None:
    """The readonly display starts on a default; the requested date is typed
    into the calendar input, committed, and confirmed on the display."""
    driver = FakeDriver(rows=rows_payload(1))  # default display: 11/09/2026
    pages._fill_departure_date(driver, date(2026, 9, 21), timeout_seconds=5)

    assert driver._date_value == "21/09/2026"
    assert ("click", pages.DATE_TRIGGER) in driver.calls
    assert ("fill", f"{pages.CALENDAR_INPUT}=21/09/2026") in driver.calls
    assert ("press", f"{pages.CALENDAR_INPUT}:Enter") in driver.calls


def test_departure_date_is_idempotent_when_already_selected() -> None:
    driver = FakeDriver(rows=rows_payload(1))
    driver._date_value = "21/09/2026"
    pages._fill_departure_date(driver, date(2026, 9, 21), timeout_seconds=5)

    # already correct → the picker is never opened or typed into
    assert all(pages.CALENDAR_INPUT not in detail for _, detail in driver.calls)
    assert ("click", pages.DATE_TRIGGER) not in driver.calls


def test_a_date_that_will_not_take_is_a_loud_failure_not_a_wrong_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-filled default must never be silently accepted: if the display
    never shows the requested date, the search refuses."""
    monkeypatch.setattr(pages, "_OPTION_POLL_SECONDS", 0.0)

    class _StuckDate(FakeDriver):
        def press(self, selector: str, key: str) -> None:  # never commits
            self.calls.append(("press", f"{selector}:{key}"))

    driver = _StuckDate(rows=rows_payload(1))  # stays on 11/09/2026
    with pytest.raises(ContractViolation, match="unconfirmed date"):
        pages._fill_departure_date(driver, date(2026, 9, 21), timeout_seconds=0.05)


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
            # Goods Type: the field is filtered by the label's CODE query
            # (`_goods_type_query`), then matched on the exact full label.
            "0000": ["0000 - General Cargo"],
        }
        self.dimension_unit = dimension_unit
        self.weight_unit = weight_unit
        self.never_settles = never_settles
        self.calls: list[tuple[str, str]] = []
        self._pending_options: list[str] = []
        # DatePicker model: readonly display starts on a default; typing into
        # the calendar input + Enter commits the new value.
        self._date_value = "11/09/2026"
        self._date_pending: str | None = None
        # The Goods Type AntD select hides its inner search field until opened.
        self._commodity_open = False
        # The AntD *selected item* (committed value); "" while only the grey
        # placeholder shows. Clicking an option commits it (see click_option).
        self._goods_type_selected = ""

    # -- protocol --------------------------------------------------------

    def goto(self, url: str) -> None:
        self.calls.append(("goto", url))

    def click(self, selector: str) -> None:
        self.calls.append(("click", selector))
        if selector == pages.COMMODITY_CONTROL:
            self._commodity_open = True  # opening the select reveals its search field

    def fill(self, selector: str, text: str) -> None:
        self.calls.append(("fill", f"{selector}={text}"))
        if selector == pages.CALENDAR_INPUT:
            self._date_pending = text
            return
        self._pending_options = self.options.get(text, [])

    def press(self, selector: str, key: str) -> None:
        self.calls.append(("press", f"{selector}:{key}"))
        if selector == pages.CALENDAR_INPUT and key == "Enter" and self._date_pending:
            self._date_value = self._date_pending  # commit the typed date

    def wait_visible(self, selector: str, timeout_seconds: float) -> bool:
        if selector == pages.AUTHENTICATED_MARKER:
            return self.authenticated
        if selector == pages.CALENDAR_INPUT:
            return True  # clicking the readonly input opens the calendar panel
        if selector == pages.COMMODITY_INPUT:
            return self._commodity_open  # hidden until the Goods Type select is opened
        return bool(self._pending_options)

    def option_texts(self, selector: str) -> list[str]:
        return list(self._pending_options)

    def click_option(self, selector: str, text: str) -> None:
        self.calls.append(("option", text))
        self._goods_type_selected = text  # the AntD selected item commits

    def evaluate(self, script: str, argument: object = None) -> object:
        if "departureDate" in script:  # _date_input_value read
            return self._date_value
        if "hasPassword" in script:
            return {"hasPassword": True, "hasSearchForm": False, "onApp": True}
        if "CM|IN" in script:
            return {"ok": True, "unit": self.dimension_unit}
        if "KG|LB" in script:
            # Mirror the browser script's contract: only an exact "KG" is Total
            # mode; "KG/Unit" (per-piece) or a non-KG unit reports failure, so the
            # search refuses rather than entering a piece-multiplied weight.
            if self.weight_unit == "KG":
                return {"ok": True, "unit": "KG", "totalMode": True}
            return {
                "ok": False,
                "unit": self.weight_unit,
                "totalMode": False,
                "reason": f"weight not in Total KG mode ({self.weight_unit})",
            }
        if "settled" in script:  # _JS_RESULTS_STATE (wait-for-settle)
            if self.never_settles:
                return {"onResults": True, "settled": False, "loading": True, "empty": False}
            if self.empty:
                return {"onResults": True, "settled": False, "loading": False, "empty": True}
            return {"onResults": True, "settled": True, "loading": False, "empty": False}
        if "view_selector_radio_buttons" in script:  # _JS_ENSURE_FULL_LIST
            return {"ok": True, "how": "radio"}
        if "selection-item" in script:  # _JS_GOODS_TYPE_SELECTED
            return self._goods_type_selected
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


def test_the_settle_predicate_carries_webcargos_live_empty_wording() -> None:
    """A node-less guard for the empty-state detection fix: the predicate must
    keep matching WebCargo's actual zero-rate wording (live-observed 2026-09-13),
    or an empty search hangs until timeout again. The behavioural check that the
    !anyCount guard still holds lives in tests/js/results_state.test.js."""
    predicate = pages._JS_RESULTS_STATE
    assert "No results found" in predicate
    assert "There may be no results" in predicate


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

    assert ("fill", f"{pages.CALENDAR_INPUT}=15/09/2026") in driver.calls  # via the picker
    assert ("press", f"{pages.ORIGIN_INPUT}:Escape") in driver.calls
    # The WebCargo Pieces field carries the client's actual count (QUERY.pieces == 8),
    # never a hardcoded "1" — the volumetric-weight basis depends on it.
    assert ("fill", f"{pages.UNITS_INPUT}=8") in driver.calls
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


def test_per_piece_kg_unit_weight_stops_the_search_and_enters_no_weight() -> None:
    """The 300 kg -> 900 kg bug. Left in per-piece ("KG/Unit") mode, WebCargo
    multiplies the entered figure by the Pieces count. RateQuery.weight_kg is a
    shipment total, so the search must refuse rather than search a multiplied
    weight — and must never fill the weight field on that path."""
    driver = FakeDriver(rows=rows_payload(1), weight_unit="KG/Unit")

    with pytest.raises(ContractViolation, match="weight unit"):
        search_with(driver)

    assert not any(
        call[0] == "fill" and call[1].startswith(f"{pages.WEIGHT_INPUT}=")
        for call in driver.calls
    ), "the weight must not be entered when Total KG mode was not verified"


def test_require_ok_rejects_kg_per_unit_as_not_total() -> None:
    """Defence in depth: even if the browser script reported ok, a 'KG/Unit'
    unit must not pass a check that expects an exact 'KG' (total) — the old
    startswith('KG') let it through, which is how the tripled weight escaped."""
    with pytest.raises(ContractViolation, match="not exactly 'KG'"):
        pages._require_ok(
            {"ok": True, "unit": "KG/Unit", "totalMode": False},
            expect_unit="KG",
            what="weight unit",
        )


def test_total_kg_mode_is_positively_verified_before_the_weight_is_entered() -> None:
    """The positive path: with the weight select in exact "KG" (Total) mode the
    search proceeds and the total weight is entered verbatim (not multiplied)."""
    driver = FakeDriver(rows=rows_payload(1))  # weight_unit defaults to "KG" (Total)

    search_with(driver)

    assert ("fill", f"{pages.WEIGHT_INPUT}={pages._figure(QUERY.weight_kg)}") in driver.calls


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


def test_a_goods_type_webcargo_does_not_offer_is_a_loud_refusal() -> None:
    """End to end: the decided Goods Type label must exist in WebCargo's list.
    An absent label is a loud refusal naming it — never a substitute or guess."""
    driver = FakeDriver(
        rows=rows_payload(1),
        options={
            "Bangalore": ["BLR - Bangalore"],
            "Manila": ["MNL - Manila"],
            "0000 - General Cargo": [],  # WebCargo does not offer the configured label
        },
    )

    with pytest.raises(PermanentFailure, match="did not offer the Goods Type"):
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


# --- the authenticated-shell probe (read-only; never a login) ----------------------


def test_is_authenticated_is_true_when_the_search_form_renders() -> None:
    driver = FakeDriver(authenticated=True)

    assert pages.is_authenticated(
        driver, base_url="https://example.invalid/app/", timeout_seconds=1
    )
    assert ("goto", pages.search_url("https://example.invalid/app/")) in driver.calls
    assert [c for c in driver.calls if c[0] == "fill"] == []  # a probe, never a login


def test_is_authenticated_is_false_when_the_search_form_never_appears() -> None:
    driver = FakeDriver(authenticated=False)

    assert not pages.is_authenticated(
        driver, base_url="https://example.invalid/app/", timeout_seconds=0.05
    )
    assert [c for c in driver.calls if c[0] == "fill"] == []  # still never a login


# --- the operator sign-in ceremony (on a LIVE session; no launch/close) -------------


def test_operator_login_reports_success_only_after_the_form_is_visible(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from translog_quote.adapters.webcargo.browser.reauth import run_operator_login

    driver = FakeDriver(authenticated=True)
    prompts: list[str] = []

    run_operator_login(
        driver,
        base_url="https://example.invalid/app/",
        navigation_timeout_seconds=1,
        prompt=lambda msg: prompts.append(msg) or "",
    )

    assert prompts  # the human ceremony ran
    assert "Authenticated" in capsys.readouterr().out  # printed only after verify
    assert [c for c in driver.calls if c[0] == "fill"] == []  # never signed in for them


def test_operator_login_refuses_when_the_form_never_appears() -> None:
    from translog_quote.adapters.webcargo.browser.reauth import run_operator_login

    driver = FakeDriver(authenticated=False)

    with pytest.raises(WebCargoSessionLost):
        run_operator_login(
            driver,
            base_url="https://example.invalid/app/",
            navigation_timeout_seconds=0.05,
            prompt=lambda _msg: "",
        )


class _AuthOnNavigate(FakeDriver):
    """A fresh page: the authenticated search form appears only AFTER the flow
    navigates to the search surface — the way a real per-job blank page behaves
    (and the opposite of an operator page that is already on the form)."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._navigated = False

    def goto(self, url: str) -> None:
        super().goto(url)
        if pages.SEARCH_HASH in url:
            self._navigated = True

    def wait_visible(self, selector: str, timeout_seconds: float) -> bool:
        if selector == pages.AUTHENTICATED_MARKER:
            return self._navigated  # not on the form until we navigate to it
        return super().wait_visible(selector, timeout_seconds)


def test_verify_accepts_the_current_authenticated_page_without_navigating() -> None:
    """The operator has just signed in and the form is visible: verify must NOT
    navigate away (a fresh navigation can bounce the session back to login)."""
    driver = FakeDriver(authenticated=True)

    pages.verify_authenticated(
        driver, base_url="https://host/ajaxnew/?rand=408788&ctry=in", timeout_seconds=1
    )

    assert [c for c in driver.calls if c[0] == "goto"] == []  # stayed on the live page


def test_verify_navigates_to_the_canonical_url_when_not_already_on_the_form() -> None:
    driver = _AuthOnNavigate()

    pages.verify_authenticated(
        driver, base_url="https://host/ajaxnew/?rand=408788&ctry=in", timeout_seconds=1
    )

    gotos = [detail for kind, detail in driver.calls if kind == "goto"]
    assert len(gotos) == 1  # navigated exactly once, to the search surface
    assert "rand=" not in gotos[0]  # the canonical, rand-free URL
    assert "ctry=in" in gotos[0]  # country context preserved


def test_verify_still_reports_session_loss_when_the_form_never_appears() -> None:
    driver = FakeDriver(authenticated=False)  # form absent before AND after nav

    with pytest.raises(WebCargoSessionLost):
        pages.verify_authenticated(
            driver, base_url="https://host/ajaxnew/?rand=1", timeout_seconds=0.01
        )

    assert any(kind == "goto" for kind, _ in driver.calls)  # it did try the canonical URL


def test_per_job_navigation_uses_the_canonical_rand_free_url() -> None:
    """The fix must apply to the job path too: a fresh job page navigates to
    the same canonical (rand-stripped) search URL before searching."""
    driver = _AuthOnNavigate(rows=rows_payload(1))

    result = run_rate_search(
        driver,
        QUERY,
        base_url="https://host/ajaxnew/?rand=408788&ctry=in",
        search_timeout_seconds=0.05,
        navigation_timeout_seconds=1,
        poll_interval_seconds=0.001,
    )

    gotos = [detail for kind, detail in driver.calls if kind == "goto"]
    assert gotos, "a fresh job page must navigate to the search form"
    assert all("rand=" not in g for g in gotos)  # canonical everywhere, no stale nonce
    assert any("ctry=in" in g for g in gotos)
    assert len(result.records) == 1  # and the search still completes


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
