"""adapters.webcargo.browser — the persistent-session browser layer.

    ManagedBrowser   — lifecycle state machine; session persists, pages do not
    driver           — the only Playwright touchpoint (persistent Chromium)

The rate-search adapter, page interactions and extraction land here in later
phases, written only against inspected WebCargo UI evidence.
"""

from translog_quote.adapters.webcargo.browser.driver import (
    PlaywrightHandle,
    launch_persistent_chromium,
)
from translog_quote.adapters.webcargo.browser.manager import (
    BrowserHandle,
    ManagedBrowser,
    SessionState,
)

__all__ = [
    "BrowserHandle",
    "ManagedBrowser",
    "PlaywrightHandle",
    "SessionState",
    "launch_persistent_chromium",
]
