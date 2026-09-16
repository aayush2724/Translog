"""Enqueueing, job inspection, and the one-browser-worker lock.

The only module that talks to Redis/RQ. The queue libraries are imported
lazily, inside the functions that need them, so a core install without the
`api`/`worker` extras still imports this package — the same pattern the Gmail
consent command established for its optional dependency.

The worker-side job function is named by dotted path, not imported: the API
process enqueues work without ever loading the worker, the browser adapter, or
Playwright.
"""

from __future__ import annotations

import contextlib
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any, TypeVar

from translog_quote.errors import PermanentFailure
from translog_quote.interface.jobs.model import (
    JobState,
    JobStatus,
    RateSearchJobRequest,
    RateSearchJobResult,
    job_state_from_rq_status,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from translog_quote.config import Settings

_T = TypeVar("_T")

#: The function RQ executes, as a dotted path (see module docstring).
RATE_SEARCH_JOB = "translog_quote.interface.worker.jobs.run_rate_search"

#: Redis client tuning for a hosted broker (Upstash) that drops idle
#: connections. Short timeouts bound a stall — we measured a 26s readiness hang
#: — so a dead socket fails fast into a retry rather than blocking a request or
#: the worker. A 30s health check PINGs a connection idle longer than that
#: before reusing it, so a silently-dropped connection is detected and replaced
#: instead of handed to a command. TCP keepalive lets the OS notice a dead peer
#: behind NAT and idle timeouts.
_SOCKET_TIMEOUT_SECONDS = 5
_HEALTH_CHECK_INTERVAL_SECONDS = 30

#: A single, bounded retry layer for transient drops (see `_with_retry`). Small
#: on purpose: one "Connection closed by server" is absorbed; a real outage
#: still surfaces quickly rather than hanging the page or the worker. Redis-py's
#: own `retry` is deliberately NOT configured, so there is exactly one retry
#: layer to reason about and to test.
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 0.2


def build_redis(settings: Settings) -> Any:
    """One resilient Redis client, shared by this module and the browser worker.

    Carries connection-health settings only; retry policy lives in the single
    `_with_retry` wrapper, never on the client.
    """
    import socket

    from redis import Redis

    keepalive_options: dict[int, int] = {}
    for name, value in (("TCP_KEEPIDLE", 60), ("TCP_KEEPINTVL", 30), ("TCP_KEEPCNT", 3)):
        option = getattr(socket, name, None)
        if option is not None:
            keepalive_options[option] = value

    return Redis.from_url(
        settings.queue.redis_url,
        socket_connect_timeout=_SOCKET_TIMEOUT_SECONDS,
        socket_timeout=_SOCKET_TIMEOUT_SECONDS,
        health_check_interval=_HEALTH_CHECK_INTERVAL_SECONDS,
        socket_keepalive=True,
        socket_keepalive_options=keepalive_options or None,
    )


def _with_retry(
    operation: Callable[[], _T],
    *,
    attempts: int = _RETRY_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> _T:
    """Run a Redis operation, retrying a transient connection drop a few times.

    Only `ConnectionError`/`TimeoutError` are retried — the transient Upstash
    drop — with a short backoff; any other error propagates at once. Retrying an
    enqueue is safe because the RQ job id is the request's deterministic
    idempotency key: a retry after a lost response joins the existing job rather
    than creating a duplicate.
    """
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import TimeoutError as RedisTimeoutError

    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            return operation()
        except (RedisConnectionError, RedisTimeoutError) as exc:
            last = exc
            if attempt + 1 < attempts:
                sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
    assert last is not None  # noqa: S101 - loop runs at least once
    raise last


def _connection(settings: Settings) -> Any:
    return build_redis(settings)


def _fetch_rq_job(job_id: str, connection: Any) -> Any | None:
    from rq.exceptions import NoSuchJobError
    from rq.job import Job

    try:
        return Job.fetch(job_id, connection=connection)
    except NoSuchJobError:
        return None


def enqueue_rate_search(request: RateSearchJobRequest, settings: Settings) -> tuple[str, bool]:
    """Idempotently enqueue one rate search. Returns ``(job_id, created)``.

    The job id is the request's own idempotency key, so a byte-identical
    request inside the result TTL joins the existing job (``created=False``)
    rather than queueing duplicate browser work. A previously *failed*
    identical request is re-enqueued: the caller asked again, and the failed
    run is already preserved under its own TTL.
    """
    from rq import Queue

    job_id = request.idempotency_key()

    def _enqueue() -> tuple[str, bool]:
        connection = build_redis(settings)
        existing = _fetch_rq_job(job_id, connection)
        if existing is not None:
            if job_state_from_rq_status(existing.get_status()) is not JobState.FAILED:
                return job_id, False
            existing.delete()

        queue = Queue(settings.queue.rate_search_queue, connection=connection)
        queue.enqueue(
            RATE_SEARCH_JOB,
            request.model_dump(mode="json"),
            job_id=job_id,
            job_timeout=settings.queue.job_timeout_seconds,
            result_ttl=settings.queue.result_ttl_seconds,
            failure_ttl=settings.queue.failure_ttl_seconds,
        )
        return job_id, True

    # Safe to retry: the deterministic job id makes a re-run join the existing
    # job rather than duplicate it (see `_with_retry`).
    return _with_retry(_enqueue)


def fetch_job_status(job_id: str, settings: Settings) -> JobStatus | None:
    """The state a client polls for, or ``None`` for a job nobody created.

    A failed job reports the failure's summary line — the exception class and
    its message, which the worker keeps free of payloads and credentials —
    never a traceback.
    """
    def _fetch() -> JobStatus | None:
        job = _fetch_rq_job(job_id, build_redis(settings))
        if job is None:
            return None

        state = job_state_from_rq_status(job.get_status())

        result: RateSearchJobResult | None = None
        if state is JobState.COMPLETED:
            result = RateSearchJobResult.model_validate(job.return_value())

        error: str | None = None
        if state is JobState.FAILED:
            error = _failure_summary(job.exc_info)

        return JobStatus(job_id=job_id, state=state, result=result, error=error)

    return _with_retry(_fetch)


def _failure_summary(exc_info: str | None) -> str:
    """The last line of a stored failure: ``SomeError: reason``, nothing more."""
    if not exc_info:
        return "job failed with no recorded reason"
    lines = [line.strip() for line in exc_info.strip().splitlines() if line.strip()]
    return lines[-1] if lines else "job failed with no recorded reason"


# --- worker liveness (read-only for the dashboard; written by the worker) -----

#: The worker publishes this when it exits because the persistent WebCargo
#: session needs an operator sign-in, so the dashboard can tell "offline" (no
#: worker at all) from "needs login" (a worker stopped, awaiting `--login`).
WORKER_STATUS_KEY = "translog:rate-search:worker-status"  # noqa: S105 - key name, not a secret
WORKER_STATUS_NEEDS_LOGIN = "needs_login"

#: How long the "needs login" note lives if nobody acts. It outlasts a normal
#: operator response window but is not permanent — once it lapses the dashboard
#: shows plain "offline" rather than a stale "needs login".
_WORKER_STATUS_TTL_SECONDS = 24 * 3600

#: The liveness one pending request reports. Plain strings, serialised as-is.
WORKER_ONLINE = "online"
WORKER_OFFLINE = "offline"
WORKER_NEEDS_LOGIN = "needs_login"
WORKER_UNKNOWN = "unknown"


def _as_text(value: Any) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def worker_liveness(settings: Settings) -> str:
    """Read-only worker status: online / needs_login / offline / unknown.

    Online when the single-worker lock key exists (the running worker refreshes
    it); else `needs_login` when the worker published that before exiting; else
    `offline`. Bounded (short-timeout client) and total: ANY Redis error yields
    `unknown` — never `offline` — so a broker blip can neither mislabel a live
    worker nor raise into the caller. No acquire, no refresh, no write.
    """
    try:
        connection = build_redis(settings)
        if connection.exists(settings.queue.worker_lock_key):
            return WORKER_ONLINE
        status = connection.get(WORKER_STATUS_KEY)
        if status is not None and _as_text(status) == WORKER_STATUS_NEEDS_LOGIN:
            return WORKER_NEEDS_LOGIN
        return WORKER_OFFLINE
    except Exception:  # noqa: BLE001 - liveness must never raise into the page
        return WORKER_UNKNOWN


def publish_worker_needs_login(settings: Settings) -> None:
    """The worker records that it stopped, needing an operator sign-in.

    Best-effort: an unreachable broker at shutdown must not turn a clean
    `needs_login` exit into a crash, so any error here is swallowed."""
    with contextlib.suppress(Exception):
        build_redis(settings).set(
            WORKER_STATUS_KEY, WORKER_STATUS_NEEDS_LOGIN, ex=_WORKER_STATUS_TTL_SECONDS
        )


def clear_worker_status(settings: Settings) -> None:
    """The worker clears any stale `needs_login` once it has a live session."""
    with contextlib.suppress(Exception):
        build_redis(settings).delete(WORKER_STATUS_KEY)


# --- crash-safe mid-job requeue (survives a hard worker death) -----------------

#: When the worker loses the WebCargo session MID-JOB it records that job id here
#: the instant it happens, alongside the `needs_login` note. The after-work()
#: requeue is the normal path, but if the process dies before it runs, the job
#: would be stranded in the FailedJobRegistry. The next authenticated start reads
#: this key and requeues the job before consuming anything (see the worker's
#: `_drain_pending_requeue`). TTL matches the status note so it outlives the
#: operator response window but never lingers forever.
WORKER_REQUEUE_KEY = "translog:rate-search:requeue-pending"  # noqa: S105 - key name, not a secret


def record_pending_requeue(settings: Settings, job_id: str) -> None:
    """Persist the id of a job to requeue after a mid-job session loss.

    Best-effort, like `publish_worker_needs_login`: an unreachable broker at the
    moment of failure must not turn the clean stop into a crash. Losing this
    record only forfeits the crash-safety backstop, not the normal after-work()
    requeue."""
    with contextlib.suppress(Exception):
        build_redis(settings).set(WORKER_REQUEUE_KEY, job_id, ex=_WORKER_STATUS_TTL_SECONDS)


def read_pending_requeue(settings: Settings) -> str | None:
    """The recorded mid-job requeue id, or ``None`` if there is none.

    Best-effort: a broker error reads as "nothing pending" so startup is never
    blocked; the record survives for the next start to drain."""
    try:
        value = build_redis(settings).get(WORKER_REQUEUE_KEY)
    except Exception:  # noqa: BLE001 - must not block startup
        return None
    return _as_text(value) if value is not None else None


def clear_pending_requeue(settings: Settings) -> None:
    """Drop the pending-requeue record once it has been handled."""
    with contextlib.suppress(Exception):
        build_redis(settings).delete(WORKER_REQUEUE_KEY)


class WorkerLock:
    """The one-browser-worker guarantee, held as a Redis lease.

    The intended deployment is **exactly one** browser worker per rate-search
    queue: the persistent WebCargo session is shared state, and a second
    worker would mean concurrent automation of one provider session. This is
    an architectural correctness rule, so it is enforced at startup rather
    than written in a runbook — a second worker refuses loudly instead of
    silently doubling the browser.

    The lease expires on its own (`ttl_seconds`) so a crashed worker never
    wedges its successor; the live worker refreshes it as it runs.
    """

    def __init__(self, connection: Any, *, key: str, ttl_seconds: int) -> None:
        self._connection = connection
        self._key = key
        self._ttl = ttl_seconds
        self._token = uuid.uuid4().hex

    def acquire(self) -> None:
        taken = self._connection.set(self._key, self._token, nx=True, ex=self._ttl)
        if not taken:
            raise PermanentFailure(
                "another browser worker already holds the rate-search worker lock. "
                "The intended deployment is exactly one browser worker per queue; "
                "stop the other worker (or let its lock expire) before starting this one."
            )

    def refresh(self) -> None:
        """Extend the lease — but only while it is still ours.

        Finding someone else's token means this worker lost the lock (its TTL
        lapsed during a stall and another worker started). Continuing would be
        precisely the concurrent-automation scenario the lock exists to
        prevent, so the only safe answer is to stop.
        """
        holder = self._connection.get(self._key)
        if holder is None or holder != self._token.encode("utf-8"):
            raise PermanentFailure(
                "the rate-search worker lock is no longer held by this worker; "
                "refusing to continue alongside another browser worker."
            )
        self._connection.expire(self._key, self._ttl)

    def release(self) -> None:
        holder = self._connection.get(self._key)
        if holder is not None and holder == self._token.encode("utf-8"):
            self._connection.delete(self._key)


class LockHeartbeat:
    """Keeps an acquired `WorkerLock`'s lease alive for the worker's whole life.

    A daemon thread refreshes the lock every ~ttl/2 seconds, so a worker that
    is merely idle — `SimpleWorker` blocked waiting for the next job — does not
    let its lease lapse and then lose the lock on the following job. The moment
    the process dies the daemon thread dies with it: nothing refreshes the
    lease, it expires on its own TTL, and a successor may acquire it. That is
    exactly the crash-recovery the lock relies on, unchanged.

    Ownership is never weakened. The thread calls the very same
    `WorkerLock.refresh`, which still refuses (`PermanentFailure`) the instant
    the lease is no longer this worker's. That refusal is surfaced through
    `on_lock_lost` so the worker can stop warmly, and then the thread exits —
    it never spins on a lost lock and never lingers past `stop()`.

    A *transient* refresh error (a Redis blip) is tolerated: the tick is
    skipped and the next one retried. If the outage outlives the lease, the
    first successful read afterwards sees a lost lock and takes the stop path.
    """

    def __init__(
        self,
        lock: WorkerLock,
        *,
        interval_seconds: float,
        on_lock_lost: Callable[[BaseException], None],
    ) -> None:
        self._lock = lock
        self._interval = interval_seconds
        self._on_lock_lost = on_lock_lost
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="worker-lock-heartbeat", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def stop(self) -> None:
        """Stop beating and join the thread, so none outlives worker shutdown."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=self._interval + 5.0)

    def _run(self) -> None:
        # wait() returns True the moment stop() is called (exit cleanly), or
        # False on timeout (a tick: time to refresh). The first refresh is one
        # interval in, which is safe — the lease was just acquired at full TTL.
        while not self._stop.wait(self._interval):
            try:
                self._lock.refresh()
            except PermanentFailure as lost:
                # Ownership is gone. Existing semantics: stop. Surface it, then
                # end the thread — never keep beating on a lock that isn't ours.
                self._notify_lost(lost)
                return
            except Exception:  # noqa: BLE001 - a transient refresh error must not
                # kill the worker; skip this tick and retry on the next one.
                continue

    def _notify_lost(self, lost: BaseException) -> None:
        # A faulty callback must not leak the thread: surface, but never raise.
        with contextlib.suppress(Exception):
            self._on_lock_lost(lost)


def acquire_worker_lock(settings: Settings) -> WorkerLock:
    """Build and immediately acquire the single-worker lock."""
    lock = WorkerLock(
        _connection(settings),
        key=settings.queue.worker_lock_key,
        ttl_seconds=settings.queue.worker_lock_ttl_seconds,
    )
    lock.acquire()
    return lock
