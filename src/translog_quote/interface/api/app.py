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

    # --- global handling + health, for logging and monitoring ---------------
    import time

    from fastapi import Request
    from fastapi.responses import JSONResponse

    from translog_quote.observability import get_logger

    log = get_logger("interface.api")

    @app.middleware("http")
    async def log_requests(request: Request, call_next):  # type: ignore[no-untyped-def]
        """One structured access line per request. Errors are logged by the
        exception handler below; this times and records the normal responses."""
        started = time.monotonic()
        response = await call_next(request)
        log.info(
            "%s %s -> %d (%.1fms)",
            request.method,
            request.url.path,
            response.status_code,
            (time.monotonic() - started) * 1000,
        )
        return response

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """Any unhandled error becomes a 500 whose body carries no internals.

        The class and message are logged (kept free of payloads and credentials
        by the same discipline the worker uses); the client is told only that
        something failed, never what."""
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    @app.get("/health")
    def health() -> dict[str, str]:
        """Liveness: the process is up and serving. No dependency calls."""
        return {"status": "ok"}

    @app.get("/health/ready")
    def health_ready() -> JSONResponse:
        """Readiness: the job queue (Redis) this API depends on is reachable.

        A probe touches the shared Redis, so keep the polling interval sane. It
        reports rather than raises — an unreachable queue is a 503 the load
        balancer can act on, not a traceback."""
        try:
            from redis import Redis

            Redis.from_url(
                active.queue.redis_url, socket_connect_timeout=5, socket_timeout=5
            ).ping()
        except Exception as exc:  # noqa: BLE001 - readiness reports every failure as 503
            log.warning("readiness: redis unreachable (%s)", type(exc).__name__)
            return JSONResponse(
                status_code=503, content={"status": "not ready", "redis": "unreachable"}
            )
        return JSONResponse(status_code=200, content={"status": "ready", "redis": "ok"})

    return app


def run(settings: Settings, *, host: str, port: int) -> None:
    """Serve the app under uvicorn. The `__main__` entry point's engine."""
    import uvicorn

    uvicorn.run(create_app(settings), host=host, port=port)
