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
    WORKER_NEEDS_LOGIN,
    WORKER_OFFLINE,
    WORKER_ONLINE,
    WORKER_UNKNOWN,
    LockHeartbeat,
    WorkerLock,
    acquire_worker_lock,
    build_redis,
    clear_pending_requeue,
    clear_worker_status,
    enqueue_rate_search,
    fetch_job_status,
    publish_worker_needs_login,
    read_pending_requeue,
    record_pending_requeue,
    worker_liveness,
)

__all__ = [
    "RATE_SEARCH_JOB",
    "WORKER_NEEDS_LOGIN",
    "WORKER_OFFLINE",
    "WORKER_ONLINE",
    "WORKER_UNKNOWN",
    "JobState",
    "JobStatus",
    "LockHeartbeat",
    "RateSearchJobRequest",
    "RateSearchJobResult",
    "WorkerLock",
    "acquire_worker_lock",
    "build_redis",
    "clear_pending_requeue",
    "clear_worker_status",
    "enqueue_rate_search",
    "fetch_job_status",
    "job_state_from_rq_status",
    "publish_worker_needs_login",
    "read_pending_requeue",
    "record_pending_requeue",
    "worker_liveness",
]
