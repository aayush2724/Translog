"""The persistent-browser lifecycle, as an explicit state machine.

    STARTING -> READY -> PROCESSING -> READY -> ...
                   \\-> SESSION_EXPIRED   (operator re-authentication path)
                   \\-> BROWSER_FAILED    (one relaunch, then a loud stop)

One browser session, many jobs. The session — cookies, authentication, the
Chromium profile — persists across jobs; the *page* a job works in does not.
`job_page` hands every job a page created for it alone and closes it on the
way out, so job B can never inherit job A's search state. Persistent SESSION,
disposable SEARCH PAGE.

This module knows nothing about WebCargo: no URL, no selector, no login form.
It manages whatever the injected launcher produces, which is what makes it
testable with a fake and keeps every UI fact out of the lifecycle layer.
"""

from __future__ import annotations

from contextlib import contextmanager, suppress
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from translog_quote.errors import PermanentFailure, TransientFailure

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


class BrowserHandle[PageT](Protocol):
    """The minimal surface the lifecycle needs from a live browser session."""

    def new_page(self) -> PageT: ...

    def close(self) -> None: ...


class SessionState(StrEnum):
    STARTING = "starting"
    READY = "ready"
    PROCESSING = "processing"
    SESSION_EXPIRED = "session_expired"
    BROWSER_FAILED = "browser_failed"


class ManagedBrowser[PageT]:
    """Owns one long-lived browser session and leases fresh pages to jobs.

    `launch` is called once and its handle reused for every job — never per
    job, and never per login. A dead handle earns exactly one relaunch; a
    second consecutive failure stops loudly (`TransientFailure`) so the
    process supervisor restarts the worker rather than the worker thrashing.

    An expired provider session is *not* recovered here: recovery would mean
    logging in, and logging in may mean MFA or CAPTCHA, which belong to a
    person. `mark_session_expired` fails the current job with the reason and
    makes every later job fail fast the same way — no repeated visits to a
    login page — until the operator re-authenticates and the worker restarts.
    """

    def __init__(self, launch: Callable[[], BrowserHandle[PageT]]) -> None:
        self._launch = launch
        self._handle: BrowserHandle[PageT] | None = None
        self._state = SessionState.STARTING
        self._expired_reason: str | None = None

    @property
    def state(self) -> SessionState:
        return self._state

    def ensure_ready(self) -> None:
        """A live session, launching one if needed. Fail-fast when expired."""
        if self._state is SessionState.SESSION_EXPIRED:
            raise PermanentFailure(self._expired_message())
        if self._handle is None:
            self._state = SessionState.STARTING
            self._handle = self._launch_or_fail()
        self._state = SessionState.READY

    @contextmanager
    def job_page(self) -> Iterator[PageT]:
        """One fresh page for one job, on the shared session.

        The page is created here and closed here, unconditionally. A crashed
        session is relaunched once and the job gets its page on the new
        session; an expired session refuses immediately.
        """
        self.ensure_ready()
        self._state = SessionState.PROCESSING

        try:
            page = self._new_page_with_one_relaunch()
        except Exception:
            if self._state is not SessionState.SESSION_EXPIRED:
                self._state = SessionState.BROWSER_FAILED
            raise

        try:
            yield page
        finally:
            self._close_quietly(page)
            if self._state is SessionState.PROCESSING:
                self._state = SessionState.READY

    def mark_session_expired(self, reason: str) -> None:
        """Called by the adapter when the provider demands a login.

        From here on every job refuses with the reason instead of driving at
        the login page. The worker does not authenticate: the operator does,
        through the explicit re-authentication command.
        """
        self._expired_reason = reason
        self._state = SessionState.SESSION_EXPIRED

    def close(self) -> None:
        if self._handle is not None:
            self._close_quietly(self._handle)
            self._handle = None

    # --- internals ----------------------------------------------------------

    def _new_page_with_one_relaunch(self) -> PageT:
        assert self._handle is not None, "job_page ran without ensure_ready"
        try:
            return self._handle.new_page()
        except Exception:
            # The session died under us. One relaunch: the profile on disk
            # still holds the authentication state, so a fresh handle is the
            # same session — not a new login.
            self._close_quietly(self._handle)
            self._handle = self._launch_or_fail()
            return self._handle.new_page()

    def _launch_or_fail(self) -> BrowserHandle[PageT]:
        try:
            return self._launch()
        except Exception as exc:
            self._state = SessionState.BROWSER_FAILED
            self._handle = None
            raise TransientFailure(
                f"browser session could not be launched: {type(exc).__name__}. "
                "The worker should exit and be restarted by its supervisor."
            ) from exc

    def _expired_message(self) -> str:
        detail = self._expired_reason or "the provider session has expired"
        return (
            f"WebCargo session unavailable: {detail}. Jobs will fail until an "
            "operator re-authenticates (run the worker's login command) and "
            "the worker is restarted. No automated login is attempted."
        )

    @staticmethod
    def _close_quietly(closeable: object) -> None:
        close = getattr(closeable, "close", None)
        if close is None:
            return
        # Teardown must never mask the job's own outcome.
        with suppress(Exception):
            close()
