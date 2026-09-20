"""Worker reliability: a resilient broker connection, a liveness indicator, and
a worker that stops cleanly (never crash-loops) when the WebCargo session is
lost — requeuing any in-flight job rather than failing it permanently.

No real Redis, RQ, or browser: fakes and monkeypatches throughout.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from translog_quote.config import WebCargoMode
from translog_quote.interface.jobs import queue
from translog_quote.interface.jobs.queue import (
    WORKER_STATUS_NEEDS_LOGIN,
    _with_retry,
    worker_liveness,
)


def _settings(lock_key: str = "translog:rate-search:browser-worker") -> Any:
    return SimpleNamespace(queue=SimpleNamespace(worker_lock_key=lock_key))


def _session_lost() -> Exception:
    from translog_quote.errors import WebCargoSessionLost

    return WebCargoSessionLost("cold start not ready")


# --- item 2: one bounded retry layer --------------------------------------------


def test_with_retry_absorbs_one_transient_drop() -> None:
    calls = {"n": 0}

    def op() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RedisConnectionError("Connection closed by server")
        return "ok"

    assert _with_retry(op, sleep=lambda _s: None) == "ok"
    assert calls["n"] == 2  # failed once, then succeeded


def test_with_retry_absorbs_a_timeout_too() -> None:
    calls = {"n": 0}

    def op() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RedisTimeoutError("timed out")
        return "ok"

    assert _with_retry(op, sleep=lambda _s: None) == "ok"


def test_with_retry_gives_up_after_attempts() -> None:
    def op() -> str:
        raise RedisConnectionError("down")

    with pytest.raises(RedisConnectionError):
        _with_retry(op, attempts=3, sleep=lambda _s: None)


def test_with_retry_does_not_retry_other_errors() -> None:
    calls = {"n": 0}

    def op() -> str:
        calls["n"] += 1
        raise ValueError("not a connection problem")

    with pytest.raises(ValueError):
        _with_retry(op, sleep=lambda _s: None)
    assert calls["n"] == 1  # not retried


# --- item 1: read-only worker liveness ------------------------------------------


class _FakeConn:
    def __init__(
        self, *, exists: int = 0, status: bytes | None = None, raises: bool = False
    ) -> None:
        self._exists = exists
        self._status = status
        self._raises = raises

    def exists(self, _key: str) -> int:
        if self._raises:
            raise RedisConnectionError("down")
        return self._exists

    def get(self, _key: str) -> bytes | None:
        if self._raises:
            raise RedisConnectionError("down")
        return self._status


def _fake_build_redis(conn: _FakeConn) -> Any:
    return lambda _settings: conn


def test_worker_liveness_online_when_the_lock_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(queue, "build_redis", _fake_build_redis(_FakeConn(exists=1)))
    assert worker_liveness(_settings()) == "online"


def test_worker_liveness_needs_login_from_the_status_key(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _FakeConn(exists=0, status=WORKER_STATUS_NEEDS_LOGIN.encode())
    monkeypatch.setattr(queue, "build_redis", _fake_build_redis(conn))
    assert worker_liveness(_settings()) == "needs_login"


def test_worker_liveness_offline_when_no_lock_and_no_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(queue, "build_redis", _fake_build_redis(_FakeConn(exists=0, status=None)))
    assert worker_liveness(_settings()) == "offline"


def test_worker_liveness_unknown_on_a_redis_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(queue, "build_redis", _fake_build_redis(_FakeConn(raises=True)))
    # Never "offline" and never raises — a broker blip must not mislabel a live
    # worker nor block the page.
    assert worker_liveness(_settings()) == "unknown"


def test_publish_and_clear_worker_status_are_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker's status writes never raise, even if the broker is down."""

    class _Boom:
        def set(self, *_a: object, **_k: object) -> None:
            raise RedisConnectionError("down")

        def delete(self, *_a: object) -> None:
            raise RedisConnectionError("down")

    monkeypatch.setattr(queue, "build_redis", lambda _s: _Boom())
    queue.publish_worker_needs_login(_settings())  # must not raise
    queue.clear_worker_status(_settings())  # must not raise


# --- item 3: startup auth retry, degrade cleanly, requeue mid-job ---------------


