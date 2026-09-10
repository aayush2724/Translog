"""The rate-search API contract: 202-and-poll, honest failure codes.

The queue functions are stubbed at the app module's boundary, so these tests
exercise the HTTP contract — validation, idempotent acceptance, polling, 404,
and a down queue — without Redis. Skipped wholesale where the `api` extra is
not installed; the core suite must pass on a clean checkout.
"""

from __future__ import annotations

from typing import Any

import pytest
from tests.unit.test_rate_search_jobs import request

pytest.importorskip("fastapi")
redis_exceptions = pytest.importorskip("redis.exceptions")

from fastapi.testclient import TestClient  # noqa: E402

from translog_quote.config import Settings  # noqa: E402
from translog_quote.interface.api import app as app_module  # noqa: E402
from translog_quote.interface.jobs import JobState, JobStatus  # noqa: E402

VALID = request().model_dump(mode="json")


def client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    enqueue: Any = None,
    fetch: Any = None,
) -> TestClient:
    if enqueue is not None:
        monkeypatch.setattr(app_module, "enqueue_rate_search", enqueue)
    if fetch is not None:
        monkeypatch.setattr(app_module, "fetch_job_status", fetch)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    return TestClient(app_module.create_app(settings))


# --- POST: validate, enqueue, 202 --------------------------------------------------


def test_a_valid_request_is_accepted_with_a_job_id(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []

    def fake_enqueue(req: object, settings: object) -> tuple[str, bool]:
        seen.append(req)
        return "rate-search-abc", True

    response = client(monkeypatch, enqueue=fake_enqueue).post("/api/rate-search", json=VALID)

    assert response.status_code == 202
    body = response.json()
    assert body == {
        "job_id": "rate-search-abc",
        "created": True,
        "status_url": "/api/rate-search/rate-search-abc",
    }
    assert len(seen) == 1  # exactly one enqueue for one POST


def test_a_repeated_request_joins_the_existing_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """`created=False` is the idempotency contract answering, not an error."""
    response = client(
        monkeypatch, enqueue=lambda req, settings: ("rate-search-abc", False)
    ).post("/api/rate-search", json=VALID)

    assert response.status_code == 202
    assert response.json()["created"] is False


def test_an_invalid_request_is_rejected_before_the_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def exploding_enqueue(req: object, settings: object) -> tuple[str, bool]:
        raise AssertionError("the queue must not be reached")

    api = client(monkeypatch, enqueue=exploding_enqueue)

    assert api.post("/api/rate-search", json={}).status_code == 422
    assert (
        api.post("/api/rate-search", json={**VALID, "weight_kg": -1}).status_code == 422
    )
    assert (
        api.post("/api/rate-search", json={**VALID, "airport_code": "BLR"}).status_code
        == 422
    )


def test_a_down_queue_is_503_not_a_stack_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(req: object, settings: object) -> tuple[str, bool]:
        raise redis_exceptions.ConnectionError("connection refused")

    response = client(monkeypatch, enqueue=down).post("/api/rate-search", json=VALID)

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "unavailable" in detail
    assert "refused" not in detail  # the transport error's own text stays inside


# --- GET: poll -----------------------------------------------------------------------


def test_polling_a_known_job_returns_its_state(monkeypatch: pytest.MonkeyPatch) -> None:
    status = JobStatus(job_id="rate-search-abc", state=JobState.FAILED, error="X: reason")

    response = client(monkeypatch, fetch=lambda job_id, settings: status).get(
        "/api/rate-search/rate-search-abc"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "failed"
    assert body["error"] == "X: reason"
    assert body["result"] is None


def test_polling_an_unknown_job_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    response = client(monkeypatch, fetch=lambda job_id, settings: None).get(
        "/api/rate-search/nope"
    )

    assert response.status_code == 404
