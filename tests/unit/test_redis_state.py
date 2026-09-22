"""RedisDemonstrationStore (watermark) and RedisAuditLog — the two durable
surfaces that live outside StorePort — survive a restart on Redis."""

from __future__ import annotations

from datetime import UTC, datetime

from tests.unit.fake_redis import FakeRedis, keys_of

from translog_quote.interface.web.redis_state import RedisAuditLog, RedisDemonstrationStore
from translog_quote.pipeline.audit import AuditEvent, AuditEventType


def test_watermark_persists_across_a_restart() -> None:
    redis = FakeRedis()
    at = datetime(2026, 9, 21, 8, 0, tzinfo=UTC)
    RedisDemonstrationStore(redis, account_id="account-a").record_watermark(at)

    reborn = RedisDemonstrationStore(redis, account_id="account-a")  # restart
    assert reborn.current.last_poll_watermark == at


def test_watermark_is_none_on_a_fresh_store() -> None:
    store = RedisDemonstrationStore(FakeRedis(), account_id="account-a")
    assert store.current.last_poll_watermark is None


def test_watermark_is_account_isolated() -> None:
    redis = FakeRedis()
    a_at = datetime(2026, 9, 21, 8, 0, tzinfo=UTC)
    RedisDemonstrationStore(redis, account_id="account-a").record_watermark(a_at)

    b = RedisDemonstrationStore(redis, account_id="account-b")
    assert b.current.last_poll_watermark is None
    assert "translog:store:account-a:watermark" in keys_of(redis)


def _event(request_id: str) -> AuditEvent:
    return AuditEvent(
        request_id=request_id,
        event=AuditEventType.EMAIL_RECEIVED,
        at=datetime(2026, 9, 21, 9, 0, tzinfo=UTC),
        detail={"message_id": f"<{request_id}@x>"},
    )


def test_audit_append_and_reload_across_a_restart() -> None:
    redis = FakeRedis()
    log = RedisAuditLog(redis, account_id="account-a")
    log.record(_event("R-1"))
    log.record(_event("R-2"))
    assert [e.request_id for e in log.events] == ["R-1", "R-2"]

    reborn = RedisAuditLog(redis, account_id="account-a")  # restart
    assert [e.request_id for e in reborn.events] == ["R-1", "R-2"]


def test_audit_is_account_isolated() -> None:
    redis = FakeRedis()
    RedisAuditLog(redis, account_id="account-a").record(_event("R-A"))
    b = RedisAuditLog(redis, account_id="account-b")
    assert b.events == []
    assert "translog:store:account-a:audit" in keys_of(redis)
