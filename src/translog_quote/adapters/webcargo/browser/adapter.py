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
    is_authenticated,
    run_rate_search,
)
from translog_quote.adapters.webcargo.browser.reauth import run_operator_login
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

    def ensure_authenticated(
        self, *, interactive: bool, prompt: Callable[[str], str] = input
    ) -> None:
        """Bring the long-lived session to the authenticated search surface
        before any job runs — on the SAME context the worker serves from.

        Leases one page (closed here); the context, and its in-memory session
        cookie, stay alive so every later job rides the very session the
        operator signed into. No cookie is ever exported or re-injected.

        ``interactive=True``  — an operator may sign in, in the open window,
        once; only a visible search form counts as success.
        ``interactive=False`` — an unauthenticated session is refused (marked
        expired), never logged into automatically and never on a new browser.
        """
        with self._manager.job_page() as page:
            driver = self._wrap_page(page)
            if is_authenticated(
                driver, base_url=self._base_url, timeout_seconds=self._navigation_timeout
            ):
                return
            if not interactive:
                reason = (
                    "the WebCargo session is not authenticated at worker startup; "
                    "start the worker with --login so an operator can sign in"
                )
                self._manager.mark_session_expired(reason)
                raise WebCargoSessionLost(reason)
            try:
                run_operator_login(
                    driver,
                    base_url=self._base_url,
                    navigation_timeout_seconds=self._navigation_timeout,
                    prompt=prompt,
                )
            except WebCargoSessionLost:
                self._manager.mark_session_expired(
                    "operator sign-in did not reach the WebCargo search form"
                )
                raise

    def close(self) -> None:
        """Shut the persistent session down. The worker calls this once, at
        process exit — never between jobs."""
        self._manager.close()
