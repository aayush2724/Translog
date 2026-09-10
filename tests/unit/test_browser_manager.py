"""The persistent-session lifecycle rules the browser worker depends on.

1. **Persistent session, disposable pages.** One launch serves every job; no
   job ever sees another job's page.
2. **One relaunch, then a loud stop.** A dead session earns a single retry on
   the same profile; a second failure is the supervisor's problem.
3. **An expired session is never logged into.** Jobs fail fast with the
   reason until an operator re-authenticates.

All driven with fakes: the lifecycle layer knows nothing about Playwright or
WebCargo, and these tests prove it needs neither.
"""

from __future__ import annotations

import pytest

from translog_quote.adapters.webcargo.browser import ManagedBrowser, SessionState
from translog_quote.errors import PermanentFailure, TransientFailure


class FakePage:
    def __init__(self, handle_id: int, page_number: int) -> None:
        self.handle_id = handle_id
        self.page_number = page_number
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeHandle:
    def __init__(self, handle_id: int, *, dead: bool = False) -> None:
        self.handle_id = handle_id
        self.dead = dead
        self.closed = False
        self.pages: list[FakePage] = []

    def new_page(self) -> FakePage:
        if self.dead:
            raise RuntimeError("browser process is gone")
        page = FakePage(self.handle_id, len(self.pages))
        self.pages.append(page)
        return page

    def close(self) -> None:
        self.closed = True


class Launcher:
    """A scripted launch factory: hands out handles in order, then refuses."""

    def __init__(self, *handles: FakeHandle) -> None:
        self._handles = list(handles)
        self.calls = 0

    def __call__(self) -> FakeHandle:
        self.calls += 1
        if not self._handles:
            raise RuntimeError("no browser available")
        return self._handles.pop(0)


# --- 1. persistent session, disposable pages ------------------------------------


def test_the_session_is_launched_once_and_reused_across_jobs() -> None:
    launcher = Launcher(FakeHandle(1))
    browser = ManagedBrowser(launcher)

    with browser.job_page() as first, browser.job_page() as second:
        assert first.handle_id == second.handle_id == 1

    assert launcher.calls == 1  # job B rode the SAME session, not a new login


def test_every_job_gets_a_fresh_page_which_dies_with_the_job() -> None:
    """Job B must not inherit job A's search state: new page, old one closed."""
    browser = ManagedBrowser(Launcher(FakeHandle(1)))

    with browser.job_page() as first_page:
        pass
    with browser.job_page() as second_page:
        assert first_page.closed  # A's page was disposed before B ran
        assert second_page is not first_page
        assert not second_page.closed

    assert second_page.closed  # and B's page died with B


def test_the_page_is_closed_even_when_the_job_fails() -> None:
    browser = ManagedBrowser(Launcher(FakeHandle(1)))

    with pytest.raises(RuntimeError, match="job blew up"):  # noqa: SIM117
        with browser.job_page() as page:
            raise RuntimeError("job blew up")

    assert page.closed
    assert browser.state is SessionState.READY  # ready for the next job


def test_states_walk_ready_processing_ready() -> None:
    browser = ManagedBrowser(Launcher(FakeHandle(1)))
    assert browser.state is SessionState.STARTING

    with browser.job_page():
        assert browser.state is SessionState.PROCESSING

    assert browser.state is SessionState.READY


# --- 2. one relaunch, then a loud stop -------------------------------------------


def test_a_dead_session_is_relaunched_once_and_the_job_proceeds() -> None:
    dead, replacement = FakeHandle(1, dead=True), FakeHandle(2)
    launcher = Launcher(dead, replacement)
    browser = ManagedBrowser(launcher)

    with browser.job_page() as page:
        assert page.handle_id == 2  # the job ran on the relaunched session

    assert dead.closed  # the dead handle was not leaked
    assert launcher.calls == 2
    assert browser.state is SessionState.READY


def test_a_failed_relaunch_stops_loudly_for_the_supervisor() -> None:
    launcher = Launcher(FakeHandle(1, dead=True))  # nothing left to relaunch
    browser = ManagedBrowser(launcher)

    with pytest.raises(TransientFailure, match="restarted by its supervisor"), browser.job_page():
        pass  # pragma: no cover - the page is never produced

    assert browser.state is SessionState.BROWSER_FAILED


def test_a_launch_that_never_succeeds_is_transient_not_silent() -> None:
    browser = ManagedBrowser(Launcher())  # empty: launch always raises

    with pytest.raises(TransientFailure):
        browser.ensure_ready()

    assert browser.state is SessionState.BROWSER_FAILED


# --- 3. an expired session is never logged into ----------------------------------


def test_an_expired_session_fails_fast_with_the_reason() -> None:
    launcher = Launcher(FakeHandle(1))
    browser = ManagedBrowser(launcher)
    with browser.job_page():
        pass

    browser.mark_session_expired("WebCargo presented its login page")

    with pytest.raises(PermanentFailure, match="login page"), browser.job_page():
        pass  # pragma: no cover
    assert browser.state is SessionState.SESSION_EXPIRED


def test_expiry_does_not_hammer_the_login_with_relaunches() -> None:
    """Ten failed jobs after expiry mean ten refusals — zero new launches."""
    launcher = Launcher(FakeHandle(1))
    browser = ManagedBrowser(launcher)
    with browser.job_page():
        pass
    browser.mark_session_expired("session expired")

    for _ in range(10):
        with pytest.raises(PermanentFailure):
            browser.ensure_ready()

    assert launcher.calls == 1  # the login page was never revisited


def test_the_refusal_points_at_the_operator_not_at_automation() -> None:
    browser = ManagedBrowser(Launcher(FakeHandle(1)))
    browser.mark_session_expired("session expired")

    with pytest.raises(PermanentFailure) as raised:
        browser.ensure_ready()

    message = str(raised.value)
    assert "operator" in message
    assert "No automated login" in message


def test_close_disposes_the_handle() -> None:
    handle = FakeHandle(1)
    browser = ManagedBrowser(Launcher(handle))
    browser.ensure_ready()

    browser.close()

    assert handle.closed
