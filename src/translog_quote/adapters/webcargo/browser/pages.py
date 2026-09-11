"""Every WebCargo selector, and the search flow that uses them.

Selectors live HERE and nowhere else, exactly as inspected on the live,
authenticated UI (Phase 3, 2026-09-10). Preference order: stable ids
(`#originAirport`), Ant Design structural classes (`.ant-collapse-item`),
label-anchored text pairing — and CSS-module hashed classes only as prefix
matches (`[class*="styles_duration"]`), never as exact names, because the
hash suffix changes on every frontend build.

The flow is written against `BrowserDriver`, a protocol of the operations
the flow needs — so the whole sequence is testable with a scripted fake, and
Playwright appears in exactly one module (`driver.py`). The heavyweight DOM
work (readiness polling, date-tab iteration, row harvesting) runs as
JavaScript inside the page, returning plain data that Python turns into
records; the scripts are constants here so they are reviewable next to the
selectors they use.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Protocol

from translog_quote.adapters.webcargo.browser.records import (
    FlightLegRecord,
    WebCargoRateRecord,
    WebCargoResultSet,
)
from translog_quote.errors import ContractViolation, PermanentFailure, UnresolvedLocation

if TYPE_CHECKING:
    from translog_quote.domain.rates import RateQuery

# --- where things are -------------------------------------------------------------

SEARCH_HASH = "#ebookings/search-and-book"
RESULTS_HASH = "#ebookings/dynamic-results"

# The search form (verified ids and placeholders).
ORIGIN_INPUT = "#originAirport"
DESTINATION_INPUT = "#destinationAirport"
DATE_INPUT = ".ant-calendar-picker-input"
UNITS_INPUT = "#units-0"
LENGTH_INPUT = 'input[placeholder="Length"]'
WIDTH_INPUT = 'input[placeholder="Width"]'
HEIGHT_INPUT = 'input[placeholder="Height"]'
WEIGHT_INPUT = "#weight-0"
SEARCH_BUTTON = "button.searchFlights"

#: Location/commodity suggestions render into AntD dropdowns.
DROPDOWN_OPTION = ".ant-select-dropdown li.ant-select-dropdown-menu-item"

#: The Matrix/Full-list view toggle is an Ant Design radio group. The wrapper
#: keeps a stable CSS-module *stem* (`view_selector_radio_buttons`, only the
#: hash suffix churns), and the options and their checked-state are
#: framework-stable AntD classes. Verified live: index 0 = Matrix, index 1 =
#: Full list, and Full list is active exactly when the results accordion
#: (`.ant-collapse-item`) is present.
VIEW_TOGGLE_GROUP = '[class*="view_selector_radio_buttons"] .ant-radio-button-wrapper'
FULL_LIST_INDEX = 1
RESULT_PANEL = ".ant-collapse > .ant-collapse-item"

#: The date strip in Full-list view: one `li` per departure date. The
#: selected date carries `.header_active*`, a date with no rates carries
#: `.header_disabled*`, and each enabled `li` filters the panels to that
#: date. Verified live: the panels of every enabled date, summed, equal the
#: provider's "Showing the N lowest rates" total.
DATE_TAB = "ul.ant-list-items li.ant-list-item"

#: The authenticated shell: the search form's own origin field. If this
#: never appears, we are not looking at the app we were logged into.
AUTHENTICATED_MARKER = ORIGIN_INPUT


class WebCargoSessionLost(PermanentFailure):
    """The persistent session no longer reaches the authenticated app.

    Raised instead of any login attempt. The operator re-authentication
    command is the only path back — never this process.
    """


# --- in-page scripts ---------------------------------------------------------------
# Each returns plain JSON-safe data. They are the only pieces that know how
# the React internals behave (AntD selects open on mousedown, date tabs are
# text-identified), and every one reports what it actually did so the flow
# can verify instead of assume.

_JS_LOGIN_CHECK = """
() => ({
  hasPassword: !!document.querySelector('input[type="password"]'),
  hasSearchForm: !!document.querySelector('#originAirport'),
  onApp: location.pathname.includes('/ajaxnew'),
})
"""

_JS_SET_DIMENSION_UNIT = """
async () => {
  const fire = (el) => {
    el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
    el.dispatchEvent(new MouseEvent('click', {bubbles: true}));
  };
  const selects = [...document.querySelectorAll('.ant-select')]
    .filter(s => s.offsetWidth > 0 && /^(CM|IN)$/.test(s.innerText.trim()));
  if (!selects.length) return {ok: false, reason: 'dimension unit select not found'};
  fire(selects[0]);
  await new Promise(r => setTimeout(r, 400));
  const option = [...document.querySelectorAll('.ant-select-dropdown li')]
    .find(li => li.offsetWidth > 0 && li.textContent.trim() === 'IN');
  if (!option) return {ok: false, reason: 'IN option not offered'};
  option.click();
  await new Promise(r => setTimeout(r, 300));
  return {ok: true, unit: selects[0].innerText.trim()};
}
"""

_JS_SET_WEIGHT_TOTAL_KG = """
async () => {
  const fire = (el) => {
    el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
    el.dispatchEvent(new MouseEvent('click', {bubbles: true}));
  };
  // "Weight: Unit | Total" — Total makes the single weight figure the
  // shipment total, which is what RateQuery.weight_kg means.
  const total = [...document.querySelectorAll('a,span,button,div')]
    .find(e => e.offsetWidth > 0 && e.textContent.trim() === 'Total'
            && e.closest('div')
            && /Weight/.test(e.closest('div').parentElement?.textContent || ''));
  if (total) { total.click(); await new Promise(r => setTimeout(r, 300)); }
  const selects = [...document.querySelectorAll('.ant-select')]
    .filter(s => s.offsetWidth > 0 && /^(KG|LB)(\\/Unit)?$/.test(s.innerText.trim()));
  if (!selects.length) return {ok: false, reason: 'weight unit select not found'};
  const current = selects[0].innerText.trim();
  if (!current.startsWith('KG')) {
    fire(selects[0]);
    await new Promise(r => setTimeout(r, 400));
    const option = [...document.querySelectorAll('.ant-select-dropdown li')]
      .find(li => li.offsetWidth > 0 && li.textContent.trim().startsWith('KG'));
    if (!option) return {ok: false, reason: 'KG option not offered'};
    option.click();
    await new Promise(r => setTimeout(r, 300));
  }
  return {ok: true, unit: selects[0].innerText.trim(), totalMode: !!total};
}
"""

#: Wait-for-settle only. The authoritative count is NOT read here — the
#: initial (Matrix) view reports a different figure ("We found the N cheapest")
#: than the Full-list total ("Showing the N lowest rates"), so the count is
#: read after switching to Full list. This only answers: has the results
#: surface finished loading, and did the provider find anything at all?
#:
#: `empty` requires the provider's OWN "no rates" statement AND the absence of
#: any count phrase — the "couldn't find any results for these airlines"
#: section shows even on successful searches, so it is not treated as empty on
#: its own.
_JS_RESULTS_STATE = """
() => {
  const texts = [...document.querySelectorAll('div,span,p')].map(e => e.textContent || '');
  const short = (re) => texts.some(t => re.test(t) && t.length < 200);
  const anyCount = short(/Showing\\s+the\\s+\\d+\\s+lowest rates/)
    || short(/We found\\s+the\\s+\\d+\\s+(?:cheapest|lowest) rates/);
  const loading = short(/Loading the best result|Your results are loading/);
  const noRates = short(/No rates? (?:were )?found/i) || short(/no results for your search/i);
  return {
    onResults: location.hash.includes('dynamic-results'),
    settled: anyCount && !loading,
    loading,
    empty: noRates && !anyCount && !loading,
  };
}
"""

#: Switch to Full list via the Ant Design view-toggle radio group. Idempotent:
#: if the results accordion is already present we are in Full list. Verified
#: live: option index 1 is Full list; success is the accordion appearing.
_JS_ENSURE_FULL_LIST = """
async () => {
  if (document.querySelector('.ant-collapse > .ant-collapse-item'))
    return {ok: true, how: 'already'};
  const labels = [...document.querySelectorAll(
    '[class*="view_selector_radio_buttons"] .ant-radio-button-wrapper')];
  if (labels.length < 2) return {ok: false, reason: 'view toggle not found'};
  labels[1].click();
  await new Promise(r => setTimeout(r, 1500));
  const ok = !!document.querySelector('.ant-collapse > .ant-collapse-item');
  return ok ? {ok: true, how: 'radio'} : {ok: false, reason: 'accordion did not appear'};
}
"""

#: The authoritative Full-list total, verbatim. Read only after Full list is
#: active. Verified live: this equals the sum of every enabled date tab's rows.
_JS_LOWEST_PHRASE = """
() => {
  const t = [...document.querySelectorAll('div,span,p')].map(e => e.textContent || '')
    .find(x => /Showing\\s+the\\s+\\d+\\s+lowest rates/.test(x) && x.length < 200);
  return t ? t.replace(/\\s+/g, ' ').trim() : null;
}
"""

#: Harvest every rate row across EVERY enabled date tab. The Full-list date
#: strip (`ul.ant-list-items`) has one `li` per date; a `.header_disabled` li
#: has no rates and is skipped, every other li is clicked to filter the panels
#: to that date, and its rows are tagged with the date. Duration is read from
#: the row's own "Duration" column and nowhere else; the itinerary (Via) has no
#: path into it. Returns the flat list; the count guard lives in Python.
_JS_EXTRACT_ALL = """
async (captureLegs) => {
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const readPanels = () => {
    return [...document.querySelectorAll('.ant-collapse > .ant-collapse-item')].map(p => {
      const row = {company:'', itinerary:'', departure:'', arrival:'', duration:'',
                   service:'', rate:'', surcharges:'', price:''};
      for (const col of p.querySelectorAll('.ant-collapse-header [class*="styles_grid_col"]')) {
        const labelEl = col.querySelector('[class*="styles_modalName"]');
        const valueEl = col.querySelector('[class*="styles_modalContent"]');
        const label = clean(labelEl && labelEl.textContent).toLowerCase();
        const value = clean(valueEl ? valueEl.textContent : col.textContent);
        if (label === 'company') row.company = value;
        else if (label === 'itinerary') row.itinerary = value;
        else if (label === 'departure') row.departure = value;
        else if (label === 'arrival') row.arrival = value;
        else if (label === 'duration') row.duration = value;
        else if (label === 'service' || value.startsWith('Service '))
          row.service = value.replace(/^Service\\s+/, '');
        else if (label === 'rate' || value.startsWith('Rate '))
          row.rate = value.replace(/^Rate\\s+/, '');
        else if (label === 'surcharges' || value.startsWith('Surcharges '))
          row.surcharges = value.replace(/^Surcharges\\s+/, '');
        else if (/\\/kg\\s*\\//.test(value) || /\\/\\s*[\\d.,]+\\s*[A-Za-z]{1,5}$/.test(value))
          row.price = value;
      }
      return {row, panel: p};
    });
  };
  const readLegs = async (panel) => {
    const header = panel.querySelector('.ant-collapse-header');
    if (!header) return [];
    header.click();
    await new Promise(r => setTimeout(r, 500));
    const legs = [...panel.querySelectorAll('[class*="styles_contentFlight"]')].map(legRow => {
      const grab = (frag) => {
        const el = legRow.querySelector('[class*="styles_' + frag + '"]');
        return clean(el && el.textContent);
      };
      const cols = [...legRow.querySelectorAll('[class*="styles_grid_col"]')]
        .map(c => clean(c.textContent));
      const flight = cols.map(t => (t.match(/^[A-Z0-9]{2}\\d{2,5}$/) || [null])[0])
        .find(Boolean) || '';
      const aircraft = cols.map(t => (t.match(/^[A-Z0-9]{2,4}$/) || [null])[0])
        .find(t => t && t !== flight) || '';
      const itin = grab('itinerary').split(/\\s+/).filter(Boolean);
      return {carrier: grab('company'), origin: itin[0] || '',
              destination: itin[itin.length-1] || '',
              departure: grab('departure'), arrival: grab('arrival'),
              duration: grab('duration'), aircraft, flight_number: flight};
    });
    header.click();  // leave the panel as found
    await new Promise(r => setTimeout(r, 200));
    return legs;
  };
  const collect = async (label) => {
    for (const {row, panel} of readPanels()) {
      row.legs = captureLegs ? await readLegs(panel) : [];
      row.date_tab = label;
      out.push(row);
    }
  };

  const out = [];
  const strip = document.querySelector('ul.ant-list-items');
  const tabs = strip
    ? [...strip.querySelectorAll('li.ant-list-item')]
        .filter(li => !/header_disabled/.test(li.className))
    : [];
  if (!tabs.length) {
    await collect('');
    return out;
  }
  for (const li of tabs) {
    const label = (clean(li.textContent).match(/^\\d{2}\\/\\d{2}\\/\\d{4}/) || [''])[0];
    li.click();
    await new Promise(r => setTimeout(r, 1300));
    await collect(label);
  }
  return out;
}
"""


class BrowserDriver(Protocol):
    """The operations the search flow needs — and no more.

    Implemented by `driver.PlaywrightWebCargoDriver` for the real browser
    and by scripted fakes in the tests. Keeping this narrow is what keeps
    Playwright types out of everything above the driver.
    """

    def goto(self, url: str) -> None: ...

    def click(self, selector: str) -> None: ...

    def fill(self, selector: str, text: str) -> None: ...

    def press(self, selector: str, key: str) -> None: ...

    def wait_visible(self, selector: str, timeout_seconds: float) -> bool: ...

    def option_texts(self, selector: str) -> list[str]: ...

    def click_option(self, selector: str, text: str) -> None: ...

    def evaluate(self, script: str, argument: object = None) -> object: ...


def _choose_option(stated: str, options: list[str]) -> str:
    """Which suggestion the provider's own list offers for what was stated.

    Deterministic and refusal-first: an exact "CODE - Name" whose code
    equals the stated text wins; otherwise exactly one option containing
    the stated text (case-insensitively) wins; anything else is a refusal
    that lists what WAS offered. Nothing fuzzy, nothing scored.
    """
    cleaned = [option.strip() for option in options if option.strip()]
    stated_fold = stated.strip().casefold()

    exact_code = [o for o in cleaned if o.casefold().startswith(f"{stated_fold} - ")]
    if len(exact_code) == 1:
        return exact_code[0]

    containing = [o for o in cleaned if stated_fold in o.casefold()]
    if len(containing) == 1:
        return containing[0]

    raise LookupError(
        f"{stated!r} did not match exactly one provider option; offered: {cleaned!r}"
    )


def _fill_location(driver: BrowserDriver, selector: str, stated: str) -> str:
    """Type the stated place, select WebCargo's own suggestion, close the list.

    Returns the option text actually selected — the resolver evidence.
    """
    driver.click(selector)
    driver.fill(selector, stated)
    if not driver.wait_visible(DROPDOWN_OPTION, timeout_seconds=10):
        raise UnresolvedLocation(
            f"WebCargo offered no location suggestion for {stated!r}; "
            "refusing rather than guessing an airport"
        )
    try:
        chosen = _choose_option(stated, driver.option_texts(DROPDOWN_OPTION))
    except LookupError as exc:
        raise UnresolvedLocation(str(exc)) from exc
    driver.click_option(DROPDOWN_OPTION, chosen)
    driver.press(selector, "Escape")  # the list lingers and swallows clicks
    return chosen


def _fill_commodity(driver: BrowserDriver, stated: str) -> str:
    """Select the WebCargo commodity matching the caller's wording, exactly.

    The commodity select is the Goods Type AntD select; suggestions look
    like "0000 - General Cargo". No match, no search — and never a default.
    """
    commodity_input = (
        ".ant-select-search__field:not(#originAirport):not(#destinationAirport)"
    )
    driver.click(commodity_input)
    driver.fill(commodity_input, stated)
    if not driver.wait_visible(DROPDOWN_OPTION, timeout_seconds=10):
        raise PermanentFailure(
            f"WebCargo offered no commodity option for {stated!r}; the search "
            "cannot run under a commodity nobody stated"
        )
    try:
        chosen = _choose_option(stated, driver.option_texts(DROPDOWN_OPTION))
    except LookupError as exc:
        raise PermanentFailure(f"commodity {exc}") from exc
    driver.click_option(DROPDOWN_OPTION, chosen)
    return chosen


def search_url(base_url: str) -> str:
    """The search surface URL for a configured base.

    WebCargo routes on the fragment, and the configured base carries a query
    string (`...?rand=...&ctry=in`). So the search hash is appended directly:
    any existing fragment is dropped first, and no path separator is inserted
    — a "/" here would land inside the query value and corrupt it.
    """
    without_fragment = base_url.split("#", 1)[0]
    return without_fragment + SEARCH_HASH


def verify_authenticated(driver: BrowserDriver, *, base_url: str, timeout_seconds: float) -> None:
    """The persistent session, or a loud stop. Never a login attempt."""
    driver.goto(search_url(base_url))
    if driver.wait_visible(AUTHENTICATED_MARKER, timeout_seconds=timeout_seconds):
        return
    state = driver.evaluate(_JS_LOGIN_CHECK)
    raise WebCargoSessionLost(
        f"the WebCargo search form never appeared (page state: {state!r}); "
        "the persistent session has likely expired"
    )


def run_rate_search(
    driver: BrowserDriver,
    query: RateQuery,
    *,
    base_url: str,
    search_timeout_seconds: float,
    navigation_timeout_seconds: float,
    capture_legs: bool = False,
    poll_interval_seconds: float = 1.0,
) -> WebCargoResultSet:
    """One complete search on an already-authenticated session.

    Fills the form field by explicit field — units are SELECTED, never
    trusted as defaults — submits, waits for the real results surface, and
    harvests every row of every date tab. The returned set carries the
    provider's own count statement, already verified against what was
    actually captured.
    """
    if not query.commodity or not query.commodity.strip():
        raise PermanentFailure(
            "WebCargo requires a commodity and this request states none; "
            "commodity is business data the caller must supply (VR-5)"
        )

    verify_authenticated(driver, base_url=base_url, timeout_seconds=navigation_timeout_seconds)

    _fill_location(driver, ORIGIN_INPUT, query.origin.stated)
    _fill_location(driver, DESTINATION_INPUT, query.destination.stated)

    driver.fill(DATE_INPUT, query.date.strftime("%d/%m/%Y"))
    driver.press(DATE_INPUT, "Enter")

    _fill_commodity(driver, query.commodity)

    driver.fill(UNITS_INPUT, "1")
    driver.fill(LENGTH_INPUT, _figure(query.dimensions_in.length))
    driver.fill(WIDTH_INPUT, _figure(query.dimensions_in.width))
    driver.fill(HEIGHT_INPUT, _figure(query.dimensions_in.height))

    unit_state = driver.evaluate(_JS_SET_DIMENSION_UNIT)
    _require_ok(unit_state, expect_unit="IN", what="dimension unit")

    weight_state = driver.evaluate(_JS_SET_WEIGHT_TOTAL_KG)
    _require_ok(weight_state, expect_unit="KG", what="weight unit")
    driver.fill(WEIGHT_INPUT, _figure(query.weight_kg))

    driver.click(SEARCH_BUTTON)

    settled = _await_results(
        driver, timeout_seconds=search_timeout_seconds, poll_interval_seconds=poll_interval_seconds
    )
    if not settled:  # the provider's own "no rates" — a valid empty outcome
        return WebCargoResultSet(stated_count=0, stated_phrase="", records=())

    # Full list first: its "Showing the N lowest rates" is the authoritative
    # total across all date tabs (the Matrix view reports a different, smaller
    # "cheapest" figure), and the accordion rows only render in this view.
    full_list = driver.evaluate(_JS_ENSURE_FULL_LIST)
    if not (isinstance(full_list, dict) and full_list.get("ok")):
        raise ContractViolation(f"could not reach the Full list view: {full_list!r}")

    phrase = driver.evaluate(_JS_LOWEST_PHRASE)
    if not isinstance(phrase, str) or not phrase:
        raise ContractViolation(
            "the Full-list 'Showing the N lowest rates' total was not found; "
            "refusing to report a candidate set whose size the provider did "
            "not state"
        )

    raw_rows = driver.evaluate(_JS_EXTRACT_ALL, capture_legs)
    if not isinstance(raw_rows, list):
        raise ContractViolation(f"result extraction returned {type(raw_rows).__name__}")

    records = tuple(_record_from(raw) for raw in raw_rows)
    stated_count = _count_from(phrase)

    # The completeness proof: the provider's own total must equal what was
    # captured across every enabled date tab. Any gap means rows were missed,
    # and a missed row presented as a complete set is the one thing this guard
    # exists to forbid.
    if stated_count is None:
        raise ContractViolation(f"could not read a rate count from {phrase!r}")
    if stated_count != len(records):
        raise ContractViolation(
            f"WebCargo states {stated_count} rates ({phrase!r}) but "
            f"{len(records)} rows were captured across the date tabs; "
            "refusing to present an incomplete candidate set as complete"
        )

    return WebCargoResultSet(
        stated_count=stated_count,
        stated_phrase=phrase,
        records=records,
    )


# --- helpers ----------------------------------------------------------------------


def _figure(value: float) -> str:
    """A number the way a person would type it: no trailing .0 noise."""
    return str(int(value)) if float(value).is_integer() else str(value)


def _require_ok(state: object, *, expect_unit: str, what: str) -> None:
    if not (isinstance(state, dict) and state.get("ok")):
        raise ContractViolation(f"could not set the {what}: {state!r}")
    unit = str(state.get("unit", ""))
    if not unit.startswith(expect_unit):
        raise ContractViolation(
            f"the {what} reads {unit!r} after selecting {expect_unit!r}; "
            "refusing to search under an unverified unit"
        )


def _await_results(
    driver: BrowserDriver, *, timeout_seconds: float, poll_interval_seconds: float
) -> bool:
    """Wait for the results surface to settle. ``True`` when the provider has
    a result set to read, ``False`` when it states it found nothing. A page
    that never settles is an error — never silently treated as empty."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        state = driver.evaluate(_JS_RESULTS_STATE)
        if isinstance(state, dict) and state.get("onResults"):
            if state.get("settled"):
                return True
            if state.get("empty"):
                return False
        if time.monotonic() >= deadline:
            raise ContractViolation(
                f"the results surface did not settle within {timeout_seconds}s "
                f"(last state: {state!r})"
            )
        time.sleep(poll_interval_seconds)


def _count_from(phrase: str) -> int | None:
    import re

    match = re.search(r"\b(\d+)\b", phrase)
    return int(match.group(1)) if match else None


def _record_from(raw: object) -> WebCargoRateRecord:
    if not isinstance(raw, dict):
        raise ContractViolation(f"extracted row was {type(raw).__name__}, expected an object")
    legs = tuple(
        FlightLegRecord(**leg) for leg in raw.get("legs", ()) if isinstance(leg, dict)
    )
    return WebCargoRateRecord(
        company=str(raw.get("company", "")),
        itinerary=str(raw.get("itinerary", "")),
        departure=str(raw.get("departure", "")),
        arrival=str(raw.get("arrival", "")),
        duration=str(raw.get("duration", "")),
        service=str(raw.get("service", "")),
        rate=str(raw.get("rate", "")),
        surcharges=str(raw.get("surcharges", "")),
        price=str(raw.get("price", "")),
        date_tab=str(raw.get("date_tab", "")),
        legs=legs,
    )
