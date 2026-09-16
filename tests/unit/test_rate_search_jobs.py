"""The asynchronous job vocabulary: identity, states, results, and the lock.

No Redis anywhere — the pure halves are tested pure, and the lock is driven
against a dictionary-backed fake connection.
"""

from __future__ import annotations

import threading
import time
from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError
from tests.unit.test_transit_duration import _minutes, _rate

from translog_quote.domain.rates import FASTEST_ELIGIBLE, filter_rates, select_rate
from translog_quote.domain.shipment import CargoDimensions
from translog_quote.errors import ContractViolation, PermanentFailure
from translog_quote.interface.jobs import (
    JobState,
    LockHeartbeat,
    RateSearchJobRequest,
    RateSearchJobResult,
    WorkerLock,
    job_state_from_rq_status,
)

DIMS = CargoDimensions(length=34, width=24, height=6)


def request(**overrides: object) -> RateSearchJobRequest:
    base: dict[str, object] = {
        "origin": "Bangalore",
        "destination": "Manila",
        "weight_kg": 500.0,
        "dimensions_in": DIMS,
        "pieces": 8,
        "search_date": date(2026, 9, 15),
        "commodity": "General Cargo",
        "goods_type": "0000 - General Cargo",
    }
    base.update(overrides)
    return RateSearchJobRequest(**base)  # type: ignore[arg-type]


# --- idempotency ------------------------------------------------------------------


def test_identical_requests_share_one_job_identity() -> None:
    assert request().idempotency_key() == request().idempotency_key()
    assert request().idempotency_key().startswith("rate-search-")


@pytest.mark.parametrize(
    "change",
    [
        {"origin": "Mumbai"},
        {"destination": "Cebu"},
        {"weight_kg": 501.0},
        {"dimensions_in": CargoDimensions(length=35, width=24, height=6)},
        {"pieces": 9},
        {"search_date": date(2026, 9, 16)},
        {"commodity": "Pharmaceuticals"},
        {"goods_type": "1234 - Machinery"},
        {"cargo_is_liquid": True},
        {"requires_door_delivery": True},
    ],
)
def test_any_changed_field_is_a_different_job(change: dict[str, object]) -> None:
    assert request(**change).idempotency_key() != request().idempotency_key()


# --- the request is a strict edge -------------------------------------------------


def test_the_request_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        request(airport_code="BLR")  # codes are resolved by the provider, not stated


def test_the_request_rejects_a_nonpositive_weight() -> None:
    with pytest.raises(ValidationError):
        request(weight_kg=0)


def test_the_request_rejects_a_missing_or_nonpositive_piece_count() -> None:
    """Pieces is a required, positive business fact (VR: PCS_REQUIRED) — the
    queue never carries a shipment without a real count."""
    fields = request().model_dump()
    del fields["pieces"]
    with pytest.raises(ValidationError):
        RateSearchJobRequest(**fields)
    with pytest.raises(ValidationError):
        request(pieces=0)
    with pytest.raises(ValidationError):
        request(pieces=-2)


def test_commodity_is_required_and_never_defaulted() -> None:
    """VR-5's mirror: commodity is stated by the caller, or the request does
    not exist. No 'General Cargo' appears on anyone's behalf."""
    fields = request().model_dump()
    del fields["commodity"]
    with pytest.raises(ValidationError):
        RateSearchJobRequest(**fields)
    with pytest.raises(ValidationError):
        request(commodity="")


def test_goods_type_is_required_and_never_defaulted() -> None:
    """The Goods Type is decided before enqueue (rule or operator); the queue
    never carries a blank one, and none is invented here."""
    fields = request().model_dump()
    del fields["goods_type"]
    with pytest.raises(ValidationError):
        RateSearchJobRequest(**fields)
    with pytest.raises(ValidationError):
        request(goods_type="")


def test_the_query_carries_stated_places_and_no_invented_code() -> None:
    query = request().to_query()

    assert query.origin.stated == "Bangalore"
    assert query.origin.code is None
    assert query.origin.resolved_by is None
    assert query.destination.stated == "Manila"
    assert query.date == date(2026, 9, 15)
    assert query.commodity == "General Cargo"  # stated, never substituted
    assert query.goods_type == "0000 - General Cargo"  # decided before enqueue


