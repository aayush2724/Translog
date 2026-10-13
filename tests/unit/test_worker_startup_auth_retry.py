"""The startup auth probe is retried on the SAME live context — end to end.

Regression for the crash loop in the worker journal (2026-09-28/29): the first
non-interactive probe saw WebCargo's login page and the adapter marked the
session expired BEFORE raising `WebCargoSessionLost`. `_authenticate_with_retry`
caught that and probed again — straight into `ManagedBrowser.ensure_ready()`'s
fail-fast `PermanentFailure`, which nothing caught. Exit 1, systemd restart,
fresh Chromium, every ~2 min; "auth probe 2/3" never once appeared, and the
exit-78 needs-login path was unreachable.

The tests that existed faked either the provider or the retry, so the composed
adapter + manager + retry behaviour was never exercised. These compose the REAL
`ManagedBrowser`, the REAL `WebCargoBrowserAdapter` and the REAL
`_authenticate_with_retry` / `run_worker`, over the same fake browser handle and
scripted drivers the adapter tests use. No Playwright, no Redis, no WebCargo.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from tests.unit.test_browser_manager import FakeHandle, Launcher
from tests.unit.test_webcargo_adapter import adapter_over
from tests.unit.test_webcargo_extraction import QUERY, FakeDriver

from translog_quote.adapters.webcargo.browser import SessionState
from translog_quote.config import WebCargoMode
from translog_quote.errors import PermanentFailure, WebCargoSessionLost
from translog_quote.interface.worker import main as worker_main

LOGIN_PAGE = False  # FakeDriver(authenticated=False) renders the login page
SEARCH_FORM = True


def _settings() -> Any:
    """The few settings `run_worker` reads before and after the auth probe."""
    return SimpleNamespace(
        queue=SimpleNamespace(
            worker_lock_ttl_seconds=120,
            rate_search_queue="rate-search",
            job_timeout_seconds=600,
        ),
        webcargo=SimpleNamespace(
            startup_unreachable_max_wait_seconds=120.0, mode=WebCargoMode.BROWSER
        ),
    )


class _Lock:
    def __init__(self, seen: dict[str, Any]) -> None:
        self._seen = seen

    def release(self) -> None:
        self._seen["released"] = True


class _Heartbeat:
    def __init__(self, *a: object, **k: object) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


def _wire_run_worker(
    monkeypatch: pytest.MonkeyPatch, adapter: object, seen: dict[str, Any]
) -> None:
    """Stub the Redis-backed edges of `run_worker`. The auth path stays REAL:
    the real retry over the real adapter over the real manager."""
    monkeypatch.setattr(worker_main, "_AUTH_PROBE_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(worker_main, "acquire_worker_lock", lambda _s: _Lock(seen))
    monkeypatch.setattr(worker_main, "LockHeartbeat", _Heartbeat)
    monkeypatch.setattr(worker_main, "build_provider", lambda _s: adapter)
    monkeypatch.setattr(
        worker_main, "publish_worker_needs_login", lambda _s: seen.__setitem__("needs_login", True)
    )
    monkeypatch.setattr(
        worker_main, "clear_worker_status", lambda _s: seen.__setitem__("cleared", True)
    )


# --- 1-2. the first probe reports the login page and leaves the manager usable -------


def test_the_first_startup_probe_reports_a_login_page_without_poisoning_the_manager() -> None:
    handle = FakeHandle(1)
    launcher = Launcher(handle)
    adapter, handed = adapter_over(launcher, [FakeDriver(authenticated=LOGIN_PAGE)])

    with pytest.raises(WebCargoSessionLost):
        adapter.ensure_authenticated(interactive=False)

    manager = adapter._manager  # noqa: SLF001 - asserting the machine
    assert manager.state is SessionState.READY  # NOT SESSION_EXPIRED
    assert len(handed) == 1  # exactly one probe happened
    assert handle.pages[0].closed  # the probe page was disposed
    assert not handle.closed  # the live context stays for the next probe
    # The decisive check: a second lease on the same context is still granted,
    # where the old behaviour fail-fasted with PermanentFailure.
    with manager.job_page() as page:
        assert page.handle_id == 1


# --- 3-4. the retry gets its second probe; a recoverable start never exits -----------


def test_the_retry_probes_again_on_the_same_context_and_succeeds() -> None:
    handle = FakeHandle(1)
    launcher = Launcher(handle)
    adapter, handed = adapter_over(
        launcher, [FakeDriver(authenticated=LOGIN_PAGE), FakeDriver(authenticated=SEARCH_FORM)]
    )
    sleeps: list[float] = []

    worker_main._authenticate_with_retry(adapter, interactive=False, sleep=sleeps.append)

    assert len(handed) == 2  # probe 1 saw the login page, probe 2 the search form
    assert sleeps == [worker_main._AUTH_PROBE_BACKOFF_SECONDS]  # one back-off between
    assert launcher.calls == 1  # the SAME context, never a relaunch
    assert not handle.closed
    assert adapter._manager.state is SessionState.READY  # noqa: SLF001
    assert all(page.closed for page in handle.pages)  # both probe pages disposed


def test_a_recoverable_startup_condition_does_not_terminate_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run_worker` with the REAL retry over the REAL adapter and manager: a
    cold-start login-page render on probe 1 is absorbed, and the worker goes on
    to serve the queue instead of exiting."""
    handle = FakeHandle(1)
    adapter, handed = adapter_over(
        Launcher(handle),
        [FakeDriver(authenticated=LOGIN_PAGE), FakeDriver(authenticated=SEARCH_FORM)],
    )
    seen: dict[str, Any] = {}

    class _Worker:
        def __init__(self, *a: object, **k: object) -> None:
            pass

        def work(self, **_kw: object) -> None:
            seen["served"] = True  # the job loop was reached: the worker lived

    _wire_run_worker(monkeypatch, adapter, seen)
    monkeypatch.setattr(worker_main, "_drain_pending_requeue", lambda _s: None)
    monkeypatch.setattr(worker_main, "build_redis", lambda _s: object())
    monkeypatch.setattr(worker_main, "Queue", lambda *a, **k: object())
    monkeypatch.setattr(worker_main, "_LockRefreshingWorker", _Worker)

    code = worker_main.run_worker(_settings())

    assert code == 0  # a normal stop: not 1 (the crash) and not 78
    assert len(handed) == 2  # both probes ran
    assert seen.get("served") is True
    assert seen.get("cleared") is True  # a live session clears a stale needs_login
    assert "needs_login" not in seen
    assert seen.get("released") is True
    assert handle.closed  # provider.close() at exit, as always


