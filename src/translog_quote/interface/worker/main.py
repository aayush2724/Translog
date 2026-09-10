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

from typing import TYPE_CHECKING, Any

from translog_quote import bootstrap
from translog_quote.config import WebCargoMode
from translog_quote.interface.jobs import acquire_worker_lock
from translog_quote.interface.worker import jobs

if TYPE_CHECKING:
    from translog_quote.config import Settings
    from translog_quote.ports import RateSearchPort


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


def run_worker(settings: Settings) -> None:
    """Run until interrupted. Lock, provider, then serial consumption."""
    from redis import Redis
    from rq import Queue, SimpleWorker

    lock = acquire_worker_lock(settings)
    provider: RateSearchPort | None = None

    try:
        provider = build_provider(settings)
        jobs.set_provider(provider)

        class LockRefreshingWorker(SimpleWorker):
            """Refreshes the single-worker lease as it works, so a live
            worker keeps its lock and a dead one loses it by silence."""

            def execute_job(self, job: Any, queue: Any) -> None:
                lock.refresh()
                super().execute_job(job, queue)

        connection = Redis.from_url(settings.queue.redis_url)
        rq_queue = Queue(settings.queue.rate_search_queue, connection=connection)
        worker = LockRefreshingWorker([rq_queue], connection=connection)
        worker.work(with_scheduler=False)
    finally:
        jobs.set_provider(None)
        close = getattr(provider, "close", None)
        if callable(close):
            close()
        lock.release()
