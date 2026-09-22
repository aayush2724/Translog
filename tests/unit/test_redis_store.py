"""RedisStore: a durable StorePort over Redis, restart-safe and account-isolated.

Uses the in-memory FakeRedis double (tests/unit/fake_redis.py) — one instance is
one Redis DB, so handing the same instance to a second RedisStore models a
process restart with no code able to tell the difference.
"""

from __future__ import annotations

from datetime import UTC, datetime

from tests.unit.fake_redis import FailingCommitRedis, FakeRedis, keys_of

from translog_quote.adapters.store.redis import RedisStore
from translog_quote.domain.conversation import Thread
from translog_quote.domain.shipment import RequestSource, ShipmentRecord
from translog_quote.domain.workflow import QuotationRequest, RequestState


def _request(request_id: str, *, state: RequestState = RequestState.EXTRACTED, **fields: object):  # type: ignore[no-untyped-def]
    return QuotationRequest(
        request_id=request_id,
        state=state,
        record=ShipmentRecord(request_id=request_id, source=RequestSource.EMAIL),
        client_address="client@example.com",
        **fields,  # type: ignore[arg-type]
    )


def _thread(request_id: str, *message_ids: str) -> Thread:
    return Thread(request_id=request_id, message_ids=message_ids)


def test_request_and_thread_round_trip() -> None:
    redis = FakeRedis()
    store = RedisStore(redis, account_id="account-a")

    store.save_request(_request("R-1"))
    store.save_thread(_thread("R-1", "<m1@x>"))

    got = store.get_request("R-1")
    assert got is not None and got.request_id == "R-1"
    assert store.all_requests()[0].request_id == "R-1"
    assert store.all_threads()[0].message_ids == ("<m1@x>",)


def test_state_survives_a_restart() -> None:
    """A fresh RedisStore over the same Redis reloads everything — the whole point."""
    redis = FakeRedis()
    first = RedisStore(redis, account_id="account-a")
    first.save_request(_request("R-1", state=RequestState.MANUAL_REVIEW))
    first.save_thread(_thread("R-1", "<m1@x>"))

    reborn = RedisStore(redis, account_id="account-a")  # simulated restart

    restored = reborn.get_request("R-1")
    assert restored is not None
    assert restored.state is RequestState.MANUAL_REVIEW
    assert reborn.all_threads()[0].message_ids == ("<m1@x>",)


def test_accounts_never_collide() -> None:
    redis = FakeRedis()
    a = RedisStore(redis, account_id="account-a")
    b = RedisStore(redis, account_id="account-b")

    a.save_request(_request("R-A"))
    a.save_thread(_thread("R-A", "<a@x>"))
    b.save_request(_request("R-B"))

    assert [r.request_id for r in a.all_requests()] == ["R-A"]
    assert [r.request_id for r in b.all_requests()] == ["R-B"]
    assert b.get_request("R-A") is None
    # Distinct, account-scoped keys under the store namespace, apart from RQ keys.
    assert "translog:store:account-a:requests" in keys_of(redis)
    assert "translog:store:account-b:requests" in keys_of(redis)
    assert all(k.startswith("translog:store:") for k in keys_of(redis))


def test_failure_notice_dedup_field_survives_a_restart() -> None:
    redis = FakeRedis()
    at = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    RedisStore(redis, account_id="account-a").save_request(
        _request("R-1", state=RequestState.MANUAL_REVIEW, failure_notice_sent_at=at)
    )

    restored = RedisStore(redis, account_id="account-a").get_request("R-1")
    assert restored is not None
    assert restored.failure_notice_sent_at == at


def test_clarification_followup_field_survives_a_restart() -> None:
    redis = FakeRedis()
    at = datetime(2026, 9, 21, 12, 30, tzinfo=UTC)
    RedisStore(redis, account_id="account-a").save_request(
        _request("R-1", state=RequestState.CLARIFICATION_SENT, clarification_followup_sent_at=at)
    )

    restored = RedisStore(redis, account_id="account-a").get_request("R-1")
    assert restored is not None
    assert restored.clarification_followup_sent_at == at


def test_atomic_commit_writes_request_and_thread_together() -> None:
    redis = FakeRedis()
    store = RedisStore(redis, account_id="account-a")

    store.commit_request_and_thread(_request("R-1"), _thread("R-1", "<m1@x>"))

    reborn = RedisStore(redis, account_id="account-a")
    assert reborn.get_request("R-1") is not None
    assert reborn.all_threads()[0].message_ids == ("<m1@x>",)


def test_a_failed_atomic_commit_leaves_nothing_and_a_consistent_cache() -> None:
    """If EXEC raises, neither the request nor the thread is durable, and the
    in-memory cache is not updated — so a later all_threads() (the watermark's
    input) never counts an uncommitted message as settled."""
    redis = FailingCommitRedis()
    store = RedisStore(redis, account_id="account-a")

    try:
        store.commit_request_and_thread(_request("R-1"), _thread("R-1", "<m1@x>"))
    except RuntimeError:
        pass
    else:  # pragma: no cover - the fake is defined to raise
        raise AssertionError("expected the simulated EXEC failure to propagate")

    assert store.get_request("R-1") is None, "cache not updated on failed commit"
    assert store.all_threads() == ()
    # And nothing reached Redis, so a restart sees nothing either.
    reborn = RedisStore(redis, account_id="account-a")
    assert reborn.get_request("R-1") is None
    assert reborn.all_threads() == ()
