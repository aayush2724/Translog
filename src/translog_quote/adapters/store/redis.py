"""RedisStore — a durable StorePort backed by Redis, for restart-safe production.

On Render's free tier there is no persistent disk, so the filesystem store is
wiped on every restart/redeploy/spin-down. This store puts the same state in the
Upstash Redis already used for the rate-search queue, so operations mode survives
a restart without a disk.

It mirrors :class:`JsonFileStore` in shape and discipline:

- **Loaded once at construction** (one ``HGETALL`` per collection) into an
  in-memory cache, and every read is served from that cache. The hot paths —
  ``all_threads()`` on every poll (the watermark calculation) and the router's
  ``already_processed`` check — therefore make **no per-poll Redis round-trip**,
  exactly as they made no per-poll disk read before.
- **Write-through** on every save: one hash-field update per request/thread.
- **Single-writer**, like ``JsonFileStore``: the web service is one instance, so
  the in-memory cache is authoritative for this process.

Keys are namespaced per account so account-a and account-b can never collide, and
kept strictly apart from the RQ keys (``translog:rate-search:*`` / ``rq:*``)::

    translog:store:<account_id>:requests   Hash  field=request_id -> QuotationRequest JSON
    translog:store:<account_id>:threads    Hash  field=request_id -> Thread JSON

The client is injected (built by the shared ``interface.jobs.queue.build_redis``
in the composition root), so this adapter imports no Redis configuration of its
own and never reaches into ``interface``.
"""

from __future__ import annotations

from typing import Any

from translog_quote.domain.conversation import Thread
from translog_quote.domain.workflow import QuotationRequest
from translog_quote.observability import get_logger

_log = get_logger("adapters.store.redis")

#: Root of every store key. Distinct from the queue's ``translog:rate-search:*``
#: so the durable store and the job broker never touch each other's keys.
STORE_NAMESPACE = "translog:store"


def _text(value: Any) -> str:
    """A Redis reply as text. ``build_redis`` does not decode responses, so hash
    fields and values arrive as ``bytes``; a fake or a decoding client may return
    ``str`` already."""
    if isinstance(value, bytes | bytearray):
        return value.decode("utf-8")
    return str(value)


class RedisStore:
    """A :class:`~translog_quote.ports.StorePort` backed by two Redis hashes."""

    def __init__(self, client: Any, *, account_id: str | None = None) -> None:
        self._client = client
        account = account_id or "default"
        self._requests_key = f"{STORE_NAMESPACE}:{account}:requests"
        self._threads_key = f"{STORE_NAMESPACE}:{account}:threads"

        self._requests: dict[str, QuotationRequest] = {
            _text(field): QuotationRequest.model_validate_json(_text(value))
            for field, value in (client.hgetall(self._requests_key) or {}).items()
        }
        self._threads: dict[str, Thread] = {
            _text(field): Thread.model_validate_json(_text(value))
            for field, value in (client.hgetall(self._threads_key) or {}).items()
        }
        if self._requests or self._threads:
            _log.info(
                "Loaded durable state from Redis (account=%s): %d request(s), %d thread(s)",
                account,
                len(self._requests),
                len(self._threads),
            )

    # ------------------------------------------------------------ StorePort --

    def get_request(self, request_id: str) -> QuotationRequest | None:
        return self._requests.get(request_id)

    def save_request(self, request: QuotationRequest) -> None:
        self._requests[request.request_id] = request
        self._client.hset(self._requests_key, request.request_id, request.model_dump_json())

    def all_requests(self) -> tuple[QuotationRequest, ...]:
        return tuple(self._requests[key] for key in sorted(self._requests))

    def all_threads(self) -> tuple[Thread, ...]:
        return tuple(self._threads[key] for key in sorted(self._threads))

    def save_thread(self, thread: Thread) -> None:
        self._threads[thread.request_id] = thread
        self._client.hset(self._threads_key, thread.request_id, thread.model_dump_json())

    # ---------------------------------------------------- atomic commit -------

    def commit_request_and_thread(
        self, request: QuotationRequest, thread: Thread | None
    ) -> None:
        """Persist a request and (optionally) its thread in one MULTI/EXEC.

        The two hash-field writes are applied atomically, so a crash can never
        leave a durable request without its dedup thread (or the reverse) — the
        sub-window ``JsonFileStore``'s two separate file writes left open. The
        in-memory cache is updated only after ``EXEC`` returns, so a failed
        transaction leaves the cache consistent with Redis.

        ``bootstrap.commit_request`` calls this when the durable store offers it,
        and falls back to two plain saves otherwise (the filesystem/in-memory
        stores), so ``StorePort`` itself is unchanged.
        """
        pipe = self._client.pipeline(transaction=True)
        pipe.hset(self._requests_key, request.request_id, request.model_dump_json())
        if thread is not None:
            pipe.hset(self._threads_key, thread.request_id, thread.model_dump_json())
        pipe.execute()
        self._requests[request.request_id] = request
        if thread is not None:
            self._threads[thread.request_id] = thread
