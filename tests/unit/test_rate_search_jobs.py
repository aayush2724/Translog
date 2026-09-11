"""The asynchronous job vocabulary: identity, states, results, and the lock.

No Redis anywhere — the pure halves are tested pure, and the lock is driven
against a dictionary-backed fake connection.
"""

from __future__ import annotations

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
        "search_date": date(2026, 9, 15),
        "commodity": "General Cargo",
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
        {"search_date": date(2026, 9, 16)},
        {"commodity": "Pharmaceuticals"},
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


def test_commodity_is_required_and_never_defaulted() -> None:
    """VR-5's mirror: commodity is stated by the caller, or the request does
    not exist. No 'General Cargo' appears on anyone's behalf."""
    fields = request().model_dump()
    del fields["commodity"]
    with pytest.raises(ValidationError):
        RateSearchJobRequest(**fields)
    with pytest.raises(ValidationError):
        request(commodity="")


def test_the_query_carries_stated_places_and_no_invented_code() -> None:
    query = request().to_query()

    assert query.origin.stated == "Bangalore"
    assert query.origin.code is None
    assert query.origin.resolved_by is None
    assert query.destination.stated == "Manila"
    assert query.date == date(2026, 9, 15)
    assert query.commodity == "General Cargo"  # stated, never substituted


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