def test_startup_auth_probe_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    from translog_quote.interface.worker import main as worker_main

    calls = {"n": 0}

    def flaky(_provider: object, *, interactive: bool) -> None:
        calls["n"] += 1
        if calls["n"] < 3:
            raise _session_lost()

    monkeypatch.setattr(worker_main.bootstrap, "ensure_worker_session_authenticated", flaky)
    worker_main._authenticate_with_retry(object(), interactive=False, sleep=lambda _s: None)  # type: ignore[arg-type]
    assert calls["n"] == 3  # two cold-start misses, then the live session


def test_startup_auth_probe_gives_up_after_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    from translog_quote.errors import WebCargoSessionLost
    from translog_quote.interface.worker import main as worker_main

    def always_lost(_provider: object, *, interactive: bool) -> None:
        raise WebCargoSessionLost("no session")

    monkeypatch.setattr(worker_main.bootstrap, "ensure_worker_session_authenticated", always_lost)
    with pytest.raises(WebCargoSessionLost):
        worker_main._authenticate_with_retry(object(), interactive=False, sleep=lambda _s: None)  # type: ignore[arg-type]


def test_startup_session_loss_degrades_to_exit_78(monkeypatch: pytest.MonkeyPatch) -> None:
    """No crash-loop: on a persistent startup loss the worker records
    needs_login, releases the lock, and returns EXIT_NEEDS_LOGIN — before any
    job is consumed."""
    from translog_quote.errors import WebCargoSessionLost
    from translog_quote.interface.worker import main as worker_main

    released: dict[str, Any] = {"lock": False, "provider_closed": False, "published": False}

    class _Lock:
        def release(self) -> None:
            released["lock"] = True

    class _Heartbeat:
        def __init__(self, *a: object, **k: object) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    class _Provider:
        def close(self) -> None:
            released["provider_closed"] = True

    def _raise(_p: object, *, interactive: bool, **_kw: object) -> None:
        raise WebCargoSessionLost("no session")

    monkeypatch.setattr(worker_main, "acquire_worker_lock", lambda _s: _Lock())
    monkeypatch.setattr(worker_main, "LockHeartbeat", _Heartbeat)
    monkeypatch.setattr(worker_main, "build_provider", lambda _s: _Provider())
    monkeypatch.setattr(worker_main, "_authenticate_with_retry", _raise)
    monkeypatch.setattr(
        worker_main, "publish_worker_needs_login", lambda _s: released.update(published=True)
    )
    monkeypatch.setattr(
        worker_main, "clear_worker_status", lambda _s: released.update(cleared=True)
    )

    code = worker_main.run_worker(
        SimpleNamespace(
            queue=SimpleNamespace(worker_lock_ttl_seconds=120),
            webcargo=SimpleNamespace(
                startup_unreachable_max_wait_seconds=120.0, mode=WebCargoMode.BROWSER
            ),
        )
    )

    assert code == worker_main.EXIT_NEEDS_LOGIN == 78
    assert released["published"] is True
    assert released["lock"] is True  # lock released
    assert released["provider_closed"] is True  # browser released
    assert "cleared" not in released  # never reached the live-session path / worker loop


