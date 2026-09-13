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

import re
import time
from typing import TYPE_CHECKING, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from translog_quote.adapters.webcargo.browser.records import (
    FlightLegRecord,
    WebCargoRateRecord,
    WebCargoResultSet,
)
from translog_quote.errors import ContractViolation, PermanentFailure, UnresolvedLocation

if TYPE_CHECKING:
    from datetime import date

    from translog_quote.domain.rates import RateQuery

# --- where things are -------------------------------------------------------------

SEARCH_HASH = "#ebookings/search-and-book"
RESULTS_HASH = "#ebookings/dynamic-results"

# The search form (verified ids and placeholders).
#
# The `input` tag qualifier is load-bearing: WebCargo puts the id on BOTH the
# AntD `<div class="ant-select">` wrapper and its inner `<input>`, so a bare
# `#originAirport` matches two elements and Playwright's `fill` grabs the
# unfillable div first. `input#…` resolves to the one fillable field (verified
# live), and stays a valid visible marker for the authenticated shell.
ORIGIN_INPUT = "input#originAirport"
DESTINATION_INPUT = "input#destinationAirport"
DATE_INPUT = ".ant-calendar-picker-input"
#: The departure-date field. The visible input carries a stable `data-cy` hook
#: and is READONLY — it cannot be typed into directly. Clicking it opens the
#: AntD calendar panel, whose own `.ant-calendar-input` IS editable; typing the
#: date there and pressing Enter commits it back to the readonly display.
DATE_TRIGGER = 'input[data-cy="departureDate"]'
CALENDAR_INPUT = ".ant-calendar-input"

#: The Goods Type / commodity search field. Scoped to the Goods Type select's
#: stable ``id="goodsType"`` hook (``^=`` tolerates a trailing space in the
#: attribute), so it is the ONE commodity field — never the left-nav "Find a
#: page" search or any other ``.ant-select-search__field``, which a bare,
#: id-excluding selector matched once origin/destination were chosen.
COMMODITY_INPUT = '[id^="goodsType"] .ant-select-search__field'
#: The visible Goods Type control to open before the (hidden) search field can
#: be used. WebCargo puts the ``goodsType`` id on the AntD ``<div
#: class="ant-select">`` wrapper itself (see the ORIGIN_INPUT note), so this
#: scopes to that one wrapper — clicking it opens the select and reveals
#: ``COMMODITY_INPUT``. The inner ``.ant-select-search__field`` does not carry
#: the ``ant-select`` class, so this never resolves to the hidden input.
COMMODITY_CONTROL = '.ant-select[id^="goodsType"]'
UNITS_INPUT = "#units-0"
LENGTH_INPUT = 'input[placeholder="Length"]'
WIDTH_INPUT = 'input[placeholder="Width"]'
HEIGHT_INPUT = 'input[placeholder="Height"]'
WEIGHT_INPUT = "#weight-0"
SEARCH_BUTTON = "button.searchFlights"

#: Location/commodity suggestions render into AntD dropdowns.
#
# The `:not(...-disabled)` is load-bearing: the origin/destination lookup is
# a debounced async request, and while it is in flight WebCargo shows a
# *disabled* placeholder `li` reading "No results found". Without the
# exclusion, `wait_visible` returns on that placeholder and `option_texts`
# reads "No results found" before the real options arrive, so a valid place
# is refused as unresolved. Excluding the disabled placeholder makes the wait
# hold until a genuine option appears (verified live), and never reads the
# placeholder as an option. Harmless for the commodity dropdown too.
DROPDOWN_OPTION = (
    ".ant-select-dropdown li.ant-select-dropdown-menu-item"
    ":not(.ant-select-dropdown-menu-item-disabled)"
)

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
  // Live-observed empty-state wording (2026-09-13): a zero-rate search shows
  // "No results found" / "No results found for your search criteria." and
  // "There may be no results because:" — none of which the older patterns
  // matched, so an empty search hung until timeout. Still gated by !anyCount
  // below, so these never mark a rates-bearing page (which also shows the
  // "No results found for your search criteria" section) as empty.
  const noRates = short(/No rates? (?:were )?found/i) || short(/no results for your search/i)
    || short(/No results found/i) || short(/There may be no results/i);
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

    raise LookupError(f"{stated!r} did not match exactly one provider option; offered: {cleaned!r}")


#: How often the option list is re-checked while the debounced async lookup
#: settles. This is the cadence of a *condition* wait — the wait returns the
#: instant a matching option exists — not a fixed delay that assumes readiness.
_OPTION_POLL_SECONDS = 0.25


