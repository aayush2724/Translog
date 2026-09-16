"""adapters.webcargo.browser — the persistent-session browser layer.

    ManagedBrowser          — lifecycle state machine; session persists,
                              pages do not
    driver                  — the only Playwright touchpoint
    pages                   — every WebCargo selector + the search flow,
                              written against inspected UI evidence only
    records                 — verbatim provider-shaped capture
    mapper                  — capture -> canonical Rate (nothing invented)
    WebCargoBrowserAdapter  — the real RateSearchPort (is_simulated=False)
"""

from translog_quote.adapters.webcargo.browser.adapter import WebCargoBrowserAdapter
from translog_quote.adapters.webcargo.browser.driver import (
    PlaywrightHandle,
    PlaywrightWebCargoDriver,
    launch_persistent_chromium,
)
from translog_quote.adapters.webcargo.browser.manager import (
    BrowserHandle,
    ManagedBrowser,
    SessionState,
)
from translog_quote.adapters.webcargo.browser.pages import (
    BrowserDriver,
    run_rate_search,
)
from translog_quote.adapters.webcargo.browser.records import (
    FlightLegRecord,
    WebCargoRateRecord,
    WebCargoResultSet,
)
from translog_quote.errors import WebCargoSessionLost

__all__ = [
    "BrowserDriver",
    "BrowserHandle",
    "FlightLegRecord",
    "ManagedBrowser",
    "PlaywrightHandle",
    "PlaywrightWebCargoDriver",
    "SessionState",
    "WebCargoBrowserAdapter",
    "WebCargoRateRecord",
    "WebCargoResultSet",
    "WebCargoSessionLost",
    "launch_persistent_chromium",
    "run_rate_search",
]