def test_to_query_carries_the_piece_count() -> None:
    """The client's stated piece count reaches the provider query verbatim, so
    the WebCargo Pieces field reflects the real shipment."""
    assert request(pieces=8).to_query().pieces == 8


# --- RQ statuses become exactly four client-visible states ------------------------


@pytest.mark.parametrize(
    ("status", "state"),
    [
        ("queued", JobState.QUEUED),
        ("deferred", JobState.QUEUED),
        ("scheduled", JobState.QUEUED),
        ("started", JobState.PROCESSING),
        ("finished", JobState.COMPLETED),
        ("failed", JobState.FAILED),
        ("stopped", JobState.FAILED),
        ("canceled", JobState.FAILED),
    ],
)
def test_every_rq_status_has_an_explicit_state(status: str, state: JobState) -> None:
    assert job_state_from_rq_status(status) is state


def test_an_unknown_rq_status_is_a_contract_violation_not_a_guess() -> None:
    with pytest.raises(ContractViolation, match="unknown RQ job status"):
        job_state_from_rq_status("suspended")


# --- the result serialises without losing accountability --------------------------


def test_a_result_round_trips_with_exclusions_and_selection_intact() -> None:
    rates = (_rate("UL", transit=None), _rate("EK", transit=_minutes(1500)))
    filtered = filter_rates(rates)
    selection = select_rate(filtered.eligible, FASTEST_ELIGIBLE)

    result = RateSearchJobResult(
        adapter_id="webcargo-browser",
        is_simulated=False,
        returned=len(rates),
        query=request().to_query(),
        filtered=filtered,
        selection=selection,
    )

    revived = RateSearchJobResult.model_validate(result.model_dump(mode="json"))

    assert revived == result
    assert revived.selection is not None
    assert revived.selection.rate.carrier_code == "EK"
    assert revived.filtered.excluded[0].reason.value == "unrankable_no_transit"
    assert revived.is_simulated is False


# --- the one-browser-worker lock ---------------------------------------------------