def test_requeue_helper_puts_the_job_at_the_front(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mid-job session loss must not end permanently FAILED: the job is moved
    off the failed registry to the FRONT of the queue."""
    from translog_quote.interface.worker import main as worker_main

    seen: dict[str, object] = {}

    class _Registry:
        def __init__(self, name: str, connection: object) -> None:
            seen["name"] = name

        def requeue(self, job_id: str, at_front: bool = False) -> None:
            seen["job_id"] = job_id
            seen["at_front"] = at_front

    import rq.registry

    monkeypatch.setattr(rq.registry, "FailedJobRegistry", _Registry)
    monkeypatch.setattr(worker_main, "build_redis", lambda _s: object())

    worker_main._requeue_failed_at_front(
        SimpleNamespace(queue=SimpleNamespace(rate_search_queue="rate-search")), "job-1"
    )

    assert seen["job_id"] == "job-1"
    assert seen["at_front"] is True


# --- item 1 (crash safety): persist the requeue id, drain it on next start ------


class _FakeStore:
    """A one-key in-memory Redis stand-in for the pending-requeue record."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def set(self, key: str, value: Any, ex: int | None = None) -> None:
        self.data[key] = value

    def get(self, key: str) -> Any:
        return self.data.get(key)

    def delete(self, key: str) -> None:
        self.data.pop(key, None)


def test_pending_requeue_record_roundtrips(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeStore()
    monkeypatch.setattr(queue, "build_redis", lambda _s: store)
    settings = _settings()

    queue.record_pending_requeue(settings, "job-1")
    assert queue.read_pending_requeue(settings) == "job-1"

    queue.clear_pending_requeue(settings)
    assert queue.read_pending_requeue(settings) is None


def test_pending_requeue_writes_are_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broker down at the moment of failure must not crash the clean stop, and
    a broker down at startup reads as 'nothing pending' rather than raising."""

    class _Boom:
        def set(self, *_a: object, **_k: object) -> None:
            raise RedisConnectionError("down")

        def get(self, *_a: object, **_k: object) -> None:
            raise RedisConnectionError("down")

        def delete(self, *_a: object) -> None:
            raise RedisConnectionError("down")

    monkeypatch.setattr(queue, "build_redis", lambda _s: _Boom())
    queue.record_pending_requeue(_settings(), "job-1")  # must not raise
    assert queue.read_pending_requeue(_settings()) is None  # swallows -> None
    queue.clear_pending_requeue(_settings())  # must not raise


def _drain_settings() -> Any:
    return SimpleNamespace(queue=SimpleNamespace(rate_search_queue="rate-search"))


def test_drain_requeues_a_stranded_job_on_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """flagged-but-not-requeued (the worker died before after-work() ran): the
    job is still in the FailedJobRegistry, so the next start requeues it at the
    front and clears the record."""
    from translog_quote.interface.worker import main as worker_main

    seen: dict[str, Any] = {}
    cleared = {"n": 0}

    class _Registry:
        def __init__(self, _name: str, connection: object) -> None:
            pass

        def get_job_ids(self) -> list[str]:
            return ["job-1"]  # still failed

        def requeue(self, job_id: str, at_front: bool = False) -> None:
            seen["job_id"] = job_id
            seen["at_front"] = at_front

    import rq.registry

    monkeypatch.setattr(rq.registry, "FailedJobRegistry", _Registry)
    monkeypatch.setattr(worker_main, "build_redis", lambda _s: object())
    monkeypatch.setattr(worker_main, "read_pending_requeue", lambda _s: "job-1")
    monkeypatch.setattr(
        worker_main, "clear_pending_requeue", lambda _s: cleared.__setitem__("n", cleared["n"] + 1)
    )

    worker_main._drain_pending_requeue(_drain_settings())

    assert seen == {"job_id": "job-1", "at_front": True}
    assert cleared["n"] == 1  # record cleared after handling


def test_drain_is_a_noop_when_the_job_is_no_longer_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """already-requeued/absent id: not in the FailedJobRegistry, so no requeue —
    but the stale record is still cleared."""
    from translog_quote.interface.worker import main as worker_main

    seen: dict[str, Any] = {}
    cleared = {"n": 0}

    class _Registry:
        def __init__(self, _name: str, connection: object) -> None:
            pass

        def get_job_ids(self) -> list[str]:
            return []  # already requeued by the normal path, or gone

        def requeue(self, *_a: object, **_k: object) -> None:
            seen["requeued"] = True

    import rq.registry

    monkeypatch.setattr(rq.registry, "FailedJobRegistry", _Registry)
    monkeypatch.setattr(worker_main, "build_redis", lambda _s: object())
    monkeypatch.setattr(worker_main, "read_pending_requeue", lambda _s: "job-1")
    monkeypatch.setattr(
        worker_main, "clear_pending_requeue", lambda _s: cleared.__setitem__("n", cleared["n"] + 1)
    )

    worker_main._drain_pending_requeue(_drain_settings())

    assert "requeued" not in seen  # no-op
    assert cleared["n"] == 1  # record still cleared


def test_drain_does_nothing_without_a_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """No pending record: the registry is never even constructed."""
    from translog_quote.interface.worker import main as worker_main

    cleared = {"n": 0}

    class _Boom:
        def __init__(self, *_a: object, **_k: object) -> None:
            raise AssertionError("must not touch the registry when nothing is pending")

    import rq.registry

    monkeypatch.setattr(rq.registry, "FailedJobRegistry", _Boom)
    monkeypatch.setattr(worker_main, "read_pending_requeue", lambda _s: None)
    monkeypatch.setattr(
        worker_main, "clear_pending_requeue", lambda _s: cleared.__setitem__("n", cleared["n"] + 1)
    )

    worker_main._drain_pending_requeue(_drain_settings())

    assert cleared["n"] == 0  # nothing to clear


# --- round 2, Fix 1: the lock heartbeat survives a lapse ------------------------


class _FakeLockConn:
    """A tiny in-memory stand-in for a Redis client's lock ops. ``get`` returns
    the next scripted value (an Exception is raised); ``set`` (NX) and ``expire``
    are recorded."""

    def __init__(self, get_returns: list[Any], *, set_returns: bool = True) -> None:
        self._get = list(get_returns)
        self._set_returns = set_returns
        self.calls: list[tuple[Any, ...]] = []

    def get(self, _key: str) -> Any:
        value = self._get.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def set(self, _key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        self.calls.append(("set", value, nx, ex))
        return self._set_returns

    def expire(self, _key: str, ttl: int) -> bool:
        self.calls.append(("expire", ttl))
        return True


def _lock(conn: _FakeLockConn) -> Any:
    lock = queue.WorkerLock(object(), key="k", ttl_seconds=120, sleep=lambda _s: None)
    lock._connection = conn  # type: ignore[attr-defined]
    return lock


def test_one_failed_refresh_is_retried_not_fatal() -> None:
    lock = queue.WorkerLock(object(), key="k", ttl_seconds=120, sleep=lambda _s: None)
    token = lock._token.encode("utf-8")  # type: ignore[attr-defined]
    lock._connection = _FakeLockConn([RedisConnectionError("blip"), token])  # type: ignore[attr-defined]

    lock.refresh()  # the in-tick retry absorbs the blip; no raise

    assert ("expire", 120) in lock._connection.calls  # type: ignore[attr-defined]


def test_absent_key_is_reacquired_with_the_same_token() -> None:
    lock = queue.WorkerLock(object(), key="k", ttl_seconds=120, sleep=lambda _s: None)
    token = lock._token  # type: ignore[attr-defined]
    conn = _FakeLockConn([None], set_returns=True)  # key gone -> SET NX succeeds
    lock._connection = conn  # type: ignore[attr-defined]

    lock.refresh()  # re-acquires; must NOT raise

    assert ("set", token, True, 120) in conn.calls  # re-acquired with our token, NX, full TTL


def test_a_foreign_token_stops_the_worker() -> None:
    from translog_quote.errors import PermanentFailure

    lock = _lock(_FakeLockConn([b"another-workers-token"]))
    with pytest.raises(PermanentFailure):
        lock.refresh()


def test_losing_the_reacquire_race_stops_the_worker() -> None:
    from translog_quote.errors import PermanentFailure

    # key absent, but SET NX fails (someone took it), and the re-read is foreign.
    lock = _lock(_FakeLockConn([None, b"another-workers-token"], set_returns=False))
    with pytest.raises(PermanentFailure):
        lock.refresh()


# --- round 2, Fix 2: dequeue window + capped backoff ----------------------------


@pytest.mark.filterwarnings("ignore:CLIENT LIST command not supported")
def test_dequeue_timeout_is_well_under_the_idle_reset() -> None:
    from unittest.mock import MagicMock

    from rq import Queue

    from translog_quote.interface.worker.main import _WORKER_TTL_SECONDS, _LockRefreshingWorker

    conn = MagicMock()
    conn.connection_pool.connection_kwargs = {}  # RQ reads socket_timeout from here
    worker = _LockRefreshingWorker(
        [Queue("rate-search", connection=conn)], connection=conn, worker_ttl=_WORKER_TTL_SECONDS
    )
    assert worker.dequeue_timeout == 120  # worker_ttl - 15 (rq 2.12)
    assert worker.dequeue_timeout < 250  # re-issues the BLPOP before Upstash's ~270s reset


def test_reconnect_backoff_is_capped_below_the_rq_default() -> None:
    from rq.worker import Worker

    from translog_quote.interface.worker.main import _LockRefreshingWorker

    assert _LockRefreshingWorker.max_connection_wait_time == 5.0
    assert _LockRefreshingWorker.max_connection_wait_time < Worker.max_connection_wait_time  # 60.0


def test_in_tick_retries_finish_well_within_the_refresh_interval() -> None:
    """A refresh that hits two transient blips retries in-tick, and the total
    sleep is far under the ttl/6 heartbeat interval (20s for a 120s TTL) — the
    retries never eat into the next tick."""
    slept: list[float] = []
    lock = queue.WorkerLock(object(), key="k", ttl_seconds=120, sleep=slept.append)
    token = lock._token.encode("utf-8")  # type: ignore[attr-defined]
    lock._connection = _FakeLockConn(  # type: ignore[attr-defined]
        [RedisConnectionError("x"), RedisConnectionError("y"), token]
    )

    lock.refresh()  # two blips absorbed, then our token -> expire

    interval = 120 / 6  # the heartbeat interval this TTL implies
    assert sum(slept) < interval  # comfortably inside one tick
    assert sum(slept) < 1.5  # and actually sub-second (0.2 + 0.4 backoff)


@pytest.mark.filterwarnings("ignore:CLIENT LIST command not supported")
def test_a_long_job_keeps_its_registry_entry_alive_for_the_whole_timeout() -> None:
    """The in-process (SimpleWorker) guard: with job_monitoring_interval set to
    the job timeout, the heartbeat TTL taken once at job start covers the WHOLE
    job, so a slow search is never abandoned mid-run. The RQ default (30) would
    give only 90s."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from rq import Queue

    from translog_quote.interface.worker.main import _WORKER_TTL_SECONDS, _LockRefreshingWorker

    conn = MagicMock()
    conn.connection_pool.connection_kwargs = {}
    worker = _LockRefreshingWorker(
        [Queue("rate-search", connection=conn)],
        connection=conn,
        worker_ttl=_WORKER_TTL_SECONDS,
        job_monitoring_interval=600,  # = job_timeout
    )
    worker.current_job_working_time = 0.0

    ttl = worker.get_heartbeat_ttl(SimpleNamespace(timeout=600))

    assert ttl >= 600  # the start heartbeat covers the whole 600s job timeout
    assert int(min(600, 30)) + 60 == 90  # what the RQ default interval would have given


# --- round 2, Fix 3: unreachable vs login page ----------------------------------


def test_startup_unreachable_exits_75_and_never_writes_needs_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from translog_quote.errors import WebCargoUnreachable
    from translog_quote.interface.worker import main as worker_main

    flags: dict[str, Any] = {}

    class _Lock:
        def release(self) -> None:
            flags["released"] = True

    class _Heartbeat:
        def __init__(self, *a: object, **k: object) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    class _Provider:
        def close(self) -> None:
            pass

    def _unreachable(_p: object, *, interactive: bool, **_kw: object) -> None:
        raise WebCargoUnreachable("name resolution failed")

    monkeypatch.setattr(worker_main, "acquire_worker_lock", lambda _s: _Lock())
    monkeypatch.setattr(worker_main, "LockHeartbeat", _Heartbeat)
    monkeypatch.setattr(worker_main, "build_provider", lambda _s: _Provider())
    monkeypatch.setattr(worker_main, "_authenticate_with_retry", _unreachable)
    monkeypatch.setattr(
        worker_main, "publish_worker_needs_login", lambda _s: flags.__setitem__("needs_login", True)
    )
    monkeypatch.setattr(worker_main, "clear_worker_status", lambda _s: None)

    code = worker_main.run_worker(
        SimpleNamespace(
            queue=SimpleNamespace(worker_lock_ttl_seconds=120),
            webcargo=SimpleNamespace(
                startup_unreachable_max_wait_seconds=120.0, mode=WebCargoMode.BROWSER
            ),
        )
    )

    assert code == worker_main.EXIT_UNREACHABLE == 75
    assert "needs_login" not in flags  # a network problem must NOT look like needs-login
    assert flags.get("released") is True  # the lock is still released


def test_authenticate_with_retry_backs_off_then_raises_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from translog_quote.errors import WebCargoUnreachable
    from translog_quote.interface.worker import main as worker_main

    def always_unreachable(_p: object, *, interactive: bool) -> None:
        raise WebCargoUnreachable("net down")

    monkeypatch.setattr(
        worker_main.bootstrap, "ensure_worker_session_authenticated", always_unreachable
    )
    clock = [0.0, 0.0, 999.0]  # deadline calc, first check (retry), second check (past deadline)

    def _monotonic() -> float:
        return clock.pop(0) if clock else 999.0

    with pytest.raises(WebCargoUnreachable):
        worker_main._authenticate_with_retry(
            object(),  # type: ignore[arg-type]
            interactive=False,
            sleep=lambda _s: None,
            monotonic=_monotonic,
        )


def test_login_page_visible_reads_the_password_field_not_the_placeholder() -> None:
    from translog_quote.adapters.webcargo.browser import pages

    class _Driver:
        def __init__(self, state: dict[str, bool]) -> None:
            self._state = state

        def evaluate(self, _script: str, _argument: object = None) -> dict[str, bool]:
            return self._state

    assert pages.login_page_visible(_Driver({"hasPassword": True, "hasSearchForm": False})) is True
    assert (
        pages.login_page_visible(_Driver({"hasPassword": False, "hasSearchForm": False})) is False
    )


def test_is_authenticated_propagates_unreachable_from_a_failed_goto() -> None:
    from translog_quote.adapters.webcargo.browser import pages
    from translog_quote.errors import WebCargoUnreachable

    class _Driver:
        def goto(self, _url: str) -> None:
            raise WebCargoUnreachable("dns")

        def wait_visible(self, _sel: str, timeout_seconds: float) -> bool:
            return True

    with pytest.raises(WebCargoUnreachable):
        pages.is_authenticated(_Driver(), base_url="https://x.invalid/app/", timeout_seconds=1)


# --- simulated-worker guard (recommendation #1) ------------------------------


def _webcargo_settings(mode: WebCargoMode) -> Any:
    """Minimal settings the guard reads: just the WebCargo mode."""
    return SimpleNamespace(webcargo=SimpleNamespace(mode=mode))


def test_browser_worker_is_never_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Browser mode is completely unaffected — the guard passes it straight through."""
    from translog_quote.interface.worker import main as worker_main

    monkeypatch.delenv(worker_main.ALLOW_SIMULATED_WORKER_VAR, raising=False)

    assert worker_main._refuse_simulated_worker(_webcargo_settings(WebCargoMode.BROWSER)) is None


@pytest.mark.parametrize("mode", [WebCargoMode.MOCK, WebCargoMode.DEMO])
def test_simulated_worker_refuses_without_opt_in(
    mode: WebCargoMode, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mock/demo worker refuses to start by default (no silent fake rates)."""
    from translog_quote.interface.worker import main as worker_main

    monkeypatch.delenv(worker_main.ALLOW_SIMULATED_WORKER_VAR, raising=False)

    assert (
        worker_main._refuse_simulated_worker(_webcargo_settings(mode))
        == worker_main.EXIT_SIMULATED_REFUSED
    )


@pytest.mark.parametrize("mode", [WebCargoMode.MOCK, WebCargoMode.DEMO])
def test_simulated_worker_starts_with_explicit_opt_in(
    mode: WebCargoMode, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With TRANSLOG_ALLOW_SIMULATED_WORKER=1 the guard allows a simulated worker."""
    from translog_quote.interface.worker import main as worker_main

    monkeypatch.setenv(worker_main.ALLOW_SIMULATED_WORKER_VAR, "1")

    assert worker_main._refuse_simulated_worker(_webcargo_settings(mode)) is None


def test_run_worker_refuses_simulated_mode_before_touching_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard runs first: a mock worker without opt-in returns the refusal code
    without acquiring the lock or building a provider."""
    from translog_quote.interface.worker import main as worker_main

    monkeypatch.delenv(worker_main.ALLOW_SIMULATED_WORKER_VAR, raising=False)

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("must not run: the guard should refuse first")

    monkeypatch.setattr(worker_main, "acquire_worker_lock", _boom)
    monkeypatch.setattr(worker_main, "build_provider", _boom)

    code = worker_main.run_worker(_webcargo_settings(WebCargoMode.MOCK))

    assert code == worker_main.EXIT_SIMULATED_REFUSED
