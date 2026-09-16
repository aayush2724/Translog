"""The browser worker process: one queue, one worker, one persistent browser.

Startup order is the safety order:

    1. acquire the single-worker lock   (a second worker refuses loudly)
    2. build the provider ONCE          (persistent session, reused per job)
    3. consume the queue serially       (SimpleWorker: in-process, one job
                                         at a time)

`SimpleWorker` is deliberate and load-bearing: RQ's default worker forks a
child per job, and a forked child cannot drive the parent's browser. Running
jobs in this process is what lets one authenticated Chromium session serve
every job. It also makes execution strictly serial — one job must finish
before the next is dequeued — which is the concurrency=1 requirement: shared
session state, deterministic runs, controlled provider load, and failures
that are easy to reason about.
"""

from __future__ import annotations

import os
import signal
import time
from typing import TYPE_CHECKING, Any

from translog_quote import bootstrap
from translog_quote.config import WebCargoMode
from translog_quote.interface.jobs import (
    LockHeartbeat,
    acquire_worker_lock,
    build_redis,
    clear_pending_requeue,
    clear_worker_status,
    publish_worker_needs_login,
    read_pending_requeue,
    record_pending_requeue,
)
from translog_quote.interface.worker import jobs
from translog_quote.observability import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from translog_quote.config import Settings
    from translog_quote.ports import RateSearchPort

_log = get_logger("interface.worker")

#: The exit code the worker uses when it stops because the persistent WebCargo
#: session needs an operator sign-in. It is `EX_CONFIG` from sysexits.h — a
#: configuration/state the process cannot fix itself — and the systemd unit sets
#: `RestartPreventExitStatus=78`, so this exit does NOT restart-loop. Recovery is
#: `--login` then `systemctl --user start …` (see docs/webcargo-operator-auth.md).
EXIT_NEEDS_LOGIN = 78

#: The startup auth probe is retried a few times to absorb a cold-start race:
#: the persistent browser/profile may not have rendered the authenticated form
#: within one navigation timeout, which would otherwise read as a false session
#: loss. Only non-interactive startup retries; an operator `--login` gets one
#: honest attempt.
_AUTH_PROBE_ATTEMPTS = 3
_AUTH_PROBE_BACKOFF_SECONDS = 3.0


def _stop_worker_on_lock_loss(_lost: BaseException) -> None:
    """Warmly stop this process when the heartbeat finds the lease lost.

    Sends SIGINT to ourselves so RQ runs its normal warm shutdown — never a
    hard kill. Invoked from the heartbeat thread; the signal is handled on the
    main thread, which unblocks ``SimpleWorker.work()`` and runs the finally
    cleanup (stop heartbeat, close browser, release — a no-op once the lock is
    another worker's)."""
    os.kill(os.getpid(), signal.SIGINT)


def build_provider(settings: Settings) -> RateSearchPort:
    """The provider this worker serves jobs with.

    The worker is the ONE process allowed to construct the browser provider;
    every other mode goes through the ordinary composition root. Mock and
    demo modes exist here so the whole queue pipeline can be exercised end to
    end before — and independently of — the live WebCargo extraction.
    """
    if settings.webcargo.mode is WebCargoMode.BROWSER:
        return bootstrap.build_browser_rate_provider(settings)
    return bootstrap.build_rate_provider(settings)


