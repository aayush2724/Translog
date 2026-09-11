"""The browser adapter's session guarantees, over fake browser and driver.

Reuses the fake handle/launcher infrastructure from the lifecycle tests and
the scripted driver from the extraction tests, so the adapter is exercised
end to end — persistent session, disposable pages, expiry, provenance —
with no Playwright and no WebCargo anywhere.
"""

from __future__ import annotations

from typing import Any

import pytest
from tests.unit.test_browser_manager import FakeHandle, Launcher
from tests.unit.test_webcargo_extraction import QUERY, FakeDriver, rows_payload

from translog_quote.adapters.webcargo.browser import (
    ManagedBrowser,
    SessionState,
    WebCargoBrowserAdapter,
    WebCargoSessionLost,
)
from translog_quote.domain.rates import FASTEST_ELIGIBLE, filter_rates, select_rate
from translog_quote.errors import PermanentFailure


def adapter_over(
    launcher: Launcher, drivers: list[FakeDriver]
) -> tuple[WebCargoBrowserAdapter, list[FakeDriver]]:
    """An adapter whose pages are wrapped by the scripted drivers, in order."""
    handed: list[FakeDriver] = []

    def wrap(page: Any) -> FakeDriver:
        driver = drivers[len(handed)] if len(handed) < len(drivers) else drivers[-1]
        handed.append(driver)
        return driver

    return (
        WebCargoBrowserAdapter(
            manager=ManagedBrowser(launcher),
            base_url="https://example.invalid/app/",
            wrap_page=wrap,
            search_timeout_seconds=0.05,
            navigation_timeout_seconds=1,
        ),
        handed,
    )


def test_one_session_serves_many_jobs_each_on_a_fresh_page() -> None:
    """The persistent-session requirement, end to end: one launch, one
    login-holding profile — and a page per job that dies with the job."""
    handle = FakeHandle(1)
    launcher = Launcher(handle)
    adapter, handed = adapter_over(
        launcher, [FakeDriver(rows=rows_payload(2)), FakeDriver(rows=rows_payload(3))]
    )

    first = adapter.search(QUERY)
    second = adapter.search(QUERY)

    assert launcher.calls == 1  # same session, no relaunch, no login
    assert len(handle.pages) == 2  # a FRESH page per job...
    assert all(page.closed for page in handle.pages)  # ...closed in finally
    assert len(first.rates) == 2
    assert len(second.rates) == 3


def test_results_are_live_provider_data_with_the_completeness_statement() -> None:
    adapter, _ = adapter_over(Launcher(FakeHandle(1)), [FakeDriver(rows=rows_payload(3))])

    result = adapter.search(QUERY)

    assert result.is_simulated is False  # the one adapter allowed to say so
    assert result.adapter_id == "webcargo-browser"
    assert result.completeness == "Showing the 3 lowest rates"
    assert isinstance(result.raw_payload, dict)  # the verbatim capture, for audit
    assert result.raw_payload["stated_count"] == 3


def test_the_adapter_supplies_candidates_and_the_domain_decides() -> None:
    rows = rows_payload(2)
    rows[0].update(service="TK URGENT", duration="51h 20m", price="a/kg/ 10,000 Rs")
    rows[1].update(service="QR General", duration="18h 45m", price="a/kg/ 99,000 Rs")
    adapter, _ = adapter_over(Launcher(FakeHandle(1)), [FakeDriver(rows=rows)])

    result = adapter.search(QUERY)
    outcome = filter_rates(result.rates)
    selection = select_rate(outcome.eligible, FASTEST_ELIGIBLE)

    assert selection is not None
    assert selection.rate.carrier_code == "QR"  # fastest wins; price only ties


def test_an_expired_session_fails_this_job_and_every_next_one_fast() -> None:
    handle = FakeHandle(1)
    launcher = Launcher(handle)
    adapter, _ = adapter_over(launcher, [FakeDriver(authenticated=False)])

    with pytest.raises(WebCargoSessionLost):
        adapter.search(QUERY)

    manager_state = adapter._manager.state  # noqa: SLF001 - asserting the machine
    assert manager_state is SessionState.SESSION_EXPIRED
    assert handle.pages[0].closed  # the page still died with the job

    with pytest.raises(PermanentFailure, match="No automated login"):
        adapter.search(QUERY)
    assert launcher.calls == 1  # and the login page was never hammered


def test_a_crashed_session_is_relaunched_once_and_the_job_completes() -> None:
    dead, replacement = FakeHandle(1, dead=True), FakeHandle(2)
    launcher = Launcher(dead, replacement)
    adapter, _ = adapter_over(launcher, [FakeDriver(rows=rows_payload(1))])

    result = adapter.search(QUERY)

    assert len(result.rates) == 1
    assert launcher.calls == 2  # one relaunch of the SAME profile, not a login