# --- the genuinely expired path: probes spent → exit 78, never a crash ---------------


def test_a_persistently_unauthenticated_session_spends_the_probes_and_exits_78(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = FakeHandle(1)
    launcher = Launcher(handle)
    adapter, handed = adapter_over(launcher, [FakeDriver(authenticated=LOGIN_PAGE)])
    seen: dict[str, Any] = {}
    _wire_run_worker(monkeypatch, adapter, seen)

    code = worker_main.run_worker(_settings())

    assert code == worker_main.EXIT_NEEDS_LOGIN == 78  # held stopped by systemd
    assert len(handed) == worker_main._AUTH_PROBE_ATTEMPTS  # every probe actually ran
    assert launcher.calls == 1  # all on the one context
    assert seen.get("needs_login") is True
    assert "cleared" not in seen  # never reached the live-session path
    assert seen.get("released") is True


# --- unchanged: a session lost DURING operation still fails fast ---------------------


def test_a_mid_job_session_loss_after_a_good_start_still_marks_the_session_expired() -> None:
    handle = FakeHandle(1)
    adapter, _ = adapter_over(
        Launcher(handle),
        [FakeDriver(authenticated=SEARCH_FORM), FakeDriver(authenticated=LOGIN_PAGE)],
    )
    adapter.ensure_authenticated(interactive=False)  # a good start

    with pytest.raises(WebCargoSessionLost):
        adapter.search(QUERY)  # then WebCargo demands a login mid-job

    assert adapter._manager.state is SessionState.SESSION_EXPIRED  # noqa: SLF001
    with pytest.raises(PermanentFailure, match="No automated login"):
        adapter.search(QUERY)  # every later job refuses without a visit