def _choose_available_option(
    driver: BrowserDriver, container: str, stated: str, *, timeout_seconds: float
) -> str:
    """Wait until an ENABLED option matching `stated` exists, then return it.

    The origin/destination/commodity lookups are debounced async requests: for
    the first moment the only option is the disabled "No results found"
    placeholder (already excluded by ``container``), and the real results
    arrive a second or two later — longer under a loaded or virtual display.
    So rather than reading the list once, this re-applies the deterministic
    ``_choose_option`` match until a unique match appears, and returns it the
    instant it does.

    The placeholder is never mistaken for readiness (the selector excludes it)
    and nothing is ever guessed (the match stays ``_choose_option``). A
    ``LookupError`` is raised only when no unique match has appeared by the
    deadline; the caller turns that into the appropriate refusal.
    ``timeout_seconds`` is the caller's configured navigation timeout — no new
    timeout is introduced.
    """
    deadline = time.monotonic() + timeout_seconds
    last_error: LookupError | None = None
    while True:
        try:
            return _choose_option(stated, driver.option_texts(container))
        except LookupError as exc:
            last_error = exc
        if time.monotonic() >= deadline:
            assert last_error is not None
            raise last_error
        time.sleep(_OPTION_POLL_SECONDS)


def _date_input_value(driver: BrowserDriver) -> str:
    """The departure-date display value, as WebCargo currently shows it.

    Read-only observation via `evaluate` — used to drive the interaction and to
    verify the outcome, never to set the date (which goes through the UI)."""
    value = driver.evaluate(
        "() => { const e = document.querySelector('input[data-cy=\"departureDate\"]');"
        " return e ? e.value : ''; }"
    )
    return str(value) if value is not None else ""


def _fill_departure_date(driver: BrowserDriver, requested: date, *, timeout_seconds: float) -> None:
    """Set the departure date through the AntD DatePicker UI, and verify it.

    The visible ``input[data-cy="departureDate"]`` is readonly and carries a
    default date, so it cannot be filled directly. Clicking it opens the
    calendar panel; the panel's ``.ant-calendar-input`` is editable, so the
    requested date is typed there (DD/MM/YYYY, the UI's format) and committed
    with Enter. The readonly display is then polled until it shows exactly the
    requested date — the pre-filled default is never silently accepted, and a
    date that will not take is a loud failure rather than a wrong quotation.

    Idempotent: if the field already shows the requested date, it returns
    without touching the picker.
    """
    wanted = requested.strftime("%d/%m/%Y")
    if _date_input_value(driver) == wanted:
        return

    driver.click(DATE_TRIGGER)
    if not driver.wait_visible(CALENDAR_INPUT, timeout_seconds=timeout_seconds):
        raise ContractViolation(
            "the WebCargo departure-date calendar did not open; cannot set the date"
        )
    driver.fill(CALENDAR_INPUT, wanted)
    driver.press(CALENDAR_INPUT, "Enter")

    deadline = time.monotonic() + timeout_seconds
    while _date_input_value(driver) != wanted:
        if time.monotonic() >= deadline:
            raise ContractViolation(
                f"the departure date still reads {_date_input_value(driver)!r} after "
                f"selecting {wanted!r}; refusing to search under an unconfirmed date"
            )
        time.sleep(_OPTION_POLL_SECONDS)


#: A parenthesized 3-letter IATA code the client stated explicitly — the "BOM"
#: in "Mumbai (BOM)". Live-observed: WebCargo's airport autocomplete matches on
#: the code, and its options are labelled "CODE - City" (BOM is "Bombay"), so a
#: city name can return nothing while the code returns the airport.
_PARENTHESIZED_IATA = re.compile(r"\(([A-Za-z]{3})\)")


def _location_query_token(stated: str) -> str:
    """The token to type into WebCargo's location autocomplete.

    When the client explicitly wrote a single parenthesized 3-letter IATA code
    — "Mumbai (BOM)" — that code ("BOM") is used as the query, because that is
    what WebCargo autocompletes on. Nothing is inferred: only a code the client
    themselves stated is used. Anything else — a bare code ("BOM"), a bare city
    ("Mumbai"), or a string with no single unambiguous parenthesized code — is
    returned unchanged, so it reaches WebCargo exactly as before and still fails
    closed there when the provider offers no unique match. No city→airport
    mapping and no fuzzy matching is introduced.
    """
    codes: list[str] = _PARENTHESIZED_IATA.findall(stated)
    if len(codes) == 1:
        return codes[0]
    return stated


def _fill_location(
    driver: BrowserDriver, selector: str, stated: str, *, timeout_seconds: float
) -> str:
    """Type the stated place, select WebCargo's own suggestion, close the list.

    Returns the option text actually selected — the resolver evidence. When the
    client stated a parenthesized IATA code, WebCargo is queried with that code
    (see ``_location_query_token``); the deterministic ``_choose_option`` match
    is then applied to the same token, unchanged.
    """
    query = _location_query_token(stated)
    driver.click(selector)
    driver.fill(selector, query)
    try:
        chosen = _choose_available_option(
            driver, DROPDOWN_OPTION, query, timeout_seconds=timeout_seconds
        )
    except LookupError as exc:
        raise UnresolvedLocation(
            f"WebCargo offered no matching location suggestion for {stated!r} "
            f"(queried as {query!r}); refusing rather than guessing an airport ({exc})"
        ) from exc
    driver.click_option(DROPDOWN_OPTION, chosen)
    driver.press(selector, "Escape")  # the list lingers and swallows clicks
    return chosen


