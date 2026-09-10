"""The asynchronous rate-search API service.

    POST /api/rate-search            validate -> idempotent enqueue -> 202
    GET  /api/rate-search/{job_id}   -> QUEUED / PROCESSING / COMPLETED / FAILED

This process holds no browser and performs no search: it validates, assigns
the deterministic job identity, enqueues, and answers polls. The browser
worker on the other side of Redis does the work. Keeping the two processes
separate is the deployment boundary the architecture asks for — the API stays
lightweight while the worker carries Chromium.

FastAPI is imported lazily inside `create_app`, so a core install without the
`api` extra still imports this module (and the import-sweep test stays green
on a clean checkout).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from translog_quote.config import load_settings
from translog_quote.interface.jobs import (
    JobStatus,
    RateSearchJobRequest,
    enqueue_rate_search,
    fetch_job_status,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from translog_quote.config import Settings


class JobAccepted(BaseModel):
    """The 202 body: where the job lives, and whether this POST created it.

    ``created=False`` means an identical request was already in flight or
    recently completed — the idempotency contract working, not an error.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    created: bool
    status_url: str


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the API application. Settings injectable for tests."""
    try:
        from fastapi import FastAPI, HTTPException
    except ImportError as exc:
        from translog_quote.errors import PermanentFailure

        raise PermanentFailure(
            "FastAPI is not installed. The API service requires the 'api' "
            "extra: pip install '.[api]'"
        ) from exc

    from redis.exceptions import RedisError

    active = settings if settings is not None else load_settings()

    app = FastAPI(
        title="Translog rate-search API",
        description="Asynchronous WebCargo rate search: submit, then poll.",
    )

    @app.post("/api/rate-search", status_code=202, response_model=JobAccepted)
    def submit_rate_search(request: RateSearchJobRequest) -> JobAccepted:
        try:
            job_id, created = enqueue_rate_search(request, active)
        except RedisError as exc:
            # The queue being down is the API's dependency failing, not the
            # client's request failing — and the message names no host.
            raise HTTPException(
                status_code=503, detail="the job queue is unavailable; retry shortly"
            ) from exc

        return JobAccepted(
            job_id=job_id, created=created, status_url=f"/api/rate-search/{job_id}"
        )

    @app.get("/api/rate-search/{job_id}", response_model=JobStatus)
    def read_rate_search(job_id: str) -> JobStatus:
        try:
            status = fetch_job_status(job_id, active)
        except RedisError as exc:
            raise HTTPException(
                status_code=503, detail="the job queue is unavailable; retry shortly"
            ) from exc

        if status is None:
            raise HTTPException(status_code=404, detail="no such rate-search job")
        return status

    return app


def run(settings: Settings, *, host: str, port: int) -> None:
    """Serve the app under uvicorn. The `__main__` entry point's engine."""
    import uvicorn

    uvicorn.run(create_app(settings), host=host, port=port)
