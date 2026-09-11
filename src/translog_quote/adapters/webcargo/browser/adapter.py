"""WebCargoBrowserAdapter — the real `RateSearchPort`, over the persistent session.

What this class does: lease one fresh page from the long-lived authenticated
session, run one search, capture what WebCargo displayed, normalise it, and
hand the candidates back. What it never does: filter, rank, or choose — the
port's contract ("implementations filter nothing and rank nothing") is the
domain's guarantee that no provider decides a quotation.

Session semantics, made concrete:

    persistent SESSION   — `ManagedBrowser` launches once; every job reuses it
    disposable PAGE      — `job_page()` creates and always closes per job
    expired SESSION      — detected, reported, never re-logged-in from here

Every result is `is_simulated=False`: these are the provider's own displayed
figures, captured verbatim in `raw_payload` for audit, with the provider's
own candidate-set statement carried in `completeness` so nobody downstream
can promote "the N rates WebCargo returned" into "all rates that exist".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from translog_quote.adapters.webcargo.browser.mapper import ADAPTER_ID, map_records
from translog_quote.adapters.webcargo.browser.pages import (
    WebCargoSessionLost,
    run_rate_search,
)
from translog_quote.domain.rates import RateSearchResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from translog_quote.adapters.webcargo.browser.manager import ManagedBrowser
    from translog_quote.adapters.webcargo.browser.pages import BrowserDriver
    from translog_quote.domain.rates import RateQuery


class WebCargoBrowserAdapter:
    """A `RateSearchPort` that reads the live WebCargo eBooking surface."""

    adapter_id = ADAPTER_ID

    def __init__(
        self,
        *,
        manager: ManagedBrowser[Any],
        base_url: str,
        wrap_page: Callable[[Any], BrowserDriver],
        search_timeout_seconds: float,
        navigation_timeout_seconds: float,
        capture_legs: bool = False,
    ) -> None:
        self._manager = manager
        self._base_url = base_url
        self._wrap_page = wrap_page
        self._search_timeout = search_timeout_seconds
        self._navigation_timeout = navigation_timeout_seconds
        self._capture_legs = capture_legs

    def search(self, query: RateQuery) -> RateSearchResult:
        """One search on one fresh page. The session outlives it; the page
        does not (`job_page` closes it unconditionally)."""
        with self._manager.job_page() as page:
            driver = self._wrap_page(page)
            try:
                result_set = run_rate_search(
                    driver,
                    query,
                    base_url=self._base_url,
                    search_timeout_seconds=self._search_timeout,
                    navigation_timeout_seconds=self._navigation_timeout,
                    capture_legs=self._capture_legs,
                )
            except WebCargoSessionLost as expiry:
                # Fail this job with the reason and make every later job
                # fail fast too — the operator command is the only way back
                # to an authenticated profile. No login page is revisited.
                self._manager.mark_session_expired(str(expiry))
                raise

        return RateSearchResult(
            rates=map_records(result_set.records),
            adapter_id=ADAPTER_ID,
            raw_payload=result_set.model_dump(mode="json"),
            is_simulated=False,
            completeness=result_set.stated_phrase or None,
        )

    def close(self) -> None:
        """Shut the persistent session down. The worker calls this once, at
        process exit — never between jobs."""
        self._manager.close()