def _fill_commodity(driver: BrowserDriver, stated: str, *, timeout_seconds: float) -> str:
    """Select the WebCargo commodity matching the caller's wording, exactly.

    The commodity select is the Goods Type AntD select; suggestions look
    like "0000 - General Cargo". No match, no search — and never a default.

    The AntD select keeps its inner search field hidden until the select is
    opened, so the visible control is engaged first and the (now revealed)
    scoped search field is waited for before anything is typed into it —
    typing into a hidden field is what timed out the click before.
    """
    driver.click(COMMODITY_CONTROL)
    if not driver.wait_visible(COMMODITY_INPUT, timeout_seconds=timeout_seconds):
        raise PermanentFailure(
            "the WebCargo Goods Type search field never became visible after "
            "opening the commodity select; the search cannot choose a commodity blind"
        )
    driver.fill(COMMODITY_INPUT, stated)
    try:
        chosen = _choose_available_option(
            driver, DROPDOWN_OPTION, stated, timeout_seconds=timeout_seconds
        )
    except LookupError as exc:
        raise PermanentFailure(
            f"WebCargo offered no matching commodity option for {stated!r}; the "
            f"search cannot run under a commodity nobody stated ({exc})"
        ) from exc
    driver.click_option(DROPDOWN_OPTION, chosen)
    return chosen


#: A brief check for the form on the CURRENT page before deciding to navigate.
#: Long enough for an already-rendered form to be seen instantly (Playwright
#: returns as soon as the selector is visible), short enough that a blank
#: fresh page falls through to navigation without a real stall.
_CURRENT_PAGE_PROBE_SECONDS = 2.0


def search_url(base_url: str) -> str:
    """The canonical Search & Book URL for a configured base.

    WebCargo routes on the fragment, so any existing fragment is dropped and
    the search hash appended. The volatile ``rand`` cache-buster is stripped:
    it is a single-use nonce, and re-navigating to a stale one redirects an
    authenticated session back to login. Every other query parameter (e.g.
    ``ctry``, the country context) is preserved verbatim. No ``rand`` is ever
    invented — the canonical URL simply omits it.
    """
    parts = urlsplit(base_url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "rand"]
    canonical = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))
    return canonical + SEARCH_HASH


def _search_form_visible(driver: BrowserDriver, *, timeout_seconds: float) -> bool:
    """Whether the authenticated search form is on the CURRENT page. No navigation."""
    return driver.wait_visible(AUTHENTICATED_MARKER, timeout_seconds=timeout_seconds)


def is_authenticated(driver: BrowserDriver, *, base_url: str, timeout_seconds: float) -> bool:
    """Whether the authenticated shell renders, navigating to the canonical
    search surface first. A read-only probe.

    It never fills, submits, or bypasses any login control — the answer is
    "yes/no", never a sign-in."""
    driver.goto(search_url(base_url))
    return _search_form_visible(driver, timeout_seconds=timeout_seconds)


def verify_authenticated(driver: BrowserDriver, *, base_url: str, timeout_seconds: float) -> None:
    """Ensure the driver sits on the authenticated Search & Book form, or stop.

    The CURRENT page is checked first, so an operator who has just signed in
    and is already looking at the form is accepted WITHOUT navigating away from
    it — a fresh navigation to the app can bounce a just-established session
    back to login. Only when the form is not already present do we navigate to
    the canonical (rand-free) search URL and check again. A login is never
    attempted; if the form cannot be reached, the session is declared lost.
    """
    if _search_form_visible(driver, timeout_seconds=_CURRENT_PAGE_PROBE_SECONDS):
        return
    driver.goto(search_url(base_url))
    if _search_form_visible(driver, timeout_seconds=timeout_seconds):
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

    _fill_location(
        driver, ORIGIN_INPUT, query.origin.stated, timeout_seconds=navigation_timeout_seconds
    )
    _fill_location(
        driver,
        DESTINATION_INPUT,
        query.destination.stated,
        timeout_seconds=navigation_timeout_seconds,
    )

    _fill_departure_date(driver, query.date, timeout_seconds=navigation_timeout_seconds)

    _fill_commodity(driver, query.commodity, timeout_seconds=navigation_timeout_seconds)

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
    legs = tuple(FlightLegRecord(**leg) for leg in raw.get("legs", ()) if isinstance(leg, dict))
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