def _authenticate_with_retry(
    provider: RateSearchPort,
    *,
    interactive: bool,
    attempts: int = _AUTH_PROBE_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Bring the session to the authenticated form, absorbing a cold-start race.

    Non-interactive startup retries the probe a few times before declaring the
    session lost, because a freshly launched persistent profile can miss the
    first navigation-timeout window and read as a false loss. An operator
    ``--login`` is not retried — they drive it once."""
    from translog_quote.errors import WebCargoSessionLost

    last: WebCargoSessionLost | None = None
    for attempt in range(attempts):
        try:
            bootstrap.ensure_worker_session_authenticated(provider, interactive=interactive)
            return
        except WebCargoSessionLost as exc:
            last = exc
            if interactive or attempt + 1 >= attempts:
                raise
            _log.warning(
                "worker: auth probe %d/%d found no session yet; retrying in %.0fs",
                attempt + 1,
                attempts,
                _AUTH_PROBE_BACKOFF_SECONDS,
            )
            sleep(_AUTH_PROBE_BACKOFF_SECONDS)
    assert last is not None  # noqa: S101 - the loop returns or raises
    raise last


def _requeue_failed_at_front(settings: Settings, job_id: str) -> None:
    """Move a just-failed job off the failed registry and back to the FRONT of
    the queue, so a session-loss failure is retried after the operator re-logs
    in rather than ending permanently FAILED. Best-effort: an unreachable broker
    at shutdown is logged, never raised into the clean exit."""
    try:
        from rq.registry import FailedJobRegistry

        registry = FailedJobRegistry(
            settings.queue.rate_search_queue, connection=build_redis(settings)
        )
        registry.requeue(job_id, at_front=True)
        _log.info("worker: requeued %s at front after mid-job session loss", job_id)
    except Exception as exc:  # noqa: BLE001 - must not break the clean exit
        _log.warning("worker: could not requeue %s after session loss: %s", job_id, exc)


def _drain_pending_requeue(settings: Settings) -> None:
    """Crash-safety backstop, run at startup BEFORE any job is consumed.

    A mid-job session loss records its job id in Redis the instant it happens
    (`record_pending_requeue`), so even a hard death before the normal
    after-work() requeue cannot strand the job permanently FAILED. On the next
    authenticated start we requeue that job at the front if it is still in the
    FailedJobRegistry, then clear the record. An id that was already requeued
    (the normal path ran) or is simply gone is a no-op — we still clear it.
    Best-effort: a broker error leaves the record in place for the next start
    rather than raising into startup, and the record's TTL bounds it regardless.
    """
    job_id = read_pending_requeue(settings)
    if job_id is None:
        return
    try:
        from rq.registry import FailedJobRegistry

        registry = FailedJobRegistry(
            settings.queue.rate_search_queue, connection=build_redis(settings)
        )
        if job_id in registry.get_job_ids():
            registry.requeue(job_id, at_front=True)
            _log.info("worker: requeued stranded %s at front on startup", job_id)
        else:
            _log.info("worker: pending requeue %s already handled; clearing record", job_id)
    except Exception as exc:  # noqa: BLE001 - must not break startup
        _log.warning("worker: could not drain pending requeue %s: %s", job_id, exc)
        return
    clear_pending_requeue(settings)


def run_worker(settings: Settings, *, interactive_login: bool = False) -> int:
    """Run until interrupted. Returns an exit code: ``0`` for a normal stop,
    ``EXIT_NEEDS_LOGIN`` (78) when it stops because the WebCargo session needs an
    operator sign-in — startup loss or mid-job loss alike.

    Session loss no longer crashes the process. At startup the auth probe is
    retried (cold-start race); if it still finds no session, the worker records
    ``needs_login`` in Redis, releases the browser and the lock, and exits 78 so
    systemd (``RestartPreventExitStatus=78``) leaves it stopped — queued jobs,
    which carry no TTL, simply wait for the operator. A session lost MID-JOB
    requeues that job at the front (it must not end permanently FAILED) and exits
    the same way. A live session at startup clears any stale ``needs_login``.
    """
    from rq import Queue, SimpleWorker

    from translog_quote.errors import WebCargoSessionLost

    lock = acquire_worker_lock(settings)
    heartbeat: LockHeartbeat | None = None
    provider: RateSearchPort | None = None
    # Written by the exception handler (a callback) and read here after work().
    session_lost: dict[str, Any] = {"needs_login": False, "requeue": None}

    try:
        # Keep the lease alive for the worker's ENTIRE life — the idle waits
        # between jobs and any interactive operator login, not only per job —
        # so a merely-idle worker never lets its lock lapse. Refresh at ~ttl/2.
        heartbeat = LockHeartbeat(
            lock,
            interval_seconds=max(1.0, settings.queue.worker_lock_ttl_seconds / 2),
            on_lock_lost=_stop_worker_on_lock_loss,
        )
        heartbeat.start()

        provider = build_provider(settings)
        try:
            _authenticate_with_retry(provider, interactive=interactive_login)
        except WebCargoSessionLost as exc:
            # Startup session loss: degrade cleanly instead of crash-looping.
            _log.error("worker: stopping, session needs an operator sign-in: %s", exc)
            publish_worker_needs_login(settings)
            return EXIT_NEEDS_LOGIN

        clear_worker_status(settings)  # a live session clears a prior stop's note
        # Before consuming anything, rescue a job stranded by a previous worker
        # that died mid-job after flagging but before its own requeue ran.
        _drain_pending_requeue(settings)
        jobs.set_provider(provider)

        def _on_job_exception(job: Any, *exc_info: Any) -> bool:
            """Mid-job session loss: flag it and warm-stop. The job is requeued
            AFTER ``work()`` returns — RQ's ``perform_job`` runs this handler and
            then, unconditionally, records the failure (``handle_job_failure``),
            so a requeue here would be overwritten (verified in rq 2.12)."""
            exc_value = exc_info[1] if len(exc_info) > 1 else None
            if isinstance(exc_value, WebCargoSessionLost):
                session_lost["needs_login"] = True
                session_lost["requeue"] = job.id
                # Persist the id NOW, before work() unwinds, so a hard death
                # before the after-work() requeue still leaves a record the next
                # start can rescue (see _drain_pending_requeue).
                record_pending_requeue(settings, job.id)
                _stop_worker_on_lock_loss(exc_value)  # SIGINT self -> warm stop
                return False  # stop other handlers; the failure is still recorded
            return True

        class LockRefreshingWorker(SimpleWorker):
            """Re-checks single-worker ownership right before each job; the
            heartbeat keeps the same lease alive in the idle gaps between."""

            def execute_job(self, job: Any, queue: Any) -> None:
                lock.refresh()
                super().execute_job(job, queue)

        connection = build_redis(settings)
        rq_queue = Queue(settings.queue.rate_search_queue, connection=connection)
        worker = LockRefreshingWorker(
            [rq_queue], connection=connection, exception_handlers=[_on_job_exception]
        )
        worker.work(with_scheduler=False)

        if session_lost["needs_login"]:
            job_id = session_lost["requeue"]
            if job_id is not None:
                _requeue_failed_at_front(settings, job_id)
            publish_worker_needs_login(settings)
            return EXIT_NEEDS_LOGIN
        return 0
    finally:
        if heartbeat is not None:
            heartbeat.stop()
        jobs.set_provider(None)
        close = getattr(provider, "close", None)
        if callable(close):
            close()
        lock.release()
