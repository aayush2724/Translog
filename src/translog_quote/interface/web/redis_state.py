"""Redis-backed durable variants of the watermark and audit stores.

Restart-safe operations needs more than the request store: the operations
**watermark** and the **audit** trail live outside ``StorePort`` (in
``DemonstrationFile`` and ``JsonFileAuditLog``), and on Render's free tier their
files are wiped on every restart just like the store. These two classes mirror
those file-backed ones API-for-API so ``LiveSession`` uses them interchangeably,
backed by Redis keys namespaced per account and kept apart from the RQ keys::

    translog:store:<account_id>:watermark   String  Demonstration JSON
    translog:store:<account_id>:audit        List    RPUSH AuditEvent JSON

Only selected when ``demo.durable_backend == "redis"`` (production); local, demo
and test runs keep the filesystem classes unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from translog_quote.interface.web.demonstration import Demonstration
from translog_quote.observability import get_logger
from translog_quote.pipeline.audit import AuditEvent

if TYPE_CHECKING:
    from datetime import datetime

_log = get_logger("interface.web.redis_state")

#: Same key root the RedisStore uses (``adapters.store.redis.STORE_NAMESPACE``),
#: repeated here rather than imported because ``interface`` may not import
#: ``adapters``. The exact per-account key strings are pinned by tests, so the two
#: cannot drift apart unnoticed.
STORE_NAMESPACE = "translog:store"


def _text(value: Any) -> str:
    """A Redis reply as text; ``build_redis_client`` does not decode responses, so
    values arrive as ``bytes`` (a fake may return ``str`` already)."""
    if isinstance(value, bytes | bytearray):
        return value.decode("utf-8")
    return str(value)


class RedisDemonstrationStore:
    """A ``DemonstrationFile``-compatible store keeping the demonstration and its
    watermark in one Redis key.

    In operations mode only the watermark is exercised (``record_watermark`` and
    ``current.last_poll_watermark``); the full :class:`Demonstration` is persisted
    anyway so the class is correct if ever used in demonstration mode too.
    """

    def __init__(self, client: Any, *, account_id: str | None = None) -> None:
        self._client = client
        account = account_id or "default"
        self._key = f"{STORE_NAMESPACE}:{account}:watermark"
        self.current = self._load()

    @property
    def path(self) -> str:
        return self._key

    def _load(self) -> Demonstration:
        raw = self._client.get(self._key)
        if raw is None:
            return Demonstration()
        try:
            return Demonstration.model_validate_json(_text(raw))
        except ValueError as exc:
            # An unreadable value must not stop the session; the safe reading is
            # "no demonstration / no watermark", matching DemonstrationFile.
            _log.warning("Could not read %s (%s); treating as no demonstration", self._key, exc)
            return Demonstration()

    def save(self, demonstration: Demonstration) -> None:
        self.current = demonstration
        self._client.set(self._key, demonstration.model_dump_json())

    def start(self, at: datetime) -> Demonstration:
        started = Demonstration(started_at=at)
        self.save(started)
        _log.info("New demonstration started at %s", at.isoformat())
        return started

    def include(self, request_id: str) -> Demonstration:
        updated = self.current.including(request_id)
        if updated is not self.current:
            self.save(updated)
        return updated

    def record_watermark(self, at: datetime) -> Demonstration:
        """Persist the mail cutoff after a successful poll, so a restart resumes
        from here rather than from wall-clock ``now``."""
        self.save(self.current.with_watermark(at))
        return self.current


class RedisAuditLog:
    """Collects audit events in memory and appends them to a Redis list.

    Both, like ``JsonFileAuditLog``: the in-memory list is what the snapshot
    renders from, and the Redis list is what makes it survive the process. A
    write that fails is logged and does **not** raise — losing a line of the
    display trail must never abort a workflow step that has already happened.
    """

    def __init__(self, client: Any, *, account_id: str | None = None) -> None:
        self._client = client
        account = account_id or "default"
        self._key = f"{STORE_NAMESPACE}:{account}:audit"
        self.events: list[AuditEvent] = list(self._load())

    @property
    def path(self) -> str:
        return self._key

    def _load(self) -> list[AuditEvent]:
        events: list[AuditEvent] = []
        for number, entry in enumerate(self._client.lrange(self._key, 0, -1) or [], start=1):
            try:
                events.append(AuditEvent.model_validate_json(_text(entry)))
            except ValueError:
                # An entry from an older shape, skipped rather than fatal — a
                # session must not refuse to start over one unreadable line.
                _log.warning("Skipping unreadable audit entry %d in %s", number, self._key)
        return events

    def record(self, event: AuditEvent) -> None:
        self.events.append(event)
        try:
            self._client.rpush(self._key, event.model_dump_json())
        except Exception as exc:  # noqa: BLE001 - a lost display line must not abort a step
            _log.warning("Could not append audit event to %s (%s)", self._key, exc)
