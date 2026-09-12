"""interface.jobs — the asynchronous rate-search job vocabulary and queue.

Shared by the API service (which enqueues and answers polls) and the browser
worker (which consumes). The model half is pure shape; the queue half is the
only code that talks to Redis/RQ, and imports it lazily.
"""

from translog_quote.interface.jobs.model import (
    JobState,
    JobStatus,
    RateSearchJobRequest,
    RateSearchJobResult,
    job_state_from_rq_status,
)
from translog_quote.interface.jobs.queue import (
    RATE_SEARCH_JOB,
    LockHeartbeat,
    WorkerLock,
    acquire_worker_lock,
    enqueue_rate_search,
    fetch_job_status,
)

__all__ = [
    "RATE_SEARCH_JOB",
    "JobState",
    "JobStatus",
    "LockHeartbeat",
    "RateSearchJobRequest",
    "RateSearchJobResult",
    "WorkerLock",
    "acquire_worker_lock",
    "enqueue_rate_search",
    "fetch_job_status",
    "job_state_from_rq_status",
]
