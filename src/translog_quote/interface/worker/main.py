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
from typing import TYPE_CHECKING, Any

from translog_quote import bootstrap
from translog_quote.config import WebCargoMode
from translog_quote.interface.jobs import LockHeartbeat, acquire_worker_lock
from translog_quote.interface.worker import jobs

if TYPE_CHECKING:
    from translog_quote.config import Settings
    from translog_quote.ports import RateSearchPort


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


def run_worker(settings: Settings, *, interactive_login: bool = False) -> None:
    """Run until interrupted. Lock, provider, authenticate, then serial consumption.

    The provider's persistent browser context is authenticated once, before
    the queue loop, and that same live context serves every job — fresh page
    per job, session cookie held in memory. ``interactive_login`` lets an
    operator sign in at startup; without it an unauthenticated session is
    refused loudly rather than logged into automatically.
    """
    from redis import Redis
    from rq import Queue, SimpleWorker

    lock = acquire_worker_lock(settings)
    heartbeat: LockHeartbeat | None = None
    provider: RateSearchPort | None = None

    try:
        # Keep the lease alive for the worker's ENTIRE life — the idle waits
        # between jobs and any interactive operator login, not only per job —
        # so a merely-idle worker never lets its lock lapse. Refresh at ~ttl/2;
        # the TTL itself is unchanged.
        heartbeat = LockHeartbeat(
            lock,
            interval_seconds=max(1.0, settings.queue.worker_lock_ttl_seconds / 2),
            on_lock_lost=_stop_worker_on_lock_loss,
        )
        heartbeat.start()

        provider = build_provider(settings)
        bootstrap.ensure_worker_session_authenticated(provider, interactive=interactive_login)
        jobs.set_provider(provider)

        class LockRefreshingWorker(SimpleWorker):
            """Re-checks single-worker ownership right before each job; the
            heartbeat keeps the same lease alive in the idle gaps between."""

            def execute_job(self, job: Any, queue: Any) -> None:
                lock.refresh()
                super().execute_job(job, queue)

        connection = Redis.from_url(settings.queue.redis_url)
        rq_queue = Queue(settings.queue.rate_search_queue, connection=connection)
        worker = LockRefreshingWorker([rq_queue], connection=connection)
        worker.work(with_scheduler=False)
    finally:
        if heartbeat is not None:
            heartbeat.stop()
        jobs.set_provider(None)
        close = getattr(provider, "close", None)
        if callable(close):
            close()
        lock.release()