class FakeRedis:
    """Just enough of the redis client for the lock: SET NX EX, GET, EXPIRE, DEL."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.ttls: dict[str, int] = {}

    def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> Any:
        if nx and key in self.store:
            return None
        self.store[key] = value.encode("utf-8")
        if ex is not None:
            self.ttls[key] = ex
        return True

    def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    def expire(self, key: str, ttl: int) -> None:
        self.ttls[key] = ttl

    def delete(self, key: str) -> None:
        self.store.pop(key, None)
        self.ttls.pop(key, None)


def test_a_second_worker_is_refused_loudly() -> None:
    """The intended deployment is exactly one browser worker per queue."""
    redis = FakeRedis()
    WorkerLock(redis, key="lock", ttl_seconds=120).acquire()

    with pytest.raises(PermanentFailure, match="exactly one browser worker"):
        WorkerLock(redis, key="lock", ttl_seconds=120).acquire()


def test_the_lock_expires_rather_than_wedging_a_restart() -> None:
    redis = FakeRedis()
    WorkerLock(redis, key="lock", ttl_seconds=120).acquire()

    assert redis.ttls["lock"] == 120  # a crashed worker's lock dies on its own


def test_refresh_extends_only_a_lock_this_worker_still_holds() -> None:
    redis = FakeRedis()
    lock = WorkerLock(redis, key="lock", ttl_seconds=120)
    lock.acquire()

    lock.refresh()  # fine: still ours

    redis.store["lock"] = b"someone-else"  # lease lapsed; another worker took it
    with pytest.raises(PermanentFailure, match="no longer held"):
        lock.refresh()


def test_release_never_removes_another_workers_lock() -> None:
    redis = FakeRedis()
    lock = WorkerLock(redis, key="lock", ttl_seconds=120)
    lock.acquire()
    redis.store["lock"] = b"someone-else"

    lock.release()

    assert redis.store["lock"] == b"someone-else"


# --- the lock heartbeat: keep the lease alive while the worker is idle -------------


class _CountingRedis(FakeRedis):
    """FakeRedis that counts EXPIRE calls and signals once a threshold is hit,
    so a test can wait for real heartbeat refreshes without blind sleeps."""

    def __init__(self, *, signal_after: int = 2) -> None:
        super().__init__()
        self.expire_calls = 0
        self._signal_after = signal_after
        self.reached = threading.Event()

    def expire(self, key: str, ttl: int) -> None:
        super().expire(key, ttl)
        self.expire_calls += 1
        if self.expire_calls >= self._signal_after:
            self.reached.set()


def test_heartbeat_refreshes_an_acquired_lock_while_idle() -> None:
    redis = _CountingRedis(signal_after=2)
    lock = WorkerLock(redis, key="lock", ttl_seconds=120)
    lock.acquire()
    lost: list[BaseException] = []
    beat = LockHeartbeat(lock, interval_seconds=0.01, on_lock_lost=lost.append)

    beat.start()
    try:
        assert redis.reached.wait(timeout=2)  # it refreshed the lease on its own
    finally:
        beat.stop()

    assert redis.expire_calls >= 2  # kept the lease alive across idle ticks
    assert lost == []  # ownership never lost
    assert not beat.is_alive()


def test_heartbeat_stops_beating_after_shutdown() -> None:
    redis = _CountingRedis(signal_after=1)
    lock = WorkerLock(redis, key="lock", ttl_seconds=120)
    lock.acquire()
    beat = LockHeartbeat(lock, interval_seconds=0.01, on_lock_lost=lambda _e: None)
    beat.start()
    assert redis.reached.wait(timeout=2)

    beat.stop()
    assert not beat.is_alive()

    settled = redis.expire_calls
    time.sleep(0.1)  # many intervals would have elapsed had it kept beating
    assert redis.expire_calls == settled  # a stopped heartbeat refreshes nothing


def test_a_lost_lock_is_surfaced_and_ends_the_heartbeat() -> None:
    redis = FakeRedis()
    lock = WorkerLock(redis, key="lock", ttl_seconds=120)
    lock.acquire()
    redis.store["lock"] = b"another-worker"  # the lease lapsed; someone else holds it

    surfaced: list[BaseException] = []
    done = threading.Event()

    def on_lost(exc: BaseException) -> None:
        surfaced.append(exc)
        done.set()

    beat = LockHeartbeat(lock, interval_seconds=0.01, on_lock_lost=on_lost)
    beat.start()
    try:
        assert done.wait(timeout=2)
    finally:
        beat.stop()

    assert len(surfaced) == 1
    assert isinstance(surfaced[0], PermanentFailure)  # the existing lock semantics
    assert not beat.is_alive()  # it stops the instant ownership is gone


def test_a_dead_heartbeat_lets_the_lease_expire_for_a_successor() -> None:
    """If the worker dies, the heartbeat dies with it, the un-refreshed lease
    lapses, and another worker can take the lock — the crash-recovery path."""
    redis = FakeRedis()
    lock = WorkerLock(redis, key="lock", ttl_seconds=120)
    lock.acquire()
    beat = LockHeartbeat(lock, interval_seconds=100, on_lock_lost=lambda _e: None)
    beat.start()

    beat.stop()  # the worker process ended: nothing refreshes the lease now
    assert not beat.is_alive()

    redis.delete("lock")  # real Redis would expire the un-refreshed TTL; model it
    WorkerLock(redis, key="lock", ttl_seconds=120).acquire()  # successor, no raise


def test_no_heartbeat_thread_leaks_after_shutdown() -> None:
    redis = _CountingRedis(signal_after=1)
    lock = WorkerLock(redis, key="lock", ttl_seconds=120)
    lock.acquire()
    beat = LockHeartbeat(lock, interval_seconds=0.01, on_lock_lost=lambda _e: None)
    beat.start()
    assert redis.reached.wait(timeout=2)

    beat.stop()

    assert not beat.is_alive()
    assert all(t.name != "worker-lock-heartbeat" for t in threading.enumerate())
